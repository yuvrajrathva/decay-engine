# Timestamps, imports, and the grace period

Decay needs to know *when a memory last mattered*. This document explains which
timestamps the library reads, how you point it at your own columns, what happens
to records imported from somewhere else, and exactly what the grace period
protects.

The short version: **you decide which timestamp means "created".** The library
has no opinion beyond the defaults, and the one thing worth getting right is
that imported records need their *access* timestamp remapped too, not just their
creation timestamp.

## The two timestamps decay reads

| Signal | Default metadata keys | What it is for |
| --- | --- | --- |
| `created_at` | `created_at`, `createdAt`, `created`, `timestamp`, `ts` | The grace period, and the fallback for scoring. |
| `last_accessed_at` | `last_accessed_at`, `lastAccessedAt`, `last_accessed`, `accessed_at`, `last_used_at` | Scoring. This is the one that drives decay. |

Keys are tried in the order listed, and the **first present, non-null** one wins.
So a store that already writes `createdAt` needs no configuration at all.

### Which one actually drives the score

`last_accessed_at` if it exists, otherwise `created_at`. The library calls that
the record's **reference time**:

```
reference_time = last_accessed_at or created_at
idle_time      = now - reference_time
strength       = 0.5 ** (idle_time / effective_half_life)
```

Anchoring on last access rather than creation is deliberate. A two-year-old note
read yesterday is hot; a note written last month that nothing ever retrieved is
cold. Age alone is a bad proxy for worth.

**If a store tracks no access times at all**, `created_at` carries the whole
score and decay degrades gracefully into pure aging. It still works — it just
cannot tell a well-used memory from an ignored one. See
[access-tracking.md](access-tracking.md).

**If neither timestamp is readable**, the record is not judged. It gets the
`SKIP` action, is reported, and is never deleted. The library does not delete
what it cannot measure.

## Accepted formats

Any of these parse, in either field:

| Form | Example |
| --- | --- |
| `datetime` (aware or naive) | `datetime(2026, 1, 1, tzinfo=timezone.utc)` |
| ISO 8601 string | `"2026-01-01T00:00:00+00:00"`, `"2026-01-01T00:00:00Z"` |
| Epoch seconds | `1767225600`, `"1767225600"` |
| Epoch milliseconds | `1767225600000` |

Naive datetimes are assumed UTC. Epoch values above `1e11` are read as
milliseconds, below as seconds. Anything unparseable — `"yesterday"`, `None`,
`""`, a malformed date — yields no signal rather than a guess, which means the
record is skipped, not evicted.

## Pointing decay at your own columns

`FieldMap` is the whole mechanism. Pass one key or several candidates:

```python
from decay_engine import DecayEngine, DecayPolicy, FieldMap

policy = DecayPolicy(
    field_map=FieldMap(
        created_at="inserted_at",
        last_accessed_at=("read_at", "touched_at"),  # tried in order
        access_count="retrieval_count",
        pinned="is_permanent",
    )
)
engine = DecayEngine(adapter, policy)
```

**You choose what "created" means.** If your schema distinguishes when a fact
became true from when the row was written, point `created_at` at whichever one
should govern the grace period. The library does not care which; it only reads
the key you name.

With the pgvector adapter, timing signals kept as real Postgres columns rather
than JSONB keys work the same way — name them in `extra_columns` and they are
surfaced into each record's metadata, where `FieldMap` reads them normally:

```python
PgVectorAdapter(
    conn,
    table="memories",
    extra_columns=("created_at", "last_accessed_at", "access_count"),
)
```

A real column takes precedence over a same-named JSONB key, since naming it is
an explicit instruction.

## The grace period

`grace_period` (default: 1 day) makes recently created records untouchable
whatever their score:

```python
DecayPolicy(grace_period=timedelta(days=7))
```

```
if created_at is not None and (now - created_at) < grace_period:
    keep, with reason "within grace period"
```

It exists for a specific hazard: a sweep reads the store, then writes to it. Any
memory written in between — or written by another process mid-sweep — would
otherwise be judged on an access history it has not had time to accumulate. The
grace period makes new memories structurally safe rather than relying on timing.

Two consequences worth knowing:

**The grace period is checked against `created_at` only.** A record created
within the window is kept even if its `last_accessed_at` is ancient. That is
what makes the import pattern below work.

**No `created_at` means no grace period.** If a record exposes only
`last_accessed_at`, it is scored normally and the grace period silently does not
apply. There is no error — the protection just is not there. If you rely on the
grace period, make sure `created_at` is mapped and populated.

## Imported and historical records

This is where the two timestamps interact in a way that surprises people.

Say you import a year of memories from another system. Each carries its original
timestamps — created 400 days ago, last read 380 days ago. On the next sweep,
every one of them is cold enough to evict, because that is what their history
says. Sometimes that is correct. Often it is not: the records are new *to this
store*, and nothing has had a chance to read them yet.

### The trap: remapping `created_at` alone is not enough

An obvious first attempt is to set `created_at` to the import time and keep the
original access times:

```python
{"created_at": "<import time>", "last_accessed_at": "<original, 400 days ago>"}
```

During the grace period this works — the record is kept. The moment the grace
period lapses, scoring falls back to `last_accessed_at`, which is still 400 days
old, and the record is evicted on the very next sweep. The grace period only
deferred the problem by a day.

### Option 1 — treat imported records as new

Set **both** timestamps to the import time. This is what "treat them as newly
created" actually requires:

```python
import_time = datetime.now(timezone.utc).isoformat()
for memory in imported:
    memory.metadata["created_at"] = import_time
    memory.metadata["last_accessed_at"] = import_time
```

Every imported record now starts at full strength and decays from the import
forward. You lose the original history, so if you want to keep it, store it
under different keys (`original_created_at`) that decay does not read.

### Option 2 — keep the real history, map a separate field

Preserve the originals and point `FieldMap` at import-relative fields instead:

```python
policy = DecayPolicy(
    field_map=FieldMap(
        created_at="imported_at",
        last_accessed_at=("accessed_since_import", "imported_at"),
    )
)
```

The candidate list is doing the work in that second line: a record read since
the import scores on that read, and one that has not been read yet falls back to
the import time rather than its ancient original. Original timestamps stay in
the row for your own queries, untouched.

### Option 3 — pin what must not be lost, then let the rest decay

If the import contains facts that should never be evicted regardless of use, the
honest answer is not a timestamp trick:

```python
memory.metadata["pinned"] = True
```

Pinned records are always kept. `pinned`, `pin`, `protected`, and `permanent` are
all recognized by default, and string values (`"true"`, `"yes"`, `"1"`, `"on"`)
are understood because some stores coerce booleans to text.

### Option 4 — a long grace period for the import window

A grace period longer than your import's settling time gives retrieval a chance
to touch the records before any of them can be evicted:

```python
DecayPolicy(grace_period=timedelta(days=30))
```

This only helps if `created_at` reflects the import rather than the original
creation, so it composes with Option 1 or 2 — not with doing nothing.

## Verifying before you trust it

Dry run is the default, and it reports the full eviction list even when it
exceeds `max_evictions`. After an import, look before you leap:

```python
report = engine.sweep()          # dry run: changes nothing
print(report.summary())
print(report.evicted_ids)        # populated on dry runs too
print([v.record_id for v in report.skipped])   # field map mismatches
```

A large `skipped` count right after an import usually means the imported records
use timestamp keys your `FieldMap` does not list.

## See also

- [access-tracking.md](access-tracking.md) — the fields your application must
  maintain, and what decay does without them.
- [../README.md](../README.md) — scoring model, thresholds, and safety rails.
