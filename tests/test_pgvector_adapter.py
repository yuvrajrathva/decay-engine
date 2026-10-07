"""pgvector adapter: paging, JSONB merging, deletion, and identifier safety.

The fake connection below is not a SQL engine. It recognizes the handful of
statement shapes this adapter emits and models their *semantics* -- keyset
paging, `jsonb ||` merge, `= ANY(array)` delete -- which is what the adapter's
logic needs to be tested against. The live round-trip at the bottom is what
proves the SQL itself; it runs when DECAY_ENGINE_PG_DSN points at a database.
"""

import json
import os
from datetime import timedelta

import pytest

from conftest import record
from decay_engine import COLD_FLAG, STRENGTH_FIELD, DecayEngine, DecayPolicy
from decay_engine.adapters.pgvector import PgVectorAdapter


class FakeCursor:
    """Interprets the adapter's statements against an in-memory table."""

    def __init__(self, db):
        self.db = db
        self.description = None
        self._rows = []

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _columns(sql):
        selected = sql[sql.index("SELECT ") + 7 : sql.index(" FROM")]
        return [col.strip().strip('"') for col in selected.split(",")]

    def _row_tuple(self, row, columns):
        out = []
        for column in columns:
            value = row.get(column)
            if column == self.db.metadata_column and self.db.jsonb_as_text:
                value = json.dumps(value)
            out.append(value)
        return tuple(out)

    # -- DB-API ----------------------------------------------------------

    def execute(self, sql, params=None):
        self.db.statements.append((sql, params))
        params = params or ()

        if sql.startswith("SELECT COUNT(*)"):
            self.description = [("count",)]
            self._rows = [(len(self.db.rows),)]
            return

        if sql.startswith("SELECT"):
            columns = self._columns(sql)
            rows = sorted(self.db.rows, key=lambda r: r["id"])
            if " > %s" in sql:
                after, limit = params
                rows = [r for r in rows if r["id"] > after]
            else:
                (limit,) = params
            self.description = [(c,) for c in columns]
            self._rows = [self._row_tuple(r, columns) for r in rows[:limit]]
            return

        if sql.startswith("UPDATE"):
            payload, record_id = params
            patch = json.loads(payload)
            for row in self.db.rows:
                if row["id"] == record_id:
                    current = row.get(self.db.metadata_column) or {}
                    merged = dict(current)
                    merged.update(patch)  # jsonb || jsonb
                    row[self.db.metadata_column] = merged
            self.description = None
            return

        if sql.startswith("DELETE"):
            (ids,) = params
            assert isinstance(ids, list), "delete must pass an array for = ANY(%s)"
            self.db.rows = [r for r in self.db.rows if r["id"] not in ids]
            self.description = None
            return

        raise AssertionError(f"unexpected statement: {sql}")

    def executemany(self, sql, seq):
        self.db.executemany_batches.append(list(seq))
        for params in seq:
            self.execute(sql, params)

    def fetchall(self):
        return self._rows

    def close(self):
        self.db.closed_cursors += 1


class FakeConnection:
    """A DB-API-shaped connection over a list of dict rows."""

    def __init__(self, rows=(), *, metadata_column="metadata", jsonb_as_text=False):
        self.rows = [dict(r) for r in rows]
        self.metadata_column = metadata_column
        self.jsonb_as_text = jsonb_as_text
        self.statements = []
        self.executemany_batches = []
        self.commits = 0
        self.closed_cursors = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def row(record_id, memory, *, document="a memory", **columns):
    """A table row whose JSONB metadata mirrors a MemoryRecord's."""
    return {"id": record_id, "document": document, "metadata": dict(memory.metadata), **columns}


@pytest.fixture
def connection(now):
    return FakeConnection(
        [
            row("hot", record("hot", idle_days=1, extra={"source": "slack"}, now=now)),
            row("warm", record("warm", idle_days=60, extra={"source": "email"}, now=now)),
            row("dead", record("dead", idle_days=300, now=now)),
        ]
    )


@pytest.fixture
def policy():
    return DecayPolicy(half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1)


