"""Adapter for a pgvector table in Postgres.

pgvector imposes no schema: it is a column type, so every project
lays its memories out differently. This adapter therefore takes the layout as
configuration -- table, id column, document column, and a JSONB metadata column --
and leaves the rest of the row (embeddings included) untouched.

Two layouts are both common, and both work:

* **Metadata in JSONB.** The Chroma-shaped approach. Timing signals live as keys
  inside one ``jsonb`` column, and `FieldMap` reads them from there.
* **Timing signals as real columns.** The idiomatic Postgres approach, with
  ``created_at timestamptz`` and ``access_count int`` as first-class columns.
  Name them in ``extra_columns`` and they are surfaced into each record's
  metadata dict, where `FieldMap` reads them exactly as if they had been JSONB
  keys all along.

Decay's own writes (``decay_cold``, ``decay_strength``, ``decay_swept_at``)
always land in the JSONB column, so a store that wants demotion needs one.

No pgvector-specific behavior leaks into the engine or the policies; this module
is the only place that knows Postgres exists.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from ..models import MemoryRecord
from .base import StoreAdapter, chunked

__all__ = ["PgVectorAdapter"]

# Identifiers are interpolated into SQL, never parameterized, so they are
# validated rather than escaped. Postgres caps identifiers at 63 bytes.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _quote(identifier: str, *, what: str) -> str:
    """Validate and double-quote a SQL identifier.

    Rejecting anything unusual is deliberate: these names come from application
    config, and a quoting bug here would be an injection point.
    """
    if not isinstance(identifier, str) or not _IDENTIFIER.match(identifier):
        raise ValueError(
            f"{what} {identifier!r} is not a plain SQL identifier; "
            "expected letters, digits and underscores, starting with a letter"
        )
    if len(identifier.encode("utf-8")) > 63:
        raise ValueError(f"{what} {identifier!r} exceeds Postgres' 63-byte limit")
    return f'"{identifier}"'


def _jsonable(value: Any) -> Any:
    """Make a metadata value safe for ``json.dumps``."""
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class PgVectorAdapter(StoreAdapter):
    """Sweep a Postgres table of memories.

    Args:
        connection: An open DB-API connection -- ``psycopg`` (v3) and
            ``psycopg2`` both work. The adapter never opens or closes it, so
            pooling and connection lifetime stay yours.
        table: Table holding the memories.
        id_column: Primary key. Iteration pages by it, so it must be unique and
            orderable (``text``, ``uuid`` and integer keys are all fine).
        text_column: Column holding the memory body, or ``None`` if there isn't
            one. Read only; never written.
        metadata_column: ``jsonb`` column holding metadata. Decay's writes merge
            into it. Pass ``None`` for a read-only store with no JSONB column,
            in which case `update_metadata` raises.
        extra_columns: First-class columns to surface into each record's
            metadata dict, for schemas that keep timing signals as real columns.
            These take precedence over same-named JSONB keys, since naming them
            here is an explicit instruction.
        schema: Postgres schema holding the table.
        write_batch_size: Rows per statement on update and delete.
        commit: Call ``connection.commit()`` after each write. Leave it on unless
            the connection is in autocommit mode or you manage transactions.
    """

    def __init__(
        self,
        connection: Any,
        *,
        table: str = "memories",
        id_column: str = "id",
        text_column: Optional[str] = "document",
        metadata_column: Optional[str] = "metadata",
        extra_columns: Sequence[str] = (),
        schema: str = "public",
        write_batch_size: int = 256,
        commit: bool = True,
    ) -> None:
        if not hasattr(connection, "cursor"):
            raise TypeError(
                "connection has no .cursor(); expected a DB-API connection "
                "such as psycopg.connect(...)"
            )
        if write_batch_size < 1:
            raise ValueError("write_batch_size must be at least 1")
        if metadata_column is None and not extra_columns:
            raise ValueError(
                "a store with neither metadata_column nor extra_columns exposes "
                "no timing signals, so every record would be skipped"
            )

        self._connection = connection
        self._commit = commit
        self._write_batch_size = write_batch_size

        self._table = f'{_quote(schema, what="schema")}.{_quote(table, what="table")}'
        self._id = _quote(id_column, what="id_column")
        self._text = _quote(text_column, what="text_column") if text_column else None
        self._metadata = (
            _quote(metadata_column, what="metadata_column") if metadata_column else None
        )
        self._extra_names: Tuple[str, ...] = tuple(extra_columns)
        self._extra = tuple(
            _quote(name, what="extra_columns entry") for name in self._extra_names
        )
        self._metadata_column_name = metadata_column

    # -- plumbing --------------------------------------------------------

    def _execute(self, sql: str, params: Any = None, *, many: bool = False) -> Any:
        """Run one statement and return its rows, closing the cursor either way."""
        cursor = self._connection.cursor()
        try:
            if many:
                cursor.executemany(sql, params)
                return None
            cursor.execute(sql, params)
            fetch = getattr(cursor, "fetchall", None)
            if cursor.description is None or fetch is None:
                return None
            return fetch()
        finally:
            closer = getattr(cursor, "close", None)
            if callable(closer):
                closer()

    def _after_write(self) -> None:
        if self._commit:
            committer = getattr(self._connection, "commit", None)
            if callable(committer):
                committer()

    # -- store ops -------------------------------------------------------

    def iter_records(self, *, batch_size: int = 256) -> Iterator[MemoryRecord]:
        """Stream every row, paging by the id column.

        Paging is keyset-based (``WHERE id > last_seen``) rather than
        ``OFFSET``, so rows are neither skipped nor repeated if the table shifts
        underneath a long sweep.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        selected = [self._id]
        if self._text:
            selected.append(self._text)
        if self._metadata:
            selected.append(self._metadata)
        selected.extend(self._extra)
        columns = ", ".join(selected)

        last_id: Any = None
        while True:
            if last_id is None:
                sql = (
                    f"SELECT {columns} FROM {self._table} "
                    f"ORDER BY {self._id} ASC LIMIT %s"
                )
                params: Tuple[Any, ...] = (batch_size,)
            else:
                sql = (
                    f"SELECT {columns} FROM {self._table} "
                    f"WHERE {self._id} > %s ORDER BY {self._id} ASC LIMIT %s"
                )
                params = (last_id, batch_size)

            rows = self._execute(sql, params) or []
            if not rows:
                return

            for row in rows:
                yield self._to_record(row)
            last_id = rows[-1][0]

            if len(rows) < batch_size:
                return

    def _to_record(self, row: Sequence[Any]) -> MemoryRecord:
        """Assemble one row into a `MemoryRecord`."""
        cursor = 1
        text: Optional[str] = None
        if self._text:
            text = row[cursor]
            cursor += 1

        metadata: Dict[str, Any] = {}
        if self._metadata:
            raw = row[cursor]
            cursor += 1
            if isinstance(raw, str):  # driver returned jsonb unparsed
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            if isinstance(raw, Mapping):
                metadata.update(raw)

        # Explicitly configured columns win over same-named JSONB keys.
        for offset, name in enumerate(self._extra_names):
            metadata[name] = row[cursor + offset]

        return MemoryRecord(id=row[0], text=text, metadata=metadata)

    def update_metadata(self, updates: Mapping[str, Mapping[str, Any]]) -> None:
        """Merge patches into the JSONB metadata column.

        The merge happens in Postgres (``metadata || patch``), so concurrent
        writers touching other keys of the same row are not clobbered
        """
        if self._metadata is None:
            raise RuntimeError(
                "this adapter was built without a metadata_column, so decay has "
                "nowhere to record demotions; configure one to apply sweeps"
            )

        payloads = [
            (
                json.dumps({key: _jsonable(value) for key, value in patch.items()}),
                record_id,
            )
            for record_id, patch in updates.items()
            if patch
        ]
        if not payloads:
            return

        sql = (
            f"UPDATE {self._table} "
            f"SET {self._metadata} = COALESCE({self._metadata}, '{{}}'::jsonb) || %s::jsonb "
            f"WHERE {self._id} = %s"
        )
        for batch in chunked(payloads, self._write_batch_size):
            self._execute(sql, batch, many=True)
        self._after_write()

    def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        sql = f"DELETE FROM {self._table} WHERE {self._id} = ANY(%s)"
        for batch in chunked(ids, self._write_batch_size):
            self._execute(sql, (list(batch),))
        self._after_write()

    def count(self) -> int:
        rows = self._execute(f"SELECT COUNT(*) FROM {self._table}") or [(0,)]
        return int(rows[0][0])
