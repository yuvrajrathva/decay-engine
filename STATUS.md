# Build status

Where `decay-engine` actually stands, as of 2026-10-06. For how to *use* it, see
[README.md](README.md); this file is the inventory of what exists, what each
piece is, and what is still missing.

Version `0.1.0`. Python 3.10.12, zero runtime dependencies in the core.
**110 tests passing** (30 policy, 25 engine, 15 Chroma, 40 pgvector), lint clean.

Deeper documentation:
[timestamps and the grace period](docs/timestamps-and-grace-period.md),
[access tracking](docs/access-tracking.md).

## The vocabulary

One line each, in roughly the order data flows through them.

| Term | What it is |
| --- | --- |
| **MemoryRecord** | One row of an agent's memory as the adapter found it: an `id`, optional `text`, and whatever `metadata` dict the store already keeps. |
| **FieldMap** | A lookup table saying which metadata keys hold the timing signals — created-at, last-accessed-at, access count, pinned — so the library never demands you reshape your schema. |
| **MemorySignals** | The normalized result of applying a `FieldMap` to a record: real `datetime` objects and a float access count, whatever string or epoch mess the store held. |
| **StoreAdapter** | The boundary to a memory store, and the entire integration surface: stream records, patch metadata, delete by id. Three methods. |
| **Policy** | Something that looks at one record and returns one verdict. Stateless by contract, so policies are composable and testable in isolation. |
| **DecayPolicy** | The one policy that exists: scores a record by how long it has sat idle and how often it has been used, then picks an action. |
| **Action** | The four possible outcomes — `KEEP`, `DEMOTE`, `EVICT`, `SKIP`. |
| **Verdict** | A policy's decision about one record, carrying the action, the strength score, and a human-readable reason it can be audited by. |
| **DecayEngine** | The orchestrator: reads the whole store, asks the policy about every record, then applies the verdicts — but only if you asked it to. |
| **SweepReport** | The outcome of a sweep: every verdict, the counts, and which ids were evicted, demoted, or promoted — filled in on a dry run as well, with `applied` saying whether any of it happened. |
| **Strength** | A record's score in `(0, 1]`. Exponential decay on idle time, with usage stretching the half-life. The number every decision comes from. |

Two distinctions in that table carry weight:

- **`SKIP` is not `KEEP`.** Keep is a decision; skip is the absence of one. A
  record whose timestamps the field map could not read is reported and left
  alone, never deleted.
- **`DEMOTE` is not `EVICT`.** Demotion flags a record cold and writes its score
  back, leaving it in the store for your retrieval layer to down-rank. It is
  reversible — a demoted record that gets used again is promoted on the next
  sweep.

## What is built

| Component | File | State |
| --- | --- | --- |
| Record / signal types | `decay_engine/models.py` | **Done.** Timestamp coercion handles `datetime`, ISO-8601 (incl. trailing `Z`), and epoch seconds *or* milliseconds, with an unparseable value returning `None` rather than a guess. |
| Field mapping | `decay_engine/models.py` | **Done.** Each field takes one key or several tried in order; defaults already cover `created_at` / `createdAt` / `timestamp`, `access_count` / `hits`, and friends. |
| Adapter protocol | `decay_engine/adapters/base.py` | **Done.** `iter_records` / `update_metadata` / `delete`, plus a shared `chunked` batching helper. |
| **Chroma adapter** | `decay_engine/adapters/chroma.py` | **Done and verified against live chromadb 1.5.9.** Pages by offset/limit, and merges metadata patches by reading first — necessary because Chroma's `update` replaces a record's metadata wholesale. Coerces values to the scalars Chroma accepts (`datetime` → ISO string, `None` → `""`). |
| **pgvector adapter** | `decay_engine/adapters/pgvector.py` | **Done and verified against live Postgres 16 + pgvector.** Takes the table layout as configuration, since pgvector imposes no schema. Reads timing signals from a `jsonb` column, from first-class columns named in `extra_columns`, or both. Pages by keyset (`WHERE id > last_seen`) not `OFFSET`; merges patches inside Postgres (`metadata \|\| patch`) so concurrent writers are not clobbered. SQL identifiers are validated, not escaped. |
| In-memory adapter | `decay_engine/adapters/memory.py` | **Done.** A dict-backed store for tests and for trying policies on sample data before pointing them at production. |
| Policy contract | `decay_engine/policies/base.py` | **Done.** `Action`, `Verdict`, and a runtime-checkable `Policy` protocol. |
| **Decay policy** | `decay_engine/policies/decay.py` | **Done.** Recency × usage scoring, three-band thresholds, and the four protections below. |
| **Sweep engine** | `decay_engine/engine.py` | **Done.** Read phase then write phase, never interleaved. Dry run by default, eviction ceiling, promotion of revived records, per-verdict audit callback. A report records its decisions before either path acts, so `evicted_ids` / `demoted_ids` / `promoted_ids` are populated on dry runs too and `applied` is the only flag that differs. |
| Worked example | `examples/preview_sweep.py` | **Done.** Runnable: prints a full verdict table over seven synthetic memories. |

### The scoring model, concretely

