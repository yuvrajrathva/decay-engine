"""Core data types: the record shape and the metadata field mapping.

The library deliberately does not own your schema. A store hands us an id, an
optional text body, and whatever metadata dict it already keeps; a `FieldMap`
tells us which of those keys carry the timing signals decay needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

__all__ = ["MemoryRecord", "MemorySignals", "FieldMap", "coerce_timestamp", "utcnow"]

FieldSpec = Union[str, Sequence[str], None]

# Epoch values above this are assumed to be milliseconds, not seconds.
# 1e11 seconds is the year 5138, so no realistic second-precision timestamp
# reaches it, while any millisecond timestamp after 1973 exceeds it.
_MILLISECOND_CUTOFF = 1e11


def utcnow() -> datetime:
    """Timezone-aware current time. Used everywhere so sweeps are reproducible."""
    return datetime.now(timezone.utc)


def coerce_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort parse of the timestamp shapes vector stores actually hold.

    Accepts datetimes, epoch seconds or milliseconds (int/float, or their string
    forms), and ISO-8601 strings including a trailing ``Z``. Returns ``None`` for
    anything unparseable rather than guessing, so the caller can decline to act
    on a record it cannot measure.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds != seconds:  # NaN
            return None
        if abs(seconds) > _MILLISECOND_CUTOFF:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return coerce_timestamp(float(raw))
        except ValueError:
            pass
        candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return None


def _coerce_count(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "pinned"}
    return bool(value)


@dataclass
class MemoryRecord:
    """One row of an agent's memory, as the adapter found it."""

    id: str
    text: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def get(self, keys: Sequence[str]) -> Any:
        """First present, non-null value among ``keys``."""
        for key in keys:
            if key in self.metadata and self.metadata[key] is not None:
                return self.metadata[key]
        return None


@dataclass(frozen=True)
class MemorySignals:
    """Normalized timing signals extracted from a record's metadata."""

    created_at: Optional[datetime]
    last_accessed_at: Optional[datetime]
    access_count: float
    pinned: bool

    @property
    def reference_time(self) -> Optional[datetime]:
        """The instant a memory was last known to matter.

        Last access if the store tracks it, otherwise creation. ``None`` means
        the store gave us no usable timing signal at all.
        """
        return self.last_accessed_at or self.created_at


def _normalize(spec: FieldSpec) -> Tuple[str, ...]:
    if spec is None:
        return ()
    if isinstance(spec, str):
        return (spec,)
    return tuple(spec)


@dataclass(frozen=True)
class FieldMap:
    """Which metadata keys hold the signals decay reads.

    Each field accepts a single key or several candidates tried in order, so the
    defaults already fit the common naming conventions without configuration.
    """

    created_at: FieldSpec = ("created_at", "createdAt", "created", "timestamp", "ts")
    last_accessed_at: FieldSpec = (
        "last_accessed_at",
        "lastAccessedAt",
        "last_accessed",
        "accessed_at",
        "last_used_at",
    )
    access_count: FieldSpec = ("access_count", "accessCount", "hits", "use_count")
    pinned: FieldSpec = ("pinned", "pin", "protected", "permanent")

    def read(self, record: MemoryRecord) -> MemorySignals:
        created = coerce_timestamp(record.get(_normalize(self.created_at)))
        accessed = coerce_timestamp(record.get(_normalize(self.last_accessed_at)))
        count = _coerce_count(record.get(_normalize(self.access_count)))
        pinned_raw = record.get(_normalize(self.pinned))
        return MemorySignals(
            created_at=created,
            last_accessed_at=accessed,
            access_count=max(0.0, count) if count is not None else 0.0,
            pinned=_coerce_flag(pinned_raw) if pinned_raw is not None else False,
        )


def signals_to_metadata(signals: MemorySignals) -> Dict[str, Any]:
    """Debug helper: the signals as a flat dict, for logging a sweep decision."""
    return {
        "created_at": signals.created_at.isoformat() if signals.created_at else None,
        "last_accessed_at": (
            signals.last_accessed_at.isoformat() if signals.last_accessed_at else None
        ),
        "access_count": signals.access_count,
        "pinned": signals.pinned,
    }
