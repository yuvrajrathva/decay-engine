from datetime import datetime, timedelta, timezone

import pytest

from decay_engine.models import MemoryRecord

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now():
    return NOW


def record(
    record_id: str,
    *,
    idle_days: float = 0.0,
    age_days: float = None,
    access_count: int = 0,
    pinned: bool = None,
    extra: dict = None,
    now: datetime = NOW,
) -> MemoryRecord:
    """Build a record positioned relative to `now`.

    `idle_days` is time since last access; `age_days` defaults to just older than
    the idle window so the grace period does not accidentally apply.
    """
    if age_days is None:
        age_days = idle_days + 30
    metadata = {
        "created_at": (now - timedelta(days=age_days)).isoformat(),
        "last_accessed_at": (now - timedelta(days=idle_days)).isoformat(),
        "access_count": access_count,
    }
    if pinned is not None:
        metadata["pinned"] = pinned
    if extra:
        metadata.update(extra)
    return MemoryRecord(id=record_id, text=f"memory {record_id}", metadata=metadata)
