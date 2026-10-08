# Scoped sweeps

By default a sweep walks the whole store. A **scope** narrows it to a subset, so
you can sweep one tenant, one user's memories, one collection of imported
records, or anything else your backend can filter on.

```python
engine.sweep(dry_run=False, scope={"tenant_id": "acme"})
```

Records outside the scope are not read, not judged, and not touched. They do not
appear in the report at all.

## The core knows nothing about your partitioning

`decay-engine` has no concept of a tenant, a user, or a namespace, and it never
inspects a scope. The engine takes whatever object you pass and hands it to the
adapter unchanged; the adapter translates it into something its backend
understands. That is the whole design:

```
engine.sweep(scope=X)
   └── adapter.iter_records(batch_size=..., scope=X)
          └── Chroma:   collection.get(where=X, ...)
              Postgres: SELECT ... WHERE (X) AND id > last_seen ...
```

So the shapes a scope can take are defined by the adapter, not the library. A
scope can be a dict, a dataclass, a callable, or anything else an adapter cares
to accept.

## What each adapter accepts

### Chroma

A mapping becomes Chroma's `where` metadata filter, so its full operator syntax
works:

```python
engine.sweep(scope={"tenant_id": "acme"})
engine.sweep(scope={"tenant_id": {"$in": ["acme", "globex"]}})
engine.sweep(scope={"kind": {"$ne": "scratch"}})
```

For a document filter as well, use `ChromaScope`:

```python
from decay_engine.adapters.chroma import ChromaScope

engine.sweep(scope=ChromaScope(
    where={"tenant_id": "acme"},
    where_document={"$contains": "invoice"},
))
```

Paging then walks the filtered set rather than the whole collection.

### pgvector

A mapping is matched against the JSONB metadata column using containment
(`metadata @> '{"tenant_id": "acme"}'`), which is indexable with a GIN index:

```python
engine.sweep(scope={"tenant_id": "acme"})
```

For real columns, or any predicate SQL can express, use `PgVectorScope`. Values
go in `params` and are passed as query parameters, never interpolated:

```python
from decay_engine.adapters.pgvector import PgVectorScope

# equality on real columns, with the column names validated for you
engine.sweep(scope=PgVectorScope.for_columns({"tenant_id": "acme"}))

# anything else
engine.sweep(scope=PgVectorScope(
    "tenant_id = %s AND NOT archived AND access_count < %s",
    ("acme", 10),
))
```

The fragment is ANDed with the keyset paging condition, so paging stays correct
within the filtered set. `PgVectorScope` refuses an empty predicate or one
containing `;`.

`count()` takes a scope too, which is handy for sizing a sweep first:

```python
adapter.count()                          # whole table
adapter.count({"tenant_id": "acme"})     # just this tenant
```

### In-memory

Either a predicate or a mapping of metadata values that must all match. Useful
for trying a policy on sample data:

```python
store.iter_records(scope=lambda record: record.id.startswith("acme-"))
engine.sweep(scope={"tenant_id": "acme", "kind": "note"})
```

## A worked example: sweeping tenants on their own schedules

Different tenants can have different retention rules, and each sweep is
independently guarded:

```python
from datetime import timedelta
from decay_engine import DecayEngine, DecayPolicy
from decay_engine.adapters.pgvector import PgVectorAdapter, PgVectorScope

adapter = PgVectorAdapter(conn, table="memories")

retention = {
    "acme":   timedelta(days=90),   # generous plan
    "globex": timedelta(days=14),   # trial accounts forget fast
}

for tenant, half_life in retention.items():
    scope = PgVectorScope.for_columns({"tenant_id": tenant})
    engine = DecayEngine(
        adapter,
        DecayPolicy(half_life=half_life),
        max_evictions=500,          # per-sweep ceiling, so per tenant
    )

    preview = engine.sweep(scope=scope)
    print(f"{tenant}: {preview.summary()}")
    if preview.eviction_limit_exceeded:
        print(f"  skipping {tenant}: {len(preview.evicted_ids)} evictions is too many")
        continue

    engine.sweep(dry_run=False, scope=scope)
```

Note what `max_evictions` means here: it counts the deletions *in that scope*. A
ceiling of 500 applied per tenant is a much tighter guard than the same ceiling
applied to one sweep over everything, which is a good reason to scope sweeps even
when you do not have to.

`examples/scoped_sweep.py` is this example, runnable with no database:

```bash
python examples/scoped_sweep.py
```

## Behavior and guarantees

**No scope means the whole store.** This is the default and matches every
release before scoping existed. The engine does not even pass the keyword when
unscoped, so adapters written against the older contract keep working.

**The report describes the scope, not the store.** `scanned`, `counts`,
`evicted_ids` and the rest cover only records inside the scope.
`report.scope` records what the sweep ran under, for audit logs.

**An adapter that cannot scope raises instead of widening.** Give a scope to an
adapter whose `iter_records` has no `scope` parameter and you get
`ScopeNotSupported`, before anything is read:

```python
from decay_engine import ScopeNotSupported

try:
    engine.sweep(dry_run=False, scope={"tenant_id": "acme"})
except ScopeNotSupported:
    ...  # this adapter would have swept everything
```

Silently ignoring an unsupported scope would turn "delete acme's dead memories"
into "delete everyone's", so it is an error rather than a fallback. For the same
reason, an empty `PgVectorScope` or an empty mapping scope on pgvector is
rejected rather than treated as "match everything" — pass `None` if you really
mean the whole table.

**Writes follow the read.** Demotions and evictions act on ids the scoped read
returned, so nothing outside the scope is ever written to or deleted.

## Supporting scopes in your own adapter

Add the keyword, and translate it:

```python
def iter_records(self, *, batch_size=256, scope=None):
    if scope is None:
        ...        # must mean every record
    elif isinstance(scope, Mapping):
        ...        # your backend's filter
    else:
        raise TypeError(f"unsupported scope: {type(scope).__name__}")
```

Two rules matter. `None` must always mean the whole store, and an unrecognized
scope must raise rather than be ignored — quietly widening a scope is the one
failure here that destroys data.

## See also

- [../README.md](../README.md) — scoring model, thresholds, and safety rails.
- [timestamps-and-grace-period.md](timestamps-and-grace-period.md)
- [access-tracking.md](access-tracking.md)
