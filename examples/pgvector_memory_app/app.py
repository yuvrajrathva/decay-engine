"""decay-engine + pgvector: a tiny memory application you can actually run.

    docker compose run --rm app demo

Subcommands are listed by ``--help``. The interesting ones are `sweep`
(dry-run by default), `search --cold-mode`, and `demo`, which runs the whole
story end to end and asserts the outcomes.

The division of labour this example exists to show:

    the application          decay-engine
    ---------------          ------------
    writes memories          scores them by idle time and usage
    embeds and searches      decides keep / demote / evict / skip
    records retrievals  -->  reads last_retrieved_at, retrieval_count
    reads decay_cold    <--  writes decay_cold, decay_strength
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from typing import List, Optional

import memory_store as store
from sample_data import SAMPLES
from decay_engine import DecayEngine, DecayPolicy, FieldMap, SweepReport
from decay_engine.adapters.pgvector import PgVectorAdapter

# --- decay configuration -------------------------------------------------
#
# The FieldMap is the only place the library learns this schema's column names.
# created_at and pinned happen to match the defaults; the other two do not, and
# without this mapping every record would come back SKIP.
FIELD_MAP = FieldMap(
    created_at=store.CREATED_AT,
    last_accessed_at=store.LAST_RETRIEVED_AT,
    access_count=store.RETRIEVAL_COUNT,
    pinned=store.PINNED,
)

POLICY = DecayPolicy(
    half_life=timedelta(days=30),      # idle time at which an unused memory halves
    usage_boost=0.5,                   # retrievals stretch that half-life
    demote_below=0.5,                  # under this strength: flag cold
    evict_below=0.05,                  # under this strength: delete
    never_evict_above_accesses=5,      # well-used memories demote, never delete
    grace_period=timedelta(days=1),    # brand-new memories are untouchable
    field_map=FIELD_MAP,
)

# The timing columns are real Postgres columns rather than JSONB keys, so they
# are named in extra_columns; the adapter surfaces them into each record's
# metadata where the FieldMap above reads them. Decay's own writes still go to
# the jsonb column.
TIMING_COLUMNS = (
    store.CREATED_AT,
    store.LAST_RETRIEVED_AT,
    store.RETRIEVAL_COUNT,
    store.PINNED,
)

MAX_EVICTIONS = 100  # refuse to apply a sweep that would delete more than this

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
COLOR = {"keep": "\033[32m", "demote": "\033[33m", "evict": "\033[31m", "skip": "\033[36m"}


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print("─" * max(len(text), 48))


def action_label(action: str) -> str:
    return f"{COLOR.get(action, '')}{action:<7}{RESET}"


def build_engine(connection, *, write_scores: bool = False) -> DecayEngine:
    """Wrap the app's table in an adapter and hand it to the engine."""
    adapter = PgVectorAdapter(
        connection,
        table="memories",
        id_column="id",
        text_column="document",
        metadata_column="metadata",
        extra_columns=TIMING_COLUMNS,
    )
    return DecayEngine(
        adapter, POLICY, write_scores=write_scores, max_evictions=MAX_EVICTIONS
    )


# -- rendering ------------------------------------------------------------


def show_memories(connection) -> None:
    rows = store.list_memories(connection)
    print(f"{'id':<16}{'idle':>7}{'reads':>7}{'pin':>5}{'cold':>6}{'strength':>10}  document")
    print("─" * 100)
    for row in rows:
        idle = "   —   " if row.idle_days is None else f"{row.idle_days:6.1f}d"
        strength = "     —    " if row.strength is None else f"{row.strength:10.4f}"
        cold = "yes" if row.cold else "no"
        pin = "yes" if row.pinned else "-"
        print(
            f"{row.id:<16}{idle:>7}{row.retrieval_count:>7}{pin:>5}{cold:>6}"
            f"{strength}  {row.document[:44]}"
        )
    print(f"\n{len(rows)} memories in the store")