class TestConstruction:
    def test_rejects_something_that_is_not_a_connection(self):
        with pytest.raises(TypeError, match="no .cursor"):
            PgVectorAdapter(object())

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"table": "memories; DROP TABLE users"},
            {"table": 'mem"ories'},
            {"id_column": "id OR 1=1"},
            {"schema": "public-schema"},
            {"metadata_column": "meta data"},
            {"extra_columns": ["created_at", "bad column"]},
            {"table": "x" * 64},
        ],
    )
    def test_identifiers_that_are_not_plain_names_are_rejected(self, kwargs):
        """Identifiers are interpolated, not parameterized, so they are validated."""
        with pytest.raises(ValueError):
            PgVectorAdapter(FakeConnection(), **kwargs)

    def test_rejects_a_store_exposing_no_metadata_at_all(self):
        with pytest.raises(ValueError, match="no timing signals"):
            PgVectorAdapter(FakeConnection(), metadata_column=None)

    def test_rejects_a_zero_write_batch(self):
        with pytest.raises(ValueError):
            PgVectorAdapter(FakeConnection(), write_batch_size=0)

    def test_quoted_identifiers_appear_in_the_sql(self, connection):
        PgVectorAdapter(connection, table="memories", schema="agent").count()
        assert '"agent"."memories"' in connection.statements[0][0]


class TestIteration:
    def test_reads_id_document_and_jsonb_metadata(self, connection):
        records = list(PgVectorAdapter(connection).iter_records())
        assert [r.id for r in records] == ["dead", "hot", "warm"]  # ordered by id
        assert records[1].text == "a memory"
        assert records[1].metadata["source"] == "slack"

    def test_pages_by_keyset_not_offset(self, now):
        """Keyset paging means no OFFSET, and each page resumes after the last id."""
        rows = [row(f"m-{i:03d}", record(f"m-{i:03d}", idle_days=1, now=now)) for i in range(250)]
        connection = FakeConnection(rows)
        records = list(PgVectorAdapter(connection).iter_records(batch_size=100))

        assert len(records) == 250
        assert len({r.id for r in records}) == 250
        selects = [sql for sql, _ in connection.statements]
        assert all("OFFSET" not in sql for sql in selects)
        assert sum("> %s" in sql for sql in selects) == 2  # pages 2 and 3 resume

    def test_exact_multiple_of_batch_size_terminates(self, now):
        rows = [row(f"m-{i:03d}", record(f"m-{i:03d}", idle_days=1, now=now)) for i in range(200)]
        records = list(PgVectorAdapter(FakeConnection(rows)).iter_records(batch_size=100))
        assert len(records) == 200

    def test_empty_table_yields_nothing(self):
        assert list(PgVectorAdapter(FakeConnection()).iter_records()) == []

    def test_jsonb_returned_as_text_is_parsed(self, now):
        connection = FakeConnection(
            [row("hot", record("hot", idle_days=1, extra={"source": "slack"}, now=now))],
            jsonb_as_text=True,
        )
        records = list(PgVectorAdapter(connection).iter_records())
        assert records[0].metadata["source"] == "slack"

    def test_table_without_a_document_column(self, connection):
        records = list(PgVectorAdapter(connection, text_column=None).iter_records())
        assert all(r.text is None for r in records)
        assert records[0].metadata  # metadata still read

    def test_cursors_are_closed(self, connection):
        list(PgVectorAdapter(connection).iter_records())
        assert connection.closed_cursors >= 1

    def test_invalid_batch_size_is_rejected(self, connection):
        with pytest.raises(ValueError):
            list(PgVectorAdapter(connection).iter_records(batch_size=0))


class TestFirstClassColumns:
    """Schemas that keep timing signals as real columns, not JSONB keys."""

    def test_extra_columns_are_surfaced_into_metadata(self, now):
        connection = FakeConnection(
            [
                {
                    "id": "a",
                    "document": "a memory",
                    "metadata": {"source": "slack"},
                    "inserted_at": now - timedelta(days=400),
                    "read_at": now - timedelta(days=380),
                    "reads": 0,
                }
            ]
        )
        adapter = PgVectorAdapter(
            connection, extra_columns=("inserted_at", "read_at", "reads")
        )
        found = list(adapter.iter_records())[0]
        assert found.metadata["reads"] == 0
        assert found.metadata["source"] == "slack"  # JSONB still merged in

    def test_a_real_column_wins_over_a_same_named_jsonb_key(self, now):
        """Naming a column explicitly is an instruction, so it takes precedence."""
        authoritative = now - timedelta(days=1)
        connection = FakeConnection(
            [
                {
                    "id": "a",
                    "document": "a memory",
                    "metadata": {"created_at": "1999-01-01T00:00:00+00:00"},
                    "created_at": authoritative,
                }
            ]
        )
        adapter = PgVectorAdapter(connection, extra_columns=("created_at",))
        found = list(adapter.iter_records())[0]
        assert found.metadata["created_at"] == authoritative

    def test_decay_reads_real_timestamptz_columns_end_to_end(self, now, policy):
        connection = FakeConnection(
            [
                {"id": "dead", "document": "d", "metadata": {},
                 "created_at": now - timedelta(days=400),
                 "last_accessed_at": now - timedelta(days=380), "access_count": 0},
                {"id": "hot", "document": "h", "metadata": {},
                 "created_at": now - timedelta(days=30),
                 "last_accessed_at": now - timedelta(days=1), "access_count": 3},
            ]
        )
        adapter = PgVectorAdapter(
            connection,
            extra_columns=("created_at", "last_accessed_at", "access_count"),
        )
        report = DecayEngine(adapter, policy).sweep(dry_run=False, now=now)
        assert report.evicted_ids == ["dead"]
        assert [r["id"] for r in connection.rows] == ["hot"]


