"""The policy contract: look at one record, return one verdict."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

try:  # pragma: no cover - Protocol moved in 3.8, runtime_checkable in 3.8
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from ..models import MemoryRecord

__all__ = ["Action", "Verdict", "Policy"]


class Action(str, Enum):
    """What a sweep should do with a record.

    ``SKIP`` is distinct from ``KEEP``: keep is a decision, skip is the absence
    of one. A record whose timestamps we could not read is skipped, never evicted.
    """

    KEEP = "keep"
    DEMOTE = "demote"
    EVICT = "evict"
    SKIP = "skip"


@dataclass(frozen=True)
class Verdict:
    """A policy's decision about a single record, with its reasoning."""

    record_id: str
    action: Action
    reason: str
    strength: Optional[float] = None
    policy: str = ""
    annotations: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_destructive(self) -> bool:
        return self.action is Action.EVICT


@runtime_checkable
class Policy(Protocol):
    """Anything that can judge a record. Stateless by contract."""

    name: str

    def evaluate(self, record: MemoryRecord, *, now: datetime) -> Verdict:
        ...
