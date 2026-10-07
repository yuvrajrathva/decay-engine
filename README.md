# decay-engine

A lightweight library that keeps an AI agent's long-term memory clean over time.

Agent memory stores only grow. Every conversation deposits a few more facts, and
nothing ever takes anything out, so retrieval quality decays as the store fills
with stale preferences, one-off questions, and debugging noise. `decay-engine`
plugs into the store you already have and sweeps it the way a good filing system
keeps a drawer usable.

It is not a memory store. It has no opinion about your embeddings, your schema,
or your retrieval strategy. It reads records, scores them, and either marks them
cold or removes them.

- **Zero dependencies** in the core. Chroma support is an optional extra.
- **Dry run by default.** Every sweep is a preview until you say otherwise.
- **Store-agnostic.** Three methods — stream, patch metadata, delete by id — are
  all a store has to provide.

## Documentation

- [docs/timestamps-and-grace-period.md](docs/timestamps-and-grace-period.md) —
  which timestamps decay reads, how to map your own, how to handle imported or
  historical records, and what the grace period protects.
- [docs/access-tracking.md](docs/access-tracking.md) — the fields your
  application must maintain, when to write them, and how decay degrades without
  them. **The library does not track access itself.**
- [STATUS.md](STATUS.md) — what is built, what is not, and the vocabulary.

## Install

```bash
pip install decay-engine              # core, no dependencies
pip install "decay-engine[chroma]"    # with the Chroma adapter
pip install "decay-engine[pgvector]"  # with the pgvector adapter
```

## Use

```python
from decay_engine import DecayEngine
from decay_engine.adapters.chroma import ChromaAdapter

engine = DecayEngine(ChromaAdapter(collection))

report = engine.sweep()            # dry run: decides, changes nothing
print(report.summary())
# swept 4812 records (dry run): 3901 keep, 780 demote, 108 evict, 23 skip

engine.sweep(dry_run=False)        # apply
```

Look before you leap:

```python
from decay_engine import Action

for verdict in report.by_action(Action.EVICT):
    print(verdict.record_id, verdict.strength, verdict.reason)
```

Run `python examples/preview_sweep.py` for a full worked sweep over sample data.

## How scoring works

A memory's **strength** is exponential decay on the time since it last mattered,
and usage slows that decay:

```
effective_half_life = half_life * (1 + usage_boost * ln(1 + access_count))
strength            = 0.5 ** (idle_time / effective_half_life)
```

Strength lands in `(0, 1]`, and the thresholds sort each record into one of three
outcomes:

| strength | outcome | what happens |
| --- | --- | --- |
| `>= demote_below` | **keep** | nothing, unless `write_scores=True` |
| `< demote_below` | **demote** | flagged cold, score written back — not deleted |
| `< evict_below` | **evict** | deleted |

Two decisions in that model are worth spelling out, because they are what stop
the sweep from deleting the wrong things.

**Idle time is measured from last access, not creation.** A two-year-old note
read yesterday is hot; a note written last month that nothing ever retrieved is
cold. Age alone is a bad proxy for worth.

**Usage extends the half-life rather than adding to the score.** A memory the
agent leans on constantly decays slowly, instead of getting a one-time bonus
that a long dormancy erases.

### Demotion is the interesting outcome

Eviction is the blunt instrument; demotion is what usually should happen. A
demoted record stays in the store with `decay_cold: true` and its
`decay_strength` written back, so your retrieval layer can down-rank it instead
of losing it:

```python
results = collection.query(
    query_texts=[question],
    where={"decay_cold": False},   # or rank by decay_strength
)
```

If a demoted memory gets retrieved again, the next sweep clears the cold flag.
Records come back.

## Safety

This library deletes data, so the defaults lean toward doing nothing.

| guard | default | effect |
| --- | --- | --- |
| `dry_run` | `True` | `sweep()` never writes unless asked |
| `pinned` metadata | honored | pinned records are always kept |
| `grace_period` | 1 day | new records are never touched |
| `never_evict_above_accesses` | 5 | well-used records are demoted, never deleted |
| `max_evictions` | `None` | refuse to apply a sweep that deletes too much |
| unreadable timestamps | `skip` | never evict what you cannot measure |

`never_evict_above_accesses` exists because exponential decay is unforgiving:
usage stretches the half-life but cannot defeat the curve, so a memory retrieved
ninety times still dies after enough dormancy. That is the wrong failure
direction for a fact like a user's name. Above this many accesses, a record can
be demoted but not deleted. Set it to `None` for pure decay.