class TestMetadataUpdates:
    def test_merge_happens_in_postgres_not_python(self, connection):
        """`jsonb || patch` means no read-then-write race with other writers."""
        PgVectorAdapter(connection).update_metadata({"hot": {COLD_FLAG: True}})
        sql = connection.statements[-1][0]
        assert "||" in sql and "::jsonb" in sql
        assert "SELECT" not in sql  # no read-before-write round trip

    def test_patch_preserves_keys_it_does_not_mention(self, connection):
        PgVectorAdapter(connection).update_metadata({"hot": {COLD_FLAG: True}})
        stored = next(r for r in connection.rows if r["id"] == "hot")["metadata"]
        assert stored["source"] == "slack"
        assert stored["created_at"]
        assert stored[COLD_FLAG] is True

    def test_datetimes_are_serialized_to_iso(self, connection, now):
        PgVectorAdapter(connection).update_metadata({"hot": {"when": now}})
        stored = next(r for r in connection.rows if r["id"] == "hot")["metadata"]
        assert stored["when"] == now.isoformat()

    def test_empty_patches_issue_no_statement(self, connection):
        PgVectorAdapter(connection).update_metadata({"hot": {}})
        assert connection.statements == []
        assert connection.commits == 0

    def test_no_updates_at_all_issues_no_statement(self, connection):
        PgVectorAdapter(connection).update_metadata({})
        assert connection.statements == []

    def test_writes_are_batched(self, now):
        rows = [row(f"m-{i}", record(f"m-{i}", idle_days=1, now=now)) for i in range(5)]
        connection = FakeConnection(rows)
        adapter = PgVectorAdapter(connection, write_batch_size=2)
        adapter.update_metadata({f"m-{i}": {COLD_FLAG: True} for i in range(5)})
        assert [len(b) for b in connection.executemany_batches] == [2, 2, 1]

    def test_commit_is_called_once_per_write_call(self, connection):
        adapter = PgVectorAdapter(connection, write_batch_size=1)
        adapter.update_metadata({"hot": {COLD_FLAG: True}, "warm": {COLD_FLAG: True}})
        assert connection.commits == 1

    def test_commit_can_be_disabled(self, connection):
        PgVectorAdapter(connection, commit=False).update_metadata({"hot": {COLD_FLAG: True}})
        assert connection.commits == 0

    def test_a_store_without_jsonb_cannot_record_demotions(self, now):
        """Read-only schemas are allowed, but applying a sweep must fail loudly."""
        connection = FakeConnection(
            [{"id": "a", "document": "d", "created_at": now - timedelta(days=400)}]
        )
        adapter = PgVectorAdapter(
            connection, metadata_column=None, extra_columns=("created_at",)
        )
        with pytest.raises(RuntimeError, match="nowhere to record demotions"):
            adapter.update_metadata({"a": {COLD_FLAG: True}})


class TestDeletion:
    def test_deletes_with_a_single_array_parameter(self, connection):
        PgVectorAdapter(connection).delete(["dead"])
        sql, params = connection.statements[-1]
        assert "= ANY(%s)" in sql
        assert params == (["dead"],)
        assert [r["id"] for r in connection.rows] == ["hot", "warm"]

    def test_deletes_are_batched(self, now):
        rows = [row(f"m-{i}", record(f"m-{i}", idle_days=1, now=now)) for i in range(5)]
        connection = FakeConnection(rows)
        PgVectorAdapter(connection, write_batch_size=2).delete([f"m-{i}" for i in range(5)])
        deletes = [p for sql, p in connection.statements if sql.startswith("DELETE")]
        assert [len(p[0]) for p in deletes] == [2, 2, 1]
        assert connection.rows == []

    def test_deleting_nothing_issues_no_statement(self, connection):
        PgVectorAdapter(connection).delete([])
        assert connection.statements == []
        assert connection.commits == 0


