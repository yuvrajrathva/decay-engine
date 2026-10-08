"""Sweep two tenants on different retention policies, without touching each other.

    python examples/scoped_sweep.py

Uses the in-memory store so it runs with no database. The only thing that changes
for Chroma or pgvector is the scope's shape -- see docs/scoped-sweeps.md.
"""

from datetime import timedelta

from decay_engine import DecayEngine, DecayPolicy, MemoryRecord, utcnow
from decay_engine.adapters.memory import InMemoryStore

NOW = utcnow()


def memory(record_id, text, idle_days, tenant, access_count=0):
    return MemoryRecord(
        id=record_id,
        text=text,
        metadata={
            "tenant_id": tenant,
            "created_at": (NOW - timedelta(days=idle_days + 30)).isoformat(),
            "last_accessed_at": (NOW - timedelta(days=idle_days)).isoformat(),
            "access_count": access_count,
        },
    )


store = InMemoryStore(
    [
        # acme is on a generous plan
        memory("acme-1", "prefers metric units", idle_days=3, tenant="acme", access_count=9),
        memory("acme-2", "asked about SSO once", idle_days=100, tenant="acme"),
        memory("acme-3", "debug trace 9f21", idle_days=240, tenant="acme"),
        # globex is a trial account that should forget quickly
        memory("globex-1", "prefers dark mode", idle_days=3, tenant="globex", access_count=4),
        memory("globex-2", "asked about pricing", idle_days=40, tenant="globex"),
        memory("globex-3", "scratch note", idle_days=90, tenant="globex"),
    ]
)

RETENTION = {
    "acme": DecayPolicy(half_life=timedelta(days=90), evict_below=0.1),
    "globex": DecayPolicy(half_life=timedelta(days=14), evict_below=0.1),
}

print(f"store holds {len(store)} memories across 2 tenants\n")

for tenant, policy in RETENTION.items():
    engine = DecayEngine(store, policy, max_evictions=10)
    scope = {"tenant_id": tenant}

    preview = engine.sweep(scope=scope)  # dry run, scoped
    half_life = policy.half_life.days
    print(f"{tenant} (half-life {half_life}d): {preview.summary()}")
    for verdict in sorted(preview.verdicts, key=lambda v: -(v.strength or 0)):
        strength = "—" if verdict.strength is None else f"{verdict.strength:.4f}"
        print(f"    {verdict.record_id:<10}{verdict.action.value:<8}{strength}")

    engine.sweep(dry_run=False, scope=scope)
    print()

print(f"after both sweeps, {len(store)} memories remain:")
for record_id in sorted(store.ids):
    print(f"    {record_id}")
print()
print("Each sweep only ever read its own tenant's records, so the ceiling of 10")
print("evictions applied per tenant rather than across the whole store.")
