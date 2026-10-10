# A memory application on pgvector, kept clean by decay-engine

A small, runnable CLI that stores memories in Postgres with `pgvector`, searches
them by vector similarity, tracks what it retrieves, and sweeps the stale ones.
Everything runs in Docker — no Python, Postgres, or model to install.

It exists to show the division of labour, which is the part that is easy to get
wrong:

| the application does | decay-engine does |
| --- | --- |
| write memories and embed them | score them by idle time and usage |
| search, and decide what it used | decide KEEP / DEMOTE / EVICT / SKIP |
| **write `last_retrieved_at`, `retrieval_count`** | **read** them |
| **read `decay_cold`, `decay_strength`** | **write** them |

The library never records an access. Nothing in it knows what "using" a memory
means in your agent, so `memory_store.record_access()` here is application code.

## Run it

From this directory:

```bash
docker compose run --rm app demo
```

That builds the image, starts Postgres with `pgvector`, creates the schema,
seeds eight memories, and walks the whole story — ending in 19 `ok` lines,
because the demo asserts its own outcomes. `docker compose up --build` does the
same thing if you prefer watching it as a stack.

No `.env` is needed; every compose value has a default. Copy `.env.example` to
`.env` to change the database name, credentials, or published port.

When you are done:

```bash
docker compose down -v        # -v also drops the database volume
```

## What the demo shows

1. **Seed** — eight memories, chosen so every outcome is reachable.
2. **Retrieve and record** — a search is *not* an access. The app records only
   the hit it actually used, incrementing `retrieval_count`.
3. **Dry-run sweep** — all four outcomes, nothing written.
4. **Applied sweep** — the same decisions, carried out.
5. **Retrieval after demotion** — the three ways cold memories can be treated.
6. **Promotion** — a cold memory that gets used again loses its cold flag.

## Driving it yourself

Any subcommand works the same way. `docker compose run --rm app --help` lists
them all.

```bash
# the store, and each memory's decay state
docker compose run --rm app list

# preview a sweep: decides everything, changes nothing
docker compose run --rm app sweep

# then actually do it
docker compose run --rm app sweep --apply

# search; records an access for the hits, like a real agent would
docker compose run --rm app search "what units does the user prefer?"

# search without recording an access
docker compose run --rm app search "SSO pricing" --no-track

# how should demoted memories affect retrieval? three answers:
docker compose run --rm app search "SSO pricing" --cold-mode ignore
docker compose run --rm app search "SSO pricing" --cold-mode exclude
docker compose run --rm app search "SSO pricing" --cold-mode penalize --penalty 5

# store something new, protect something important, start over
docker compose run --rm app remember my-note "The user's dog is called Biscuit."
docker compose run --rm app pin my-note
docker compose run --rm app seed          # back to the sample set
```

Inspect the data directly — the published port defaults to `55433`:

```bash
docker compose exec postgres psql -U decay -d decay_demo \
  -c "SELECT id, retrieval_count, last_retrieved_at, metadata FROM memories;"
```

## The four outcomes

The sample data is built so each one happens for a visible reason.

| memory | idle | reads | outcome | why |
| --- | --- | --- | --- | --- |
| `pref-units` | 2d | 12 | **keep** | recently used |
| `pref-dark-mode` | 10d | 3 | **keep** | still strong |
| `contact-email` | 500d | 0 | **keep** | `pinned` — never touched |
| `sso-pricing` | 60d | 1 | **demote** | cooling; flagged cold, kept |
| `name-timezone` | 500d | 90 | **demote** | cold enough to evict, but 90 reads earn tenure |
| `debug-trace` | 240d | 0 | **evict** | never reused |
| `scratch-note` | 320d | 2 | **evict** | barely used, long idle |
| `legacy-import` | — | 0 | **skip** | no timestamps, so unmeasurable |

Two of these are the point of the whole example.

**`skip` is not `keep`.** `legacy-import` has no `created_at` and no
`last_retrieved_at`, so there is nothing to score it on. It is reported and left
alone. A library that guessed here would delete records it knows nothing about.