```
effective_half_life = half_life * (1 + usage_boost * ln(1 + access_count))
strength            = 0.5 ** (idle_time / effective_half_life)
```

Idle time is measured **from last access, not creation** — a two-year-old note
read yesterday is hot, a note from last month that nothing retrieved is cold.
Usage **extends the half-life** rather than adding to the score, so a memory the
agent leans on constantly decays slowly instead of getting a one-time bonus that
dormancy erases.

### Current defaults

| Knob | Default | Meaning |
| --- | --- | --- |
| `half_life` | 30 days | Idle time at which an unused memory hits strength 0.5. The one knob worth tuning per store. |
| `usage_boost` | 0.5 | How much each access slows decay. `0` makes scoring pure recency. |
| `demote_below` | 0.5 | Under this strength, flag cold. |
| `evict_below` | 0.05 | Under this strength, delete. |
| `grace_period` | 1 day | Newly created records are untouchable. |
| `never_evict_above_accesses` | 5 | At or above this access count, demote-only — never delete. |
| `max_usage_multiplier` | 10.0 | Ceiling on the half-life stretch, so a runaway counter can't make a record immortal. |
| `respect_pinned` | `True` | Honor the pinned flag from the field map. |
| `write_scores` *(engine)* | `False` | Also stamp kept records with their score. Off because it is one write per record. |
| `max_evictions` *(engine)* | `None` | Refuse to *apply* a sweep deleting more than this. |

`never_evict_above_accesses` exists because of a defect the worked example
caught: exponential decay is unforgiving, so usage stretches the half-life but
cannot defeat the curve, and a record retrieved ninety times still got deleted
after 400 days idle. For a library whose job is protecting what matters, that was
the wrong failure direction.

## What is not built

- **Other cleaning policies.** Near-duplicate merging, consolidation /
  summarization, and contradiction resolution are all deliberately out of scope
  for `0.1.0`. The `Policy` protocol is the seam they would plug into.
- **Policy composition.** `DecayEngine` takes exactly one policy; there is no
  pipeline that runs several and reconciles their verdicts.
- **Scoped sweeps.** `sweep()` always walks the whole collection. No filtering to
  a namespace, tenant, or user — which multi-tenant stores will need.
- **Access-count writing.** Nothing here increments `access_count` or
  `last_accessed_at`; the library only reads them. The host application must
  record retrievals — see [docs/access-tracking.md](docs/access-tracking.md).
- **CLI.** Library only.
- **Other adapters.** Chroma and pgvector ship; Qdrant, Weaviate, and Pinecone
  do not. Nothing in the core assumes any of them.
- **First-class column writes (pgvector).** `extra_columns` is read-only. Decay's
  own writes always go to the `jsonb` column, so a schema with no `jsonb` column
  can be swept in dry-run and evicted from, but not demoted.
- **Packaging.** Not published to PyPI, and not committed to git — the directory
  is untracked inside the parent `Devdev` repo.

## Verification

```bash
.venv/bin/python -m pytest        # 110 passed
```

The suite needs neither a database nor `chromadb` to run. Each adapter is tested
against a double reproducing its backend's awkward parts — Chroma replacing
metadata on update and rejecting non-scalars; Postgres' keyset paging, `jsonb ||`
merge, and `= ANY(array)` deletes — plus live round-trip tests that skip when the
real backend is unavailable.

Both live paths have been run and pass: **chromadb 1.5.9** and **Postgres 16 with
pgvector** (including a check that a metadata patch leaves the `vector` column
untouched). To reproduce the Postgres run:

```bash
docker run -d --rm --name decay-pg -e POSTGRES_PASSWORD=decay \
  -e POSTGRES_DB=decay -p 55432:5432 pgvector/pgvector:pg16
DECAY_ENGINE_PG_DSN="postgresql://postgres:decay@localhost:55432/decay" \
  .venv/bin/python -m pytest
```

Coverage by area: the decay curve and its refusals (30), sweep orchestration and
the safety guards (25), Chroma paging and metadata merging (15), pgvector
construction, paging, merging, deletion and identifier safety (40).

## Open questions

1. **Demotion only pays off if the retrieval layer reads it.** The library writes
   `decay_cold` and `decay_strength` and trusts you to filter or re-rank on them.
   If an agent ignores those fields, demotion is a no-op and eviction does all
   the work — which changes how the thresholds should be tuned.
2. **Access tracking is the real dependency, and it is the host's job.** Without
   a counter something increments on retrieval, decay degrades to pure aging and
   no record ever earns tenure. Documented in
   [docs/access-tracking.md](docs/access-tracking.md); still worth considering
   whether the library should ship an opt-in helper that does the increment.
3. **The grace period reads `created_at`** (`policies/decay.py:122`), so a
   backfilled import with original timestamps bypasses it. That is now a
   documented, deliberate choice — the developer picks which timestamp means
   "created" — rather than an open bug. See
   [docs/timestamps-and-grace-period.md](docs/timestamps-and-grace-period.md).
   The open part is whether `FieldMap` should grow a distinct `inserted_at` so
   both the true creation time and the store-entry time can be honored at once.
4. **Scoped sweeps.** Multi-tenant stores will want to sweep one namespace at a
   time; `sweep()` currently always walks everything.