`SKIP` is deliberately distinct from `KEEP`. A record whose timestamps the field
map could not read is not judged at all — it is reported, never deleted. A
growing skip count means the field map does not match your schema:

```python
for verdict in report.skipped:
    print(verdict.record_id, verdict.reason)
```

## Tuning

`half_life` is the one knob worth setting per store. It is the idle time after
which an unused memory reaches strength 0.5.

```python
from datetime import timedelta
from decay_engine import DecayEngine, DecayPolicy

engine = DecayEngine(
    adapter,
    DecayPolicy(
        half_life=timedelta(days=60),   # slower forgetting
        demote_below=0.4,
        evict_below=0.05,
        usage_boost=1.0,                # reward usage harder
    ),
    max_evictions=500,
)
```

Tune against real data without risking it: a dry run reports the full eviction
list even when it exceeds `max_evictions`, so you can see what a given half-life
would have destroyed before you trust it.

## Fitting your schema

Decay needs to know when a record was created, when it was last read, and how
often. `FieldMap` says where those live, and the defaults already cover the
common spellings (`created_at`, `createdAt`, `timestamp`, `last_accessed_at`,
`accessed_at`, `access_count`, `hits`, …). Timestamps may be `datetime` objects,
ISO-8601 strings, or epoch seconds or milliseconds.

```python
from decay_engine import DecayPolicy, FieldMap

policy = DecayPolicy(
    field_map=FieldMap(
        created_at="inserted_at",
        last_accessed_at=("read_at", "touched_at"),  # tried in order
        access_count="retrieval_count",
        pinned="is_permanent",
    )
)
```

If your store does not track reads at all, decay falls back to creation time and
still works — it just becomes pure aging. Recording an access counter is what
makes the difference between a filing system and a shredder, and maintaining it
is your application's job: see
[docs/access-tracking.md](docs/access-tracking.md).

Imported or backfilled records need care, because remapping `created_at` alone is
not enough to make them look new —
[docs/timestamps-and-grace-period.md](docs/timestamps-and-grace-period.md)
explains why and what to do instead.

## Adapters

Two are shipped. Both take the store handle you already have and never open,
close, or pool connections themselves.

```python
from decay_engine.adapters.chroma import ChromaAdapter
from decay_engine.adapters.pgvector import PgVectorAdapter

ChromaAdapter(collection)                      # a chromadb collection

PgVectorAdapter(conn, table="memories")        # metadata in a jsonb column
PgVectorAdapter(                               # or as real Postgres columns
    conn,
    table="memories",
    extra_columns=("created_at", "last_accessed_at", "access_count"),
)
```

pgvector imposes no schema — it is a column type — so the adapter takes your
layout as configuration. Timing signals work whether they live as keys in a
`jsonb` column or as first-class `timestamptz` and `int` columns named in
`extra_columns`. Iteration pages by keyset (`WHERE id > last_seen`) rather than
`OFFSET`, so rows are neither skipped nor repeated if the table shifts during a
long sweep, and metadata patches merge inside Postgres (`metadata || patch`) so
concurrent writers touching other keys are not clobbered.

Decay's own writes (`decay_cold`, `decay_strength`, `decay_swept_at`) land in the
`jsonb` column, so a store that wants demotion needs one.

### Supporting another store

Implement three methods:

```python
class MyStore:
    def iter_records(self, *, batch_size=256):
        """Yield every MemoryRecord. Page; do not materialize the store."""

    def update_metadata(self, updates):
        """Merge {id: patch} into existing metadata. Never drop unmentioned keys."""

    def delete(self, ids):
        """Remove these ids."""
```

`decay_engine.adapters.memory.InMemoryStore` is a complete, compact reference
implementation, and useful on its own for trying policies on sample data before
pointing them at production.

## Status

Early. Time-and-usage decay is implemented and tested; near-duplicate merging,
consolidation, and contradiction resolution are not.

Chroma and pgvector adapters ship. See [STATUS.md](STATUS.md) for the full
inventory.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,chroma,pgvector]"
.venv/bin/python -m pytest
```

The suite runs with no database and no `chromadb` installed. Both adapters are
tested against doubles that reproduce the real backends' awkward parts — Chroma
replacing metadata on update and rejecting non-scalars, Postgres' keyset paging
and `jsonb ||` merge — plus live round-trip tests that skip when the real thing
is unavailable.

To run the live tests, point them at real backends:

```bash
# Chroma: just install it
.venv/bin/pip install chromadb

# Postgres: anything with the vector extension
docker compose up -d

DECAY_ENGINE_PG_DSN="postgresql://postgres:decay@localhost:55432/decay" \
  .venv/bin/python -m pytest
```