**`demote` is not `evict`.** `name-timezone` is colder than the eviction
threshold — a user's name and timezone, unread for 500 days. But it has been
retrieved 90 times, and `never_evict_above_accesses` makes that earn tenure, so
it is flagged rather than deleted. Step 6 then shows demotion is reversible.

## How retrieval uses the decay metadata

An applied sweep writes `decay_cold`, `decay_strength`, and `decay_swept_at`
into each demoted row's `metadata`. The library stops there; using them is the
application's call. `memory_store.search()` implements three policies:

| `--cold-mode` | behavior |
| --- | --- |
| `exclude` | cold memories are filtered out of results entirely (the default here) |
| `penalize` | cold memories still match, but their distance is scaled by `1 + penalty × (1 − strength)`, so they surface only when nothing stronger is close |
| `ignore` | the decay metadata is not consulted — what retrieval looked like before sweeping |

`penalize` is usually the better behavior in a real system: a weak memory stays
reachable when it is the only thing relevant, instead of vanishing.

Note that this only works if your retrieval layer actually reads those fields.
If it ignores them, demotion is a no-op and eviction ends up doing all the work,
which changes how you would tune the thresholds.

## Configuration worth copying

### FieldMap

Two of this schema's columns are deliberately *not* named what the library would
guess. Without the mapping, every record would come back `SKIP`:

```python
FIELD_MAP = FieldMap(
    created_at="created_at",
    last_accessed_at="last_retrieved_at",   # not the default name
    access_count="retrieval_count",         # not the default name
    pinned="pinned",
)
```

### Timing signals as real columns

They are `timestamptz` and `integer` columns, not JSONB keys, which is the
idiomatic Postgres choice. `extra_columns` surfaces them into each record's
metadata, where the `FieldMap` reads them:

```python
PgVectorAdapter(
    connection,
    table="memories",
    text_column="document",
    metadata_column="metadata",     # decay's own writes land here
    extra_columns=("created_at", "last_retrieved_at", "retrieval_count", "pinned"),
)
```

Decay's writes always go to the `jsonb` column, which is why the table has both.

### Policy

```python
DecayPolicy(
    half_life=timedelta(days=30),     # idle time at which an unused memory halves
    usage_boost=0.5,                  # retrievals stretch that half-life
    demote_below=0.5,                 # under this strength: flag cold
    evict_below=0.05,                 # under this strength: delete
    never_evict_above_accesses=5,     # well-used memories demote, never delete
    grace_period=timedelta(days=1),   # brand-new memories are untouchable
    field_map=FIELD_MAP,
)
```

The engine also takes `max_evictions=100` here, so a misconfigured half-life
cannot quietly wipe the store — an applied sweep over that ceiling raises
instead.

## Files

| file | what it is |
| --- | --- |
| `app.py` | the CLI, the decay configuration, and the guided `demo` |
| `memory_store.py` | the application's own store: writes, search, **access tracking** |
| `embedding.py` | a deterministic hashing embedder, so no model is needed |
| `sample_data.py` | the eight sample memories and their expected outcomes |
| `init.sql` | schema, run automatically on the database's first boot |
| `docker-compose.yml` | Postgres + pgvector, plus the app container |
| `Dockerfile` | installs the local library from source, then the example |

The embedder is a hash of the words in a string. It is reproducible and needs no
network, which is all the demo requires; it has no real semantics. Swapping in a
real model changes nothing else here.

## Notes

This stack is separate from the repository-root `docker-compose.yml`, which is
the development database for the library's test suite. Different container
names, volume, and host port, so running this cannot disturb that.

The image installs the library from the working tree, not from an index, so the
example always exercises your local `decay_engine`.

## See also

- [../../README.md](../../README.md) — the library itself
- [../../docs/access-tracking.md](../../docs/access-tracking.md) — the fields your app must maintain, and what breaks without them
- [../../docs/timestamps-and-grace-period.md](../../docs/timestamps-and-grace-period.md) — timestamp mapping, imports, grace period
- [../../docs/scoped-sweeps.md](../../docs/scoped-sweeps.md) — sweeping one tenant at a time
- `../preview_sweep.py`, `../scoped_sweep.py` — smaller examples with no database
