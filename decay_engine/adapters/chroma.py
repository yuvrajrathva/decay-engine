"""Adapter for a Chroma collection.

Chroma's ``update`` replaces a record's metadata wholesale, so this adapter reads
the current metadata and merges patches into it before writing. That keeps the
`StoreAdapter` promise that a patch never drops keys it did not mention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

from ..models import MemoryRecord
from .base import StoreAdapter, chunked

__all__ = ["ChromaAdapter", "ChromaScope"]

@dataclass(frozen=True)
class ChromaScope:
    """A scope for a Chroma sweep, for when a bare ``where`` is not enough.

    Both fields are passed straight through to ``collection.get``, so they use
    Chroma's own filter syntax. Passing a plain mapping as a scope is shorthand
    for ``ChromaScope(where=that_mapping)``.
    """

    where: Optional[Mapping[str, Any]] = None
    where_document: Optional[Mapping[str, Any]] = None

    def as_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if self.where:
            kwargs["where"] = dict(self.where)
        if self.where_document:
            kwargs["where_document"] = dict(self.where_document)
        return kwargs


def _scope_kwargs(scope: Any) -> Dict[str, Any]:
    """Translate a scope into ``collection.get`` keyword arguments."""
    if scope is None:
        return {}
    if isinstance(scope, ChromaScope):
        return scope.as_kwargs()
    if isinstance(scope, Mapping):
        return {"where": dict(scope)} if scope else {}
    raise TypeError(
        "ChromaAdapter scope must be a mapping (used as Chroma's `where`) or a "
        f"ChromaScope, got {type(scope).__name__}"
    )


# Chroma rejects metadata values outside these types.
_SCALARS = (str, int, float, bool)


def _sanitize(patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce a metadata patch into values Chroma will accept.

    Datetimes become ISO strings and ``None`` becomes an empty string, because
    Chroma has no null metadata value and rejects the key outright.
    """
    clean: Dict[str, Any] = {}
    for key, value in patch.items():
        if isinstance(value, datetime):
            clean[key] = value.isoformat()
        elif value is None:
            clean[key] = ""
        elif isinstance(value, _SCALARS):
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


class ChromaAdapter(StoreAdapter):
    """Wraps a ``chromadb`` collection.

    Args:
        collection: A collection handle from ``client.get_collection(...)``.
        write_batch_size: Ids per update/delete call.
    """

    def __init__(self, collection: Any, *, write_batch_size: int = 256) -> None:
        for method in ("get", "update", "delete"):
            if not hasattr(collection, method):
                raise TypeError(
                    f"collection is missing .{method}(); expected a chromadb collection"
                )
        self._collection = collection
        self._write_batch_size = write_batch_size

    @property
    def collection(self) -> Any:
        return self._collection

    def iter_records(
        self, *, batch_size: int = 256, scope: Any = None
    ) -> Iterator[MemoryRecord]:
        """Page through the collection with offset/limit.

        Paging is offset-based, so do not add or delete records while iterating;
        collect decisions first, then apply them.

        ``scope`` may be a mapping, used as Chroma's ``where`` metadata filter,
        or a `ChromaScope` when a ``where_document`` is also needed. Offsets
        then page through the filtered set.
        """
        filters = _scope_kwargs(scope)
        offset = 0
        while True:
            page = self._collection.get(
                limit=batch_size,
                offset=offset,
                include=["metadatas", "documents"],
                **filters,
            )
            ids = list(page.get("ids") or [])
            if not ids:
                return
            documents = page.get("documents") or [None] * len(ids)
            metadatas = page.get("metadatas") or [None] * len(ids)
            for index, record_id in enumerate(ids):
                yield MemoryRecord(
                    id=record_id,
                    text=documents[index] if index < len(documents) else None,
                    metadata=dict(
                        metadatas[index]
                        if index < len(metadatas) and metadatas[index]
                        else {}
                    ),
                )
            if len(ids) < batch_size:
                return
            offset += len(ids)

    def update_metadata(self, updates: Mapping[str, Mapping[str, Any]]) -> None:
        ids = [record_id for record_id in updates if updates[record_id]]
        for batch in chunked(ids, self._write_batch_size):
            current = self._collection.get(ids=batch, include=["metadatas"])
            existing_ids = list(current.get("ids") or [])
            existing_meta = current.get("metadatas") or []
            by_id = {
                record_id: dict(existing_meta[i] or {}) if i < len(existing_meta) else {}
                for i, record_id in enumerate(existing_ids)
            }
            merged_ids = []
            merged_meta = []
            for record_id in batch:
                if record_id not in by_id:  # deleted since the sweep read it
                    continue
                merged = by_id[record_id]
                merged.update(_sanitize(updates[record_id]))
                merged_ids.append(record_id)
                merged_meta.append(merged)
            if merged_ids:
                self._collection.update(ids=merged_ids, metadatas=merged_meta)

    def delete(self, ids: Sequence[str]) -> None:
        for batch in chunked(ids, self._write_batch_size):
            self._collection.delete(ids=batch)

    def count(self) -> int:
        counter = getattr(self._collection, "count", None)
        return int(counter()) if callable(counter) else 0
