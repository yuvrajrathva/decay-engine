"""Sweep orchestration: read the store, judge every record, then act.

The safety model is the important part. A sweep is a read phase followed by a
write phase, never interleaved, and the write phase does not run unless the
caller asks for it. A `SweepReport` records its decisions before either path
acts, so a dry run is a faithful preview of what applying would do -- the
`evicted_ids`, `demoted_ids`, and `promoted_ids` lists are populated either way,
and `applied` is what distinguishes a preview from a change.
"""

from __future__ import annotations

import inspect
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from .adapters.base import StoreAdapter
from .models import utcnow
from .policies.base import Action, Policy, Verdict
from .policies.decay import DecayPolicy

__all__ = [
    "COLD_FLAG",
    "STRENGTH_FIELD",
    "SWEPT_AT_FIELD",
    "DecayEngine",
    "EvictionLimitExceeded",
    "ScopeNotSupported",
    "SweepReport",
]

COLD_FLAG = "decay_cold"
STRENGTH_FIELD = "decay_strength"
SWEPT_AT_FIELD = "decay_swept_at"


@dataclass
class SweepReport:
    """What a sweep decided, and what it actually did."""

    verdicts: List[Verdict] = field(default_factory=list)
    applied: bool = False
    started_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    evicted_ids: List[str] = field(default_factory=list)
    demoted_ids: List[str] = field(default_factory=list)
    promoted_ids: List[str] = field(default_factory=list)
    previously_cold: Set[str] = field(default_factory=set)
    eviction_limit_exceeded: bool = False
    scope: Any = None

    @property
    def scanned(self) -> int:
        return len(self.verdicts)

    @property
    def counts(self) -> Dict[str, int]:
        tally = Counter(verdict.action.value for verdict in self.verdicts)
        return {action.value: tally.get(action.value, 0) for action in Action}

    def by_action(self, action: Action) -> List[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.action is action]

    @property
    def skipped(self) -> List[Verdict]:
        """Records no policy could judge. Worth watching: a growing count means
        the field map does not match the store's schema."""
        return self.by_action(Action.SKIP)

    def summary(self) -> str:
        counts = self.counts
        mode = "applied" if self.applied else "dry run"
        return (
            f"swept {self.scanned} records ({mode}): "
            f"{counts['keep']} keep, {counts['demote']} demote, "
            f"{counts['evict']} evict, {counts['skip']} skip"
        )