def show_report(report: SweepReport) -> None:
    print(f"{report.summary()}\n")
    print(f"{'id':<16}{'action':<8}{'strength':>10}  reason")
    print("─" * 100)
    ordered = sorted(report.verdicts, key=lambda v: -(v.strength or 0))
    for verdict in ordered:
        strength = "     —    " if verdict.strength is None else f"{verdict.strength:10.4f}"
        print(
            f"{verdict.record_id:<16}{action_label(verdict.action.value)} {strength}  "
            f"{DIM}{verdict.reason}{RESET}"
        )
    if report.eviction_limit_exceeded:
        print(
            f"\n{COLOR['evict']}eviction ceiling exceeded{RESET}: "
            f"{len(report.evicted_ids)} > max_evictions={MAX_EVICTIONS}; "
            "applying this sweep would raise EvictionLimitExceeded"
        )


def show_hits(rows: List[store.Row], *, cold_mode: str) -> None:
    if not rows:
        print("no matches")
        return
    adjusted_column = "adjusted" if cold_mode == "penalize" else ""
    print(f"{'id':<16}{'distance':>10}{adjusted_column:>10}{'cold':>6}{'strength':>10}  document")
    print("─" * 100)
    for row in rows:
        adjusted = f"{row.adjusted:10.4f}" if row.adjusted is not None else " " * 10
        strength = "     —    " if row.strength is None else f"{row.strength:10.4f}"
        cold = "yes" if row.cold else "no"
        print(
            f"{row.id:<16}{row.distance:10.4f}{adjusted}{cold:>6}{strength}  "
            f"{row.document[:40]}"
        )


# -- commands -------------------------------------------------------------


def cmd_seed(connection, args) -> int:
    inserted = store.seed(connection)
    print(f"seeded {inserted} memories (table truncated first)")
    show_memories(connection)
    return 0


def cmd_list(connection, args) -> int:
    show_memories(connection)
    return 0


def cmd_remember(connection, args) -> int:
    store.remember(connection, args.id, args.text)
    print(f"stored {args.id!r} with created_at = now() and retrieval_count = 0")
    return 0


def cmd_search(connection, args) -> int:
    rows = store.search(
        connection,
        args.query,
        limit=args.limit,
        cold_mode=args.cold_mode,
        penalty=args.penalty,
    )
    suffix = f"   penalty={args.penalty}" if args.cold_mode == "penalize" else ""
    print(f"query: {args.query!r}   cold_mode={args.cold_mode}{suffix}\n")
    show_hits(rows, cold_mode=args.cold_mode)

    if args.track and rows:
        used = [row.id for row in rows]
        store.record_access(connection, used)
        print(
            f"\nrecorded access for {len(used)} memories "
            f"(the application's job, not the library's): {', '.join(used)}"
        )
    elif not args.track:
        print(f"\n{DIM}--no-track: retrieval counters left unchanged{RESET}")
    return 0


def cmd_sweep(connection, args) -> int:
    engine = build_engine(connection, write_scores=args.write_scores)
    report = engine.sweep(dry_run=not args.apply)

    heading("APPLIED sweep" if args.apply else "DRY RUN (nothing was changed)")
    show_report(report)

    if args.apply:
        print(
            f"\nevicted {len(report.evicted_ids)}, demoted {len(report.demoted_ids)}, "
            f"promoted {len(report.promoted_ids)}; {store.count(connection)} memories remain"
        )
    else:
        print(
            f"\n{DIM}dry run: evicted_ids/demoted_ids are still reported "
            f"({len(report.evicted_ids)} would be deleted). "
            f"Re-run with --apply to act on it.{RESET}"
        )
    return 0


def cmd_pin(connection, args) -> int:
    store.pin(connection, args.id, not args.unpin)
    print(f"{'unpinned' if args.unpin else 'pinned'} {args.id!r}")
    return 0


def cmd_reset(connection, args) -> int:
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE memories")
    connection.commit()
    print("table truncated")
    return 0


# -- the guided demo ------------------------------------------------------


