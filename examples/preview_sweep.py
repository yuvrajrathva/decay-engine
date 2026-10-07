"""Preview a decay sweep over a synthetic store, without touching real data.

    python examples/preview_sweep.py
"""

from datetime import timedelta

from decay_engine import DecayEngine, DecayPolicy, MemoryRecord, utcnow
from decay_engine.adapters.memory import InMemoryStore

NOW = utcnow()


def memory(record_id, text, idle_days, access_count=0, **extra):
    return MemoryRecord(
        id=record_id,
        text=text,
        metadata={
            "created_at": (NOW - timedelta(days=idle_days + 30)).isoformat(),
            "last_accessed_at": (NOW - timedelta(days=idle_days)).isoformat(),
            "access_count": access_count,
            **extra,
        },
    )


store = InMemoryStore(
    [
        memory("m1", "user prefers dark mode", idle_days=2, access_count=14),
        memory("m2", "user's manager is on the platform team", idle_days=45, access_count=3),
        memory("m3", "user asked about pricing tiers once", idle_days=120),
        memory("m4", "temporary: debugging session id 4f2a", idle_days=210),
        memory("m5", "user's name and timezone", idle_days=400, access_count=90),
        memory("m6", "user's primary email", idle_days=500, pinned=True),
        MemoryRecord(id="m7", text="imported note, no timestamps", metadata={}),
    ]
)

engine = DecayEngine(
    store,
    DecayPolicy(half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1),
    max_evictions=100,
)

report = engine.sweep()  # dry run

print(report.summary())
print()
print(f"{'id':<5}{'action':<9}{'strength':<10}{'half-life':<12}reason")
print("-" * 76)
for verdict in sorted(report.verdicts, key=lambda v: -(v.strength or 0)):
    strength = "—" if verdict.strength is None else f"{verdict.strength:.4f}"
    half_life = f"{verdict.annotations['effective_half_life_days']:.0f}d"
    print(f"{verdict.record_id:<5}{verdict.action.value:<9}{strength:<10}{half_life:<12}{verdict.reason}")

print()
print("Note m5: 400 days idle but heavily used, so its half-life stretched and it")
print("survives as cold rather than being deleted. m4 was never used and goes.")
print()
print(f"nothing was changed — store still holds {len(store)} records")
print("call engine.sweep(dry_run=False) to apply")