class TestEndToEnd:
    def test_sweep_evicts_and_demotes_through_the_adapter(self, connection, now, policy):
        report = DecayEngine(PgVectorAdapter(connection), policy).sweep(
            dry_run=False, now=now
        )
        assert report.evicted_ids == ["dead"]
        assert report.demoted_ids == ["warm"]
        assert sorted(r["id"] for r in connection.rows) == ["hot", "warm"]

        warm = next(r for r in connection.rows if r["id"] == "warm")["metadata"]
        assert warm[COLD_FLAG] is True
        assert warm[STRENGTH_FIELD] == pytest.approx(0.25, abs=1e-3)
        assert warm["source"] == "email"  # untouched by the demotion

    def test_dry_run_writes_nothing(self, connection, policy, now):
        report = DecayEngine(PgVectorAdapter(connection), policy).sweep(now=now)
        assert report.evicted_ids == ["dead"]  # reported
        assert len(connection.rows) == 3  # but nothing changed
        assert all(sql.startswith("SELECT") for sql, _ in connection.statements)

    def test_count_reports_the_table_size(self, connection):
        assert PgVectorAdapter(connection).count() == 3


DSN = os.environ.get("DECAY_ENGINE_PG_DSN")


@pytest.mark.skipif(not DSN, reason="set DECAY_ENGINE_PG_DSN to run against Postgres")
class TestAgainstRealPostgres:
    """Proves the SQL itself. Needs a database with the vector extension."""

    @pytest.fixture
    def table(self):
        psycopg = pytest.importorskip("psycopg")
        name = "decay_engine_test_memories"
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(f"DROP TABLE IF EXISTS {name}")
                cur.execute(
                    f"""CREATE TABLE {name} (
                        id text PRIMARY KEY,
                        document text,
                        embedding vector(3),
                        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at timestamptz,
                        last_accessed_at timestamptz,
                        access_count int DEFAULT 0
                    )"""
                )
            conn.commit()
            yield name, conn
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {name}")
            conn.commit()

    def test_jsonb_roundtrip_with_real_sql(self, table, now, policy):
        name, conn = table
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {name} (id, document, embedding, metadata) "
                f"VALUES (%s, %s, %s, %s::jsonb)",
                [
                    ("hot", "a fresh memory", "[0.1,0.2,0.3]",
                     json.dumps({"created_at": (now - timedelta(days=2)).isoformat(),
                                 "last_accessed_at": (now - timedelta(days=1)).isoformat(),
                                 "source": "slack"})),
                    ("dead", "an abandoned memory", "[0.4,0.5,0.6]",
                     json.dumps({"created_at": (now - timedelta(days=400)).isoformat(),
                                 "last_accessed_at": (now - timedelta(days=380)).isoformat()})),
                ],
            )
        conn.commit()

        adapter = PgVectorAdapter(conn, table=name)
        report = DecayEngine(adapter, policy, write_scores=True).sweep(
            dry_run=False, now=now
        )

        assert report.evicted_ids == ["dead"]
        assert adapter.count() == 1
        with conn.cursor() as cur:
            cur.execute(f"SELECT metadata FROM {name} WHERE id = 'hot'")
            metadata = cur.fetchone()[0]
        assert metadata["source"] == "slack"  # merge preserved it
        assert metadata[COLD_FLAG] is False
        assert metadata[STRENGTH_FIELD] > 0.9

    def test_real_timestamptz_columns_roundtrip(self, table, now, policy):
        name, conn = table
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {name} "
                f"(id, document, embedding, created_at, last_accessed_at, access_count) "
                f"VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    ("hot", "fresh", "[0.1,0.2,0.3]",
                     now - timedelta(days=30), now - timedelta(days=1), 3),
                    ("dead", "abandoned", "[0.4,0.5,0.6]",
                     now - timedelta(days=400), now - timedelta(days=380), 0),
                ],
            )
        conn.commit()

        adapter = PgVectorAdapter(
            conn, table=name,
            extra_columns=("created_at", "last_accessed_at", "access_count"),
        )
        report = DecayEngine(adapter, policy).sweep(dry_run=False, now=now)
        assert report.evicted_ids == ["dead"]
        assert adapter.count() == 1

    def test_embeddings_survive_a_sweep(self, table, now, policy):
        """A metadata patch must never disturb the vector column."""
        name, conn = table
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {name} (id, document, embedding, metadata) "
                f"VALUES (%s, %s, %s, %s::jsonb)",
                ("warm", "a cooling memory", "[0.7,0.8,0.9]",
                 json.dumps({"created_at": (now - timedelta(days=90)).isoformat(),
                             "last_accessed_at": (now - timedelta(days=60)).isoformat()})),
            )
        conn.commit()

        DecayEngine(PgVectorAdapter(conn, table=name), policy).sweep(
            dry_run=False, now=now
        )
        with conn.cursor() as cur:
            cur.execute(f"SELECT embedding, metadata FROM {name} WHERE id = 'warm'")
            embedding, metadata = cur.fetchone()
        assert str(embedding) == "[0.7,0.8,0.9]"
        assert metadata[COLD_FLAG] is True