def cmd_demo(connection, args) -> int:
    failures: List[str] = []

    def check(condition: bool, description: str) -> None:
        if condition:
            print(f"  \033[32mok\033[0m   {description}")
        else:
            print(f"  \033[31mFAIL\033[0m {description}")
            failures.append(description)

    heading("1. Seed the store")
    print("Eight memories, chosen so every decay outcome is reachable.\n")
    store.seed(connection)
    show_memories(connection)

    heading("2. Retrieve, and record the access")
    print(
        "A similarity search is not an access. The app records only what it\n"
        "actually used -- here, the single best hit.\n"
    )
    hits = store.search(connection, "which units does the user prefer?", limit=1)
    show_hits(hits, cold_mode="exclude")
    before = {r.id: r.retrieval_count for r in store.list_memories(connection)}
    store.record_access(connection, [hits[0].id])
    after = {r.id: r.retrieval_count for r in store.list_memories(connection)}
    print(
        f"\nretrieval_count for {hits[0].id}: "
        f"{before[hits[0].id]} -> {after[hits[0].id]}, last_retrieved_at = now()"
    )
    check(after[hits[0].id] == before[hits[0].id] + 1, "the app incremented the counter")

    heading("3. Dry-run sweep")
    print(
        "The default. Decides everything, writes nothing. Note all four\n"
        "outcomes, and that SKIP is not the same as KEEP.\n"
    )
    engine = build_engine(connection)
    preview = engine.sweep()
    show_report(preview)

    expected = {sample.id: sample.expected for sample in SAMPLES}
    actual = {v.record_id: v.action.value for v in preview.verdicts}
    # pref-units was just retrieved, so its expectation still holds (keep).
    mismatches = {k: (expected[k], actual[k]) for k in expected if expected[k] != actual.get(k)}
    check(not mismatches, f"every verdict matched expectations {mismatches or ''}")
    check(store.count(connection) == 8, "dry run deleted nothing")
    check(
        bool(preview.evicted_ids) and not preview.applied,
        "dry run still reported evicted_ids (they are not empty on a preview)",
    )
    check(
        all(not row.cold for row in store.list_memories(connection)),
        "dry run wrote no decay_cold flags",
    )

    heading("4. Apply the same sweep")
    applied = engine.sweep(dry_run=False)
    show_report(applied)
    print()
    show_memories(connection)
    check(applied.applied, "report says it was applied")
    check(
        sorted(applied.evicted_ids) == sorted(preview.evicted_ids),
        "applying evicted exactly what the dry run predicted",
    )
    check(store.count(connection) == 6, "two memories were deleted")
    rows = {row.id: row for row in store.list_memories(connection)}
    check("debug-trace" not in rows and "scratch-note" not in rows, "the junk is gone")
    check("legacy-import" in rows, "the unmeasurable record was NOT deleted (SKIP)")
    check("contact-email" in rows, "the pinned record survived 500 idle days")
    check(
        rows["name-timezone"].cold and rows["name-timezone"].strength is not None,
        "tenure demoted the well-used record instead of deleting it",
    )
    check(
        rows["sso-pricing"].cold,
        "the cooling record is flagged decay_cold with its strength written back",
    )

    heading("5. How demotion changes retrieval")
    print(
        "The library wrote decay_cold and decay_strength. What retrieval does\n"
        "with them is the application's decision -- three options, same query.\n"
        "Watch where sso-pricing lands: it is the closest match by raw distance,\n"
        "but it is also the memory that just went cold.\n"
    )
    query = "did the user ask about SSO pricing?"
    for mode in ("ignore", "exclude", "penalize"):
        print(f"{BOLD}cold_mode={mode}{RESET}")
        rows_for_mode = store.search(connection, query, limit=6, cold_mode=mode)
        show_hits(rows_for_mode, cold_mode=mode)
        print()
    print(
        f"{DIM}penalize scales distance by 1 + penalty x (1 - strength). At the default\n"
        f"penalty of 2.0 the cold memory falls from 1st to 3rd but stays reachable;\n"
        f"raise --penalty far enough and it drops out of a small top-k altogether.{RESET}"
    )
    ignored = store.search(connection, query, limit=6, cold_mode="ignore")
    excluded = store.search(connection, query, limit=6, cold_mode="exclude")
    penalized = store.search(connection, query, limit=6, cold_mode="penalize")
    check(
        ignored[0].id == "sso-pricing",
        "ignore: the cold memory still ranks first on raw distance",
    )
    check(
        not any(r.id == "sso-pricing" for r in excluded),
        "exclude: the cold memory is filtered out of retrieval",
    )
    rank = [r.id for r in penalized].index("sso-pricing") + 1
    check(
        rank > 1,
        f"penalize: the cold memory fell from rank 1 to rank {rank}, still reachable",
    )

    heading("6. A cold memory that gets used again is promoted")
    print(
        "Demotion is reversible. The app records an access on the cold memory,\n"
        "and the next sweep clears the flag -- nothing was lost by demoting it.\n"
    )
    store.record_access(connection, ["sso-pricing"])
    print("recorded access for sso-pricing (strength resets: idle time is now ~0)")
    promoted = engine.sweep(dry_run=False)
    show_report(promoted)
    rows = {row.id: row for row in store.list_memories(connection)}
    check("sso-pricing" in promoted.promoted_ids, "the revived memory was promoted")
    check(not rows["sso-pricing"].cold, "its decay_cold flag was cleared")
    check(store.count(connection) == 6, "promotion deleted nothing")

    heading("Summary")
    if failures:
        print(f"{COLOR['evict']}{len(failures)} check(s) failed{RESET}")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"{COLOR['keep']}All checks passed.{RESET}")
    print(
        "\nThings worth taking away:\n"
        "  * SKIP is not KEEP. A memory decay cannot measure is reported, never deleted.\n"
        "  * DEMOTE is reversible, and is what usually should happen. EVICT is the blunt case.\n"
        "  * A dry run reports the same ids an applied sweep acts on; only `applied` differs.\n"
        "  * The library never touched last_retrieved_at or retrieval_count. That was all app code.\n"
    )
    print(f"{DIM}Try: docker compose run --rm app search 'dark mode' --cold-mode penalize{RESET}")
    return 0


