"""The store boundary.

Three capabilities are all decay needs from a memory store: stream records,
patch metadata, delete by id. Anything that can do those three things can be
swept, which is what makes this library store-agnostic.

Iteration also takes an optional ``scope``, which narrows a sweep to a subset of
records. The core treats a scope as opaque -- it never inspects one, and knows
nothing about tenants, users, or namespaces. Each adapter decides what shapes it
accepts and translates them into whatever its backend understands: a ``where``
filter for Chroma, a SQL predicate for Postgres, a callable for the in-memory
store. An adapter that does not implement scoping simply omits the parameter,
and the engine raises a clear error if a caller asks for one anyway.
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

    def iter_records(
        self, *, batch_size: int = 256, scope: Any = None
    ) -> Iterator[MemoryRecord]:
        """Stream records. Order is irrelevant; completeness is not.

        Implementations should page rather than materialize the whole store, so a
        sweep's memory use stays flat as the collection grows.

        Args:
            batch_size: Records to fetch per round trip.
            scope: A backend-specific filter, or ``None`` for every record.
                ``None`` must always mean the whole store -- that is what keeps
                an unscoped sweep's behavior unchanged. An adapter should reject
                a scope shape it does not understand with ``TypeError`` or
                ``ValueError`` rather than silently sweeping everything, since
                quietly widening a scope could delete records the caller meant
                to exclude.
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