class DecayEngine:
    """Runs a policy over a store and, optionally, applies its verdicts.

    Args:
        adapter: The store to sweep.
        policy: Defaults to `DecayPolicy` with its stock settings.
        batch_size: Records read per page.
        write_scores: Also write the strength back onto records that were kept.
            Off by default: it is one metadata write per record, which is
            expensive on a large store and only useful if your retrieval layer
            actually reads the score.
        max_evictions: Refuse to delete more than this in one sweep. A blunt but
            effective guard against a misconfigured half-life wiping a store.
        on_verdict: Called for every verdict as it is produced, for audit logging.
    """

    def __init__(
        self,
        adapter: StoreAdapter,
        policy: Optional[Policy] = None,
        *,
        batch_size: int = 256,
        write_scores: bool = False,
        max_evictions: Optional[int] = None,
        on_verdict: Optional[Callable[[Verdict], None]] = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if max_evictions is not None and max_evictions < 0:
            raise ValueError("max_evictions must be non-negative")
        self.adapter = adapter
        self.policy = policy or DecayPolicy()
        self.batch_size = batch_size
        self.write_scores = write_scores
        self.max_evictions = max_evictions
        self.on_verdict = on_verdict

    # -- read phase ------------------------------------------------------

    def evaluate(
        self, *, now: Optional[datetime] = None, scope: Any = None
    ) -> SweepReport:
        """Judge records without touching the store.

        ``scope`` narrows which records are read; ``None`` reads all of them.
        """
        moment = now or utcnow()
        report = SweepReport(started_at=moment, scope=scope)
        for record in self._iter_records(scope):
            verdict = self.policy.evaluate(record, now=moment)
            if _is_truthy(record.metadata.get(COLD_FLAG)):
                report.previously_cold.add(record.id)
            report.verdicts.append(verdict)
            if self.on_verdict is not None:
                self.on_verdict(verdict)
        report.finished_at = utcnow()
        return report

    def _iter_records(self, scope: Any):
        """Stream from the adapter, passing a scope only when there is one.

        Omitting the keyword entirely when unscoped keeps adapters written
        against the older two-argument protocol working unchanged.
        """
        if scope is None:
            return self.adapter.iter_records(batch_size=self.batch_size)
        if not self._adapter_accepts_scope():
            raise ScopeNotSupported(
                f"{type(self.adapter).__name__}.iter_records() takes no 'scope' "
                "parameter, so this sweep cannot be narrowed. Sweeping the whole "
                "store instead could delete records you meant to exclude, so "
                "this is an error rather than a fallback."
            )
        return self.adapter.iter_records(batch_size=self.batch_size, scope=scope)

    def _adapter_accepts_scope(self) -> bool:
        """Whether this adapter's `iter_records` understands a scope."""
        try:
            parameters = inspect.signature(self.adapter.iter_records).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            return True  # not introspectable; let the call decide
        if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
            return True
        return "scope" in parameters

    # -- write phase -----------------------------------------------------

    def sweep(
        self,
        *,
        dry_run: bool = True,
        now: Optional[datetime] = None,
        scope: Any = None,
    ) -> SweepReport:
        """Evaluate the store, and apply the verdicts unless this is a dry run.

        Dry run is the default. Pass ``dry_run=False`` deliberately.

        Args:
            dry_run: Decide without writing. The report is identical either way
                apart from ``applied``.
            now: The instant to score against. Defaults to now; pass it to make
                a sweep reproducible.
            scope: A backend-specific filter narrowing which records are swept.
                ``None`` sweeps everything, which is the default and the
                behavior of every release before scoping existed. Records outside
                the scope are not read, not judged, and not touched -- they do
                not appear in the report at all.
        """
        moment = now or utcnow()
        report = self.evaluate(now=moment, scope=scope)

        # Record the decisions before acting on any of them. Both paths read
        # these same lists, so a dry run reports exactly what applying would do
        # and the two branches cannot drift apart.
        report.evicted_ids = [v.record_id for v in report.by_action(Action.EVICT)]
        report.demoted_ids = [v.record_id for v in report.by_action(Action.DEMOTE)]
        report.promoted_ids = [
            v.record_id
            for v in report.by_action(Action.KEEP)
            if v.record_id in report.previously_cold
        ]
        report.eviction_limit_exceeded = (
            self.max_evictions is not None
            and len(report.evicted_ids) > self.max_evictions
        )

        # A dry run reports a breach rather than raising: seeing which records a
        # too-aggressive half-life would delete is the whole point of previewing.
        if dry_run:
            report.finished_at = utcnow()
            return report

        if report.eviction_limit_exceeded:
            raise EvictionLimitExceeded(
                f"sweep would evict {len(report.evicted_ids)} records, "
                f"above max_evictions={self.max_evictions}; "
                "raise the limit or loosen the policy"
            )

        patches = self._metadata_patches(report, moment)
        if patches:
            self.adapter.update_metadata(patches)
        if report.evicted_ids:
            self.adapter.delete(report.evicted_ids)

        report.applied = True
        report.finished_at = utcnow()
        return report

    def _metadata_patches(
        self, report: SweepReport, moment: datetime
    ) -> Dict[str, Dict[str, Any]]:
        """Build the metadata writes for demotions, and for keeps if enabled."""
        patches: Dict[str, Dict[str, Any]] = {}
        stamp = moment.isoformat()
        for verdict in report.verdicts:
            if verdict.action is Action.DEMOTE:
                patch: Dict[str, Any] = {COLD_FLAG: True, SWEPT_AT_FIELD: stamp}
                if verdict.strength is not None:
                    patch[STRENGTH_FIELD] = round(verdict.strength, 6)
                patches[verdict.record_id] = patch
            elif verdict.action is Action.KEEP and (
                self.write_scores or verdict.record_id in report.previously_cold
            ):
                # Clearing a stale cold flag is not optional: a memory that got
                # used again must stop being down-ranked by the retrieval layer.
                patch = {COLD_FLAG: False, SWEPT_AT_FIELD: stamp}
                if verdict.strength is not None:
                    patch[STRENGTH_FIELD] = round(verdict.strength, 6)
                patches[verdict.record_id] = patch
        return patches


def _is_truthy(value: Any) -> bool:
    """Read a stored flag back, tolerating the string forms stores coerce to."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


class EvictionLimitExceeded(RuntimeError):
    """Raised when a sweep would delete more records than ``max_evictions``."""


class ScopeNotSupported(TypeError):
    """Raised when a scope is given to an adapter that cannot narrow a sweep."""