# -- entry point ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app",
        description="A small AI-memory application backed by pgvector, kept tidy "
        "by decay-engine.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres DSN (default: $DATABASE_URL)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("demo", help="run the whole guided story and verify it")
    subcommands.add_parser("seed", help="replace the table with the sample memories")
    subcommands.add_parser("list", help="show every memory and its decay state")
    subcommands.add_parser("reset", help="empty the table")

    remember = subcommands.add_parser("remember", help="store one new memory")
    remember.add_argument("id")
    remember.add_argument("text")

    search = subcommands.add_parser("search", help="vector similarity search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=3)
    search.add_argument(
        "--cold-mode",
        choices=store.COLD_MODES,
        default="exclude",
        help="how demoted memories affect retrieval (default: exclude)",
    )
    search.add_argument(
        "--penalty",
        type=float,
        default=2.0,
        help="how harshly cold_mode=penalize down-ranks weak memories "
        "(0 behaves like --cold-mode ignore; default: 2.0)",
    )
    search.add_argument(
        "--no-track",
        dest="track",
        action="store_false",
        help="do not record an access for the hits",
    )

    sweep = subcommands.add_parser("sweep", help="run a decay sweep (dry run by default)")
    sweep.add_argument("--apply", action="store_true", help="actually write and delete")
    sweep.add_argument(
        "--write-scores",
        action="store_true",
        help="also stamp kept records with their strength",
    )

    pin = subcommands.add_parser("pin", help="protect a memory from eviction")
    pin.add_argument("id")
    pin.add_argument("--unpin", action="store_true")

    return parser


COMMANDS = {
    "demo": cmd_demo,
    "seed": cmd_seed,
    "list": cmd_list,
    "remember": cmd_remember,
    "search": cmd_search,
    "sweep": cmd_sweep,
    "pin": cmd_pin,
    "reset": cmd_reset,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dsn:
        print(
            "No database DSN. Set DATABASE_URL or pass --dsn.\n"
            "Inside Docker Compose this is set for you; see the example's README.",
            file=sys.stderr,
        )
        return 2

    connection = store.connect(args.dsn)
    try:
        return COMMANDS[args.command](connection, args)
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
