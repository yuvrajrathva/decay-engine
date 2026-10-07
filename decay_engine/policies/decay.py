"""Time-and-usage decay: the baseline filing-system behavior.

A memory's *strength* is exponential decay on the time since it last mattered,
with frequently-used memories decaying more slowly:

    effective_half_life = half_life * (1 + usage_boost * ln(1 + access_count))
    strength            = 0.5 ** (idle_time / effective_half_life)

Strength is in ``(0, 1]``. Anchoring on last access rather than creation is the
point: a two-year-old note read yesterday is hot, and a note written last month
that nothing ever retrieved is cold. Usage extending the half-life, rather than
adding to the score, is what stops a popular memory from being evicted merely
for having been written long ago.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..models import FieldMap, MemoryRecord, MemorySignals
from .base import Action, Policy, Verdict

__all__ = ["DecayPolicy"]


@dataclass
class DecayPolicy(Policy):
    """Score records by recency and usage, then keep, demote, or evict.

    Args:
        half_life: Idle time after which an never-accessed memory reaches
            strength 0.5. The single knob most worth tuning per store.
        usage_boost: How much each access slows decay. 0 disables usage
            weighting entirely, making this pure recency.
        demote_below: Strength under which a record is marked cold. Demotion is
            non-destructive; it writes the score back so your retrieval layer can
            down-rank the record.
        evict_below: Strength under which a record is deleted.
        grace_period: Newly created records are never touched, whatever their
            score. Protects memories written between a sweep's read and its write.
        respect_pinned: Honor the pinned flag from the field map.
        max_usage_multiplier: Ceiling on how far usage can stretch the half-life,
            so one runaway access counter cannot make a record immortal.
        never_evict_above_accesses: Records retrieved at least this many times are
            demoted rather than deleted, however cold they get. Usage stretches
            the half-life but cannot stop exponential decay, so without this a
            long-dormant memory the agent leaned on a hundred times is eventually
            deleted. Set to ``None`` for pure decay with no tenure.
    """

    half_life: timedelta = timedelta(days=30)
    usage_boost: float = 0.5
    demote_below: float = 0.5
    evict_below: float = 0.05
    grace_period: timedelta = timedelta(days=1)
    respect_pinned: bool = True
    max_usage_multiplier: float = 10.0
    never_evict_above_accesses: Optional[float] = 5
    field_map: FieldMap = field(default_factory=FieldMap)
    name: str = "decay"

    def __post_init__(self) -> None:
        if self.half_life.total_seconds() <= 0:
            raise ValueError("half_life must be positive")
        if self.usage_boost < 0:
            raise ValueError("usage_boost must be non-negative")
        if self.max_usage_multiplier < 1:
            raise ValueError("max_usage_multiplier must be at least 1")
        if not 0.0 <= self.evict_below <= self.demote_below <= 1.0:
            raise ValueError(
                "thresholds must satisfy 0 <= evict_below <= demote_below <= 1"
            )
        if self.grace_period.total_seconds() < 0:
            raise ValueError("grace_period must be non-negative")
        if (
            self.never_evict_above_accesses is not None
            and self.never_evict_above_accesses < 0
        ):
            raise ValueError("never_evict_above_accesses must be non-negative or None")

    # -- scoring ---------------------------------------------------------

    def effective_half_life(self, access_count: float) -> timedelta:
        """Half-life for a record with this many recorded accesses."""
        multiplier = 1.0 + self.usage_boost * math.log1p(max(0.0, access_count))
        multiplier = min(multiplier, self.max_usage_multiplier)
        return self.half_life * multiplier

    def strength(self, signals: MemorySignals, now: datetime) -> Optional[float]:
        """Strength in ``(0, 1]``, or ``None`` when the record has no timing signal."""
        reference = signals.reference_time
        if reference is None:
            return None
        idle = (now - reference).total_seconds()
        if idle <= 0:  # future timestamp or clock skew; treat as brand new
            return 1.0
        half_life = self.effective_half_life(signals.access_count).total_seconds()
        return 2.0 ** (-idle / half_life)

    # -- policy ----------------------------------------------------------

    def evaluate(self, record: MemoryRecord, *, now: datetime) -> Verdict:
        signals = self.field_map.read(record)

        if self.respect_pinned and signals.pinned:
            return self._verdict(record, Action.KEEP, "pinned", None, signals, now)

        score = self.strength(signals, now)
        if score is None:
            return self._verdict(
                record,
                Action.SKIP,
                "no readable created_at or last_accessed_at in metadata",
                None,
                signals,
                now,
            )

        created = signals.created_at
        if created is not None and now - created < self.grace_period:
            return self._verdict(
                record, Action.KEEP, "within grace period", score, signals, now
            )

        if score < self.evict_below:
            tenure = self.never_evict_above_accesses
            if tenure is not None and signals.access_count >= tenure:
                reason = (
                    f"strength {score:.4f} below evict threshold {self.evict_below}, "
                    f"but {signals.access_count:g} accesses earn tenure; demoting"
                )
                return self._verdict(record, Action.DEMOTE, reason, score, signals, now)
            reason = f"strength {score:.4f} below evict threshold {self.evict_below}"
            return self._verdict(record, Action.EVICT, reason, score, signals, now)
        if score < self.demote_below:
            reason = f"strength {score:.4f} below demote threshold {self.demote_below}"
            return self._verdict(record, Action.DEMOTE, reason, score, signals, now)
        return self._verdict(
            record, Action.KEEP, f"strength {score:.4f}", score, signals, now
        )

    def _verdict(
        self,
        record: MemoryRecord,
        action: Action,
        reason: str,
        score: Optional[float],
        signals: MemorySignals,
        now: datetime,
    ) -> Verdict:
        reference = signals.reference_time
        idle_seconds = (
            None if reference is None else max(0.0, (now - reference).total_seconds())
        )
        return Verdict(
            record_id=record.id,
            action=action,
            reason=reason,
            strength=score,
            policy=self.name,
            annotations={
                "idle_seconds": idle_seconds,
                "access_count": signals.access_count,
                "effective_half_life_days": (
                    self.effective_half_life(signals.access_count).total_seconds()
                    / 86400.0
                ),
            },
        )
