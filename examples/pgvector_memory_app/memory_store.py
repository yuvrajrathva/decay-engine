"""The application's own memory store: writes, retrieval, and access tracking.

This is the half of the system decay-engine does *not* do. The library reads
timing signals; something has to write them, and that something is here:
`record_access` is called by the app after it decides which retrieved memories
it actually used. Nothing in decay-engine ever increments a counter.

The sweep side lives in `app.py`, which wraps this table in a `PgVectorAdapter`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence

import psycopg

from embedding import embed, to_sql
from sample_data import SAMPLES

# Columns holding the timing signals decay reads. Two are named differently from
# what the library would guess, which is why app.py configures a FieldMap.
CREATED_AT = "created_at"
LAST_RETRIEVED_AT = "last_retrieved_at"
RETRIEVAL_COUNT = "retrieval_count"
PINNED = "pinned"

COLD_MODES = ("exclude", "penalize", "ignore")


@dataclass
class Row:
    """One memory as the application sees it, decay state included."""

    id: str
    document: str
    created_at: Optional[datetime]
    last_retrieved_at: Optional[datetime]
    retrieval_count: int
    pinned: bool
    cold: bool
    strength: Optional[float]
    swept_at: Optional[str]
    distance: Optional[float] = None
    adjusted: Optional[float] = None

    @property
    def idle_days(self) -> Optional[float]:
        reference = self.last_retrieved_at or self.created_at
        if reference is None:
            return None
        return (datetime.now(timezone.utc) - reference).total_seconds() / 86400.0


_SELECT = """
    id,
    document,
    created_at,
    last_retrieved_at,
    retrieval_count,
    pinned,
    COALESCE((metadata->>'decay_cold')::boolean, false)  AS cold,
    (metadata->>'decay_strength')::float                 AS strength,
    metadata->>'decay_swept_at'                          AS swept_at
"""


def _to_row(record: Sequence[Any], *, distance=None, adjusted=None) -> Row:
    return Row(
        id=record[0],
        document=record[1],
        created_at=record[2],
        last_retrieved_at=record[3],
        retrieval_count=record[4],
        pinned=record[5],
        cold=record[6],
        strength=record[7],
        swept_at=record[8],
        distance=distance,
        adjusted=adjusted,
    )


def connect(dsn: str, *, attempts: int = 30, delay: float = 1.0) -> psycopg.Connection:
    """Connect, waiting for Postgres to accept connections.

    Compose declares a healthcheck, but a retry loop costs little and makes the
    example robust when run by hand against a database that is still booting.
    """
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(dsn)
        except psycopg.OperationalError as exc:  # not up yet
            last = exc
            if attempt < attempts:
                time.sleep(delay)
    raise RuntimeError(f"could not reach Postgres after {attempts} attempts: {last}")


# -- writes ---------------------------------------------------------------


def seed(connection: psycopg.Connection) -> int:
    """Replace the table's contents with the deterministic sample memories.

    Timestamps are set relative to now, so the demo behaves the same whenever it
    is run. In a real application these would simply be ``now()`` at write time.
    """
    now = datetime.now(timezone.utc)
    rows = []
    for sample in SAMPLES:
        created = last_retrieved = None
        if sample.idle_days is not None:
            created = now - timedelta(days=sample.age_days)
            last_retrieved = now - timedelta(days=sample.idle_days)
        rows.append(
            (
                sample.id,
                sample.document,
                to_sql(embed(sample.document)),
                created,
                last_retrieved,
                sample.retrieval_count,
                sample.pinned,
            )
        )

    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE memories")
        cursor.executemany(
            """
            INSERT INTO memories
                (id, document, embedding, created_at, last_retrieved_at,
                 retrieval_count, pinned, metadata)
            VALUES (%s, %s, %s::vector, %s, %s, %s, %s, '{}'::jsonb)
            """,
            rows,
        )
    connection.commit()
    return len(rows)


def remember(connection: psycopg.Connection, memory_id: str, document: str) -> None:
    """Store one new memory, the way an agent would during a conversation.

    Note what the application is responsible for: embedding the text, stamping
    ``created_at``, and starting the retrieval counter. decay-engine reads these
    later; it never writes them.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO memories
                (id, document, embedding, created_at, retrieval_count, pinned)
            VALUES (%s, %s, %s::vector, now(), 0, false)
            ON CONFLICT (id) DO UPDATE
                SET document = EXCLUDED.document,
                    embedding = EXCLUDED.embedding
            """,
            (memory_id, document, to_sql(embed(document))),
        )
    connection.commit()


def record_access(connection: psycopg.Connection, memory_ids: Sequence[str]) -> None:
    """Mark these memories as used, right now.

    **This is the application's job, not the library's.** decay-engine reads
    ``last_retrieved_at`` and ``retrieval_count`` to score a memory, but nothing
    in it ever writes them -- it has no way to know what "using" a memory means
    in your agent.

    Called after the app decides which retrieved candidates it actually used, not
    for every row a similarity search returned. Counting every candidate inflates
    every counter equally and destroys the signal.
    """
    if not memory_ids:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE memories
               SET last_retrieved_at = now(),
                   retrieval_count   = retrieval_count + 1
             WHERE id = ANY(%s)
            """,
            (list(memory_ids),),
        )
    connection.commit()


