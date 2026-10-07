# Access tracking is your application's job

**decay-engine does not track memory access.** It reads access signals; it never
writes them. Nothing in this library hooks your retrieval path, wraps your
query calls, or increments a counter when a memory is used.

That is a deliberate boundary — the library has no way to know what counts as
"using" a memory in your agent — but it has a consequence worth being blunt
about: **if your application never records retrievals, usage-aware decay does not
work.** It degrades to pure aging, and a memory your agent depends on daily
looks identical to one nothing has ever read.

## The two fields you maintain

| Field | Default keys read | Type | Who writes it |
| --- | --- | --- | --- |
| `last_accessed_at` | `last_accessed_at`, `lastAccessedAt`, `last_accessed`, `accessed_at`, `last_used_at` | timestamp | **You** |
| `access_count` | `access_count`, `accessCount`, `hits`, `use_count` | number | **You** |

Both are read through `FieldMap`, so you can point them at any key or column you
already have — see
[timestamps-and-grace-period.md](timestamps-and-grace-period.md).

For completeness, these are the other fields in the contract:

| Field | Who writes it |
| --- | --- |
| `created_at` | You, when the memory is first stored |
| `pinned` | You, for memories that must never be evicted |
| `decay_cold`, `decay_strength`, `decay_swept_at` | **The library**, during an applied sweep |

## When to update them

Update both **whenever a memory is actually used by the agent** — not when it is
merely returned by a query.

That distinction matters. A vector search returning the top 20 candidates has not
used 20 memories; it has used however many the agent actually read into context
and acted on. Counting every candidate inflates every counter roughly equally,
which destroys the signal — if everything is hot, nothing is.

A reasonable rule: increment when a memory makes it into the prompt.

```python
from datetime import datetime, timezone

def record_access(store, memory_ids):
    """Call this after deciding which retrieved memories to actually use."""
    now = datetime.now(timezone.utc).isoformat()
    store.update_metadata({
        memory_id: {"last_accessed_at": now}      # access_count: see below
        for memory_id in memory_ids
    })
```

Incrementing a counter needs read-modify-write or an atomic operation, which is
store-specific. In Postgres it is one statement:

```sql
UPDATE memories
   SET last_accessed_at = now(),
       access_count     = access_count + 1
 WHERE id = ANY(%s);
```

In Chroma, there is no atomic increment, so read the current value and write it
back — accepting that a lost update under concurrency costs you one count, which
decay does not care about:

```python
current = collection.get(ids=memory_ids, include=["metadatas"])
collection.update(
    ids=current["ids"],
    metadatas=[
        {**metadata,
         "last_accessed_at": now,
         "access_count": (metadata.get("access_count") or 0) + 1}
        for metadata in current["metadatas"]
    ],
)
```

Note that `update_metadata` on a `StoreAdapter` merges rather than replaces, so
if you already hold an adapter you can reuse it for the timestamp half and keep
your own increment logic for the counter.

### Cost, and how to cut it

A write on every retrieval is real overhead. Two ways to reduce it without
breaking decay:

- **Coalesce the timestamp.** Skip the write if `last_accessed_at` is already
  within the last hour. Decay operates on a scale of days; hourly resolution
  changes nothing.
- **Batch the counter.** Buffer increments in memory and flush periodically. An
  access count that lags by a few minutes is harmless — it is a relative
  popularity signal, not an audit log.

What you should *not* do is write only the timestamp and never the counter. That
leaves `access_count` at zero forever, which silently disables both usage-aware
decay and the tenure protection described below.

## How `access_count` affects decay

Usage **extends the half-life** rather than adding to the score:

```
effective_half_life = half_life * (1 + usage_boost * ln(1 + access_count))
strength            = 0.5 ** (idle_time / effective_half_life)
```

The multiplier is capped by `max_usage_multiplier` (default 10) so a runaway
counter cannot make a record immortal.

With the default `half_life=30 days` and `usage_boost=0.5`:

| `access_count` | effective half-life | strength after 200 days idle |
| --- | --- | --- |
| 0 | 30.0 days | 0.0098 |
| 5 | 56.9 days | 0.0874 |
| 20 | 75.7 days | 0.1601 |
| 50 | 89.0 days | 0.2106 |
| 500 | 123.2 days | 0.3247 |

The returns are logarithmic on purpose. The difference between 0 and 20 accesses
is meaningful; between 500 and 5000 it is noise.

Extending the half-life, rather than adding a flat bonus, is what stops a
long-dormant popular memory from being judged purely on its age. But note what
the table shows: usage slows decay, it cannot stop it. Every strength in that
column keeps falling. Which is why there is a second, harder protection:

### Tenure: the floor usage buys you

`never_evict_above_accesses` (default: **5**) makes a record demote-only,
however cold it gets:

```python
DecayPolicy(never_evict_above_accesses=5)    # default
DecayPolicy(never_evict_above_accesses=None) # pure decay, no tenure
```

At or above that many accesses, a record below `evict_below` is **demoted
instead of deleted**. This exists because exponential decay is unforgiving: a
memory retrieved ninety times still dies after enough dormancy, which is the
wrong failure direction for something like a user's name.

This is the single strongest reason to maintain `access_count`. Without it, every
record sits at zero, nothing ever earns tenure, and the protection is dead code
in your deployment.

## What happens when these fields are missing

Nothing errors. Decay degrades, in specific ways:

| Missing | Effect |
| --- | --- |
| `access_count` | Treated as `0`. No half-life extension, and no record ever earns tenure — so eviction is governed entirely by idle time. |
| `last_accessed_at` | Scoring falls back to `created_at`. Decay becomes pure aging: a frequently-read old memory is indistinguishable from an abandoned one. |
| **both** `last_accessed_at` **and** `created_at` | The record gets `SKIP`. It is reported and **never deleted** — the library does not delete what it cannot measure. |
| `created_at` (alone) | Scoring still works off `last_accessed_at`, but **the grace period silently does not apply**, because it is checked against `created_at`. |
| `pinned` | Treated as not pinned. |

Unparseable values behave as missing, not as zero or as the epoch. `"yesterday"`,
`""`, and a malformed date all yield no signal, so the record is skipped rather
than evicted.

### Catching it in practice

A growing `SKIP` count is the symptom to watch — it almost always means the
`FieldMap` does not match your schema:

```python
report = engine.sweep()          # dry run
for verdict in report.skipped:
    print(verdict.record_id, verdict.reason)
```

A silent `access_count` of zero across the board is the subtler failure, because
nothing reports it. A quick audit:

```python
from decay_engine import FieldMap

counts = [
    FieldMap().read(record).access_count
    for record in adapter.iter_records()
]
print(f"{sum(1 for c in counts if c == 0)} of {len(counts)} records have never been accessed")
```

If that number is 100%, either the store is genuinely untouched or — far more
likely — nothing is incrementing the counter.

## Checklist

- [ ] Write `created_at` when a memory is first stored.
- [ ] Write `last_accessed_at` when a memory is actually used in a prompt.
- [ ] Increment `access_count` at the same moment.
- [ ] Set `pinned` on memories that must never be evicted.
- [ ] Point `FieldMap` at your keys if they differ from the defaults.
- [ ] Dry-run a sweep and check `report.skipped` is empty.

## See also

- [timestamps-and-grace-period.md](timestamps-and-grace-period.md) — timestamp
  formats, `FieldMap` wiring, imports, and the grace period.
- [../README.md](../README.md) — scoring model, thresholds, and safety rails.
