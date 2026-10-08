"""An in-process store, for tests and for trying policies on sample data."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Sequence

from ..models import MemoryRecord
from .base import StoreAdapter

__all__ = ["InMemoryStore"]


class InMemoryStore(StoreAdapter):
    """A dict-backed `StoreAdapter`. Mutations apply immediately."""

    def __init__(self, records: Optional[Iterable[MemoryRecord]] = None) -> None:
        self._records: Dict[str, MemoryRecord] = {}
        for record in records or ():
            self.add(record)

    # -- store ops -------------------------------------------------------

    def add(self, record: MemoryRecord) -> None:
        self._records[record.id] = MemoryRecord(
            id=record.id, text=record.text, metadata=dict(record.metadata)
        )

    def iter_records(
        self, *, batch_size: int = 256, scope: Any = None
    ) -> Iterator[MemoryRecord]:
        """Stream records, optionally narrowed by ``scope``.

        Accepts either a predicate taking a `MemoryRecord`, or a mapping of
        metadata keys to required values (matched by equality, all keys must
        match). ``None`` yields everything.
        """
        matches = _predicate(scope)
        # Snapshot the ids so a caller applying deletes mid-iteration is safe.
        for record_id in list(self._records):
            record = self._records.get(record_id)
            if record is not None and matches(record):
                yield record

    def update_metadata(self, updates: Mapping[str, Mapping[str, Any]]) -> None:
        for record_id, patch in updates.items():
            record = self._records.get(record_id)
            if record is None:
                continue
            merged = dict(record.metadata)
            merged.update(patch)
            record.metadata = merged

    def delete(self, ids: Sequence[str]) -> None:
        for record_id in ids:
            self._records.pop(record_id, None)

    # -- introspection ---------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, record_id: object) -> bool:
        return record_id in self._records

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        return self._records.get(record_id)

    @property
    def ids(self) -> Sequence[str]:
        return list(self._records)


def _predicate(scope: Any) -> Callable[[MemoryRecord], bool]:
    """Turn a scope into a record test, rejecting shapes we cannot honor."""
    if scope is None:
        return lambda record: True
    if callable(scope):
        return scope
    if isinstance(scope, Mapping):
        wanted = dict(scope)

        def matches(record: MemoryRecord) -> bool:
            return all(record.metadata.get(k) == v for k, v in wanted.items())

        return matches
    raise TypeError(
        "InMemoryStore scope must be a predicate or a mapping of metadata "
        f"values, got {type(scope).__name__}"
    )