def pin(connection: psycopg.Connection, memory_id: str, pinned: bool = True) -> None:
    """Protect a memory from eviction regardless of how cold it gets."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE memories SET pinned = %s WHERE id = %s", (pinned, memory_id)
        )
    connection.commit()


# -- reads ----------------------------------------------------------------


def list_memories(connection: psycopg.Connection) -> List[Row]:
    """Every memory, oldest-accessed first, with its decay state."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {_SELECT} FROM memories "
            "ORDER BY last_retrieved_at ASC NULLS FIRST, id"
        )
        return [_to_row(record) for record in cursor.fetchall()]


def count(connection: psycopg.Connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM memories")
        return int(cursor.fetchone()[0])


def search(
    connection: psycopg.Connection,
    query: str,
    *,
    limit: int = 3,
    cold_mode: str = "exclude",
    penalty: float = 2.0,
) -> List[Row]:
    """Vector similarity search, with three ways to treat demoted memories.

    This is where decay's output feeds back into the application. A sweep writes
    ``decay_cold`` and ``decay_strength``; what retrieval does with them is the
    app's choice:

    * ``exclude``  -- cold memories are filtered out entirely. Simple, and the
      usual default: a demoted memory stops influencing answers but is still
      there to be promoted if something asks for it directly.
    * ``penalize`` -- cold memories still match, but their distance is scaled up
      in proportion to how weak they are, so they only surface when nothing
      stronger is close. Softer, and usually the better behavior. ``penalty``
      controls how harsh the scaling is: at 0 it reduces to ``ignore``, and a
      high enough value pushes weak memories below stronger but less similar
      ones.
    * ``ignore``   -- decay metadata is not consulted at all, which is what
      retrieval looked like before the sweep existed.

    Returns rows ordered by whichever distance the mode uses. Access is *not*
    recorded here -- the caller decides what it actually used.
    """
    if cold_mode not in COLD_MODES:
        raise ValueError(f"cold_mode must be one of {COLD_MODES}, got {cold_mode!r}")

    vector = to_sql(embed(query))
    strength_sql = "COALESCE((metadata->>'decay_strength')::float, 1.0)"
    adjusted_sql = f"(embedding <=> %s::vector) * (1 + %s * (1 - {strength_sql}))"

    if cold_mode == "exclude":
        sql = (
            f"SELECT {_SELECT}, embedding <=> %s::vector AS distance, NULL::float "
            "FROM memories "
            "WHERE NOT COALESCE((metadata->>'decay_cold')::boolean, false) "
            "ORDER BY distance LIMIT %s"
        )
        params: tuple = (vector, limit)
    elif cold_mode == "penalize":
        sql = (
            f"SELECT {_SELECT}, embedding <=> %s::vector AS distance, "
            f"{adjusted_sql} AS adjusted "
            "FROM memories ORDER BY adjusted LIMIT %s"
        )
        params = (vector, vector, penalty, limit)
    else:  # ignore
        sql = (
            f"SELECT {_SELECT}, embedding <=> %s::vector AS distance, NULL::float "
            "FROM memories ORDER BY distance LIMIT %s"
        )
        params = (vector, limit)

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [
            _to_row(record, distance=record[9], adjusted=record[10])
            for record in cursor.fetchall()
        ]
