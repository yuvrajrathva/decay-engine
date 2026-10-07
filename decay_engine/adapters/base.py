"""The store boundary.

Three capabilities are all decay needs from a memory store: stream records,
patch metadata, delete by id. Anything that can do those three things can be
swept, which is what makes this library store-agnostic.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Mapping, Sequence

try:  # pragma: no cover
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from ..models import MemoryRecord

__all__ = ["StoreAdapter"]


@runtime_checkable
class StoreAdapter(Protocol):
    """What the engine requires of a memory store."""

    def iter_records(self, *, batch_size: int = 256) -> Iterator[MemoryRecord]:
        """Stream every record. Order is irrelevant; completeness is not.

        Implementations should page rather than materialize the whole store, so a
        sweep's memory use stays flat as the collection grows.
        """
        ...

    def update_metadata(self, updates: Mapping[str, Mapping[str, Any]]) -> None:
        """Merge metadata patches into existing records, keyed by record id.

        Must not drop keys absent from the patch, and must not touch embeddings
        or document bodies.
        """
        ...

    def delete(self, ids: Sequence[str]) -> None:
        """Remove these records permanently."""
        ...


def chunked(items: Iterable[Any], size: int) -> Iterator[list]:
    """Yield ``items`` in lists of at most ``size``. Shared adapter helper."""
    if size < 1:
        raise ValueError("size must be at least 1")
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
