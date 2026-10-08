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
from decay_engine.adapters.pgvector import PgVectorAdapter, PgVectorScope


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

    @staticmethod
    def _where(sql):
        """The WHERE clause, or '' -- paren-aware so a scope can contain AND."""
        if " WHERE " not in sql:
            return ""
        start = sql.index(" WHERE ") + 7
        end = sql.index(" ORDER BY") if " ORDER BY" in sql else len(sql)
        return sql[start:end].strip()

    @staticmethod
    def _split_scope(where):
        """Peel the leading ``(scope)`` conjunct off a WHERE clause."""
        if not where.startswith("("):
            return "", where
        depth = 0
        for index, char in enumerate(where):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                scope = where[1:index]
                rest = where[index + 1 :].strip()
                return scope, rest[4:].strip() if rest.startswith("AND ") else rest
        raise AssertionError(f"unbalanced parentheses in {where!r}")

    def _scope_matches(self, row, scope, params):
        """Evaluate the scope forms the adapter builds."""
        if not scope:
            return True
        if "@>" in scope:  # metadata @> %s::jsonb
            wanted = json.loads(params[0])
            held = row.get(self.db.metadata_column) or {}
            return all(held.get(k) == v for k, v in wanted.items())
        # one or more `"col" = %s` conjuncts, params in order
        values = list(params)
        for conjunct in scope.split(" AND "):
            column = conjunct.split("=")[0].strip().strip('"')
            if row.get(column) != values.pop(0):
                return False
        return True

    def execute(self, sql, params=None):
        self.db.statements.append((sql, params))
        params = list(params or ())

        if sql.startswith("SELECT COUNT(*)"):
            scope, _ = self._split_scope(self._where(sql))
            rows = [r for r in self.db.rows if self._scope_matches(r, scope, params)]
            self.description = [("count",)]
            self._rows = [(len(rows),)]
            return

        if sql.startswith("SELECT"):
            columns = self._columns(sql)
            limit = params.pop()  # LIMIT %s is always the last parameter
            scope, keyset = self._split_scope(self._where(sql))
            after = params.pop() if keyset else None
            rows = sorted(self.db.rows, key=lambda r: r["id"])
            rows = [r for r in rows if self._scope_matches(r, scope, params)]
            if after is not None:
                rows = [r for r in rows if r["id"] > after]
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
                        access_count int DEFAULT 0,
                        tenant_id text
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

    def test_scoped_sweep_against_real_sql(self, table, now, policy):
        """Both scope shapes, executed by Postgres rather than a double."""
        name, conn = table
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {name} (id, document, embedding, metadata, tenant_id) "
                f"VALUES (%s, %s, %s, %s::jsonb, %s)",
                [
                    (f"{tenant}-{age}", "m", "[0.1,0.2,0.3]",
                     json.dumps({"created_at": (now - timedelta(days=days + 20)).isoformat(),
                                 "last_accessed_at": (now - timedelta(days=days)).isoformat(),
                                 "tenant_id": tenant}),
                     tenant)
                    for tenant in ("acme", "globex")
                    for age, days in (("hot", 1), ("dead", 400))
                ],
            )
        conn.commit()

        adapter = PgVectorAdapter(conn, table=name)
        assert adapter.count() == 4

        # JSONB containment
        report = DecayEngine(adapter, policy).sweep(
            dry_run=False, now=now, scope={"tenant_id": "acme"}
        )
        assert report.evicted_ids == ["acme-dead"]
        assert adapter.count() == 3

        # a real column, via a validated equality predicate
        report = DecayEngine(adapter, policy).sweep(
            dry_run=False, now=now,
            scope=PgVectorScope.for_columns({"tenant_id": "globex"}),
        )
        assert report.evicted_ids == ["globex-dead"]
        assert adapter.count() == 2
        assert adapter.count({"tenant_id": "acme"}) == 1

    def test_arbitrary_sql_predicate_against_real_sql(self, table, now, policy):
        """A raw fragment the fake cannot evaluate, proving real SQL passthrough."""
        name, conn = table
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {name} (id, document, embedding, metadata, access_count) "
                f"VALUES (%s, %s, %s, %s::jsonb, %s)",
                [
                    (rid, "m", "[0.1,0.2,0.3]",
                     json.dumps({"created_at": (now - timedelta(days=420)).isoformat(),
                                 "last_accessed_at": (now - timedelta(days=400)).isoformat()}),
                     hits)
                    for rid, hits in (("rare", 0), ("popular", 99))
                ],
            )
        conn.commit()

        adapter = PgVectorAdapter(conn, table=name)
        report = DecayEngine(adapter, policy).sweep(
            dry_run=False, now=now,
            scope=PgVectorScope("access_count < %s AND document IS NOT NULL", (10,)),
        )
        assert report.evicted_ids == ["rare"]
        assert adapter.count() == 1


class TestScopedSweeps:
    """Narrowing a sweep to a subset of rows."""

    @pytest.fixture
    def tenants(self, now):
        """Two tenants, each with one fresh and one abandoned memory."""
        rows = []
        for tenant in ("acme", "globex"):
            rows.append(
                {**row(f"{tenant}-hot", record(f"{tenant}-hot", idle_days=1, now=now)),
                 "tenant_id": tenant}
            )
            rows.append(
                {**row(f"{tenant}-dead", record(f"{tenant}-dead", idle_days=300, now=now)),
                 "tenant_id": tenant}
            )
            rows[-1]["metadata"]["tenant_id"] = tenant
            rows[-2]["metadata"]["tenant_id"] = tenant
        return FakeConnection(rows)

    def test_unscoped_sweep_still_covers_the_whole_table(self, tenants, policy, now):
        report = DecayEngine(PgVectorAdapter(tenants), policy).sweep(now=now)
        assert report.scanned == 4
        assert sorted(report.evicted_ids) == ["acme-dead", "globex-dead"]

    def test_mapping_scope_filters_by_jsonb_containment(self, tenants, policy, now):
        adapter = PgVectorAdapter(tenants)
        report = DecayEngine(adapter, policy).sweep(
            now=now, scope={"tenant_id": "acme"}
        )
        assert report.scanned == 2
        assert report.evicted_ids == ["acme-dead"]
        sql = tenants.statements[0][0]
        assert "@> %s::jsonb" in sql

    def test_scoped_apply_leaves_other_rows_untouched(self, tenants, policy, now):
        adapter = PgVectorAdapter(tenants)
        DecayEngine(adapter, policy).sweep(
            dry_run=False, now=now, scope={"tenant_id": "acme"}
        )
        remaining = sorted(r["id"] for r in tenants.rows)
        assert remaining == ["acme-hot", "globex-dead", "globex-hot"]

    def test_column_scope_filters_by_real_column(self, tenants, policy, now):
        adapter = PgVectorAdapter(tenants)
        scope = PgVectorScope.for_columns({"tenant_id": "globex"})
        report = DecayEngine(adapter, policy).sweep(dry_run=False, now=now, scope=scope)
        assert report.evicted_ids == ["globex-dead"]
        assert '"tenant_id" = %s' in tenants.statements[0][0]

    def test_scope_params_precede_the_keyset_param(self, now):
        """Parameter order must mirror the clause order or paging breaks."""
        rows = [
            {**row(f"m-{i:03d}", record(f"m-{i:03d}", idle_days=1, now=now)),
             "tenant_id": "acme"}
            for i in range(150)
        ]
        connection = FakeConnection(rows)
        adapter = PgVectorAdapter(connection)
        scope = PgVectorScope.for_columns({"tenant_id": "acme"})
        found = list(adapter.iter_records(batch_size=100, scope=scope))

        assert len(found) == 150
        first_sql, first_params = connection.statements[0]
        second_sql, second_params = connection.statements[1]
        assert first_params == ("acme", 100)
        assert second_params == ("acme", "m-099", 100)
        assert '"id" > %s' in second_sql and "ORDER BY" in second_sql

    def test_scope_and_keyset_are_anded(self, tenants):
        adapter = PgVectorAdapter(tenants)
        list(adapter.iter_records(batch_size=1, scope={"tenant_id": "acme"}))
        combined = [s for s, _ in tenants.statements if '"id" > %s' in s]
        assert combined and " AND " in combined[0]

    def test_count_accepts_a_scope(self, tenants):
        adapter = PgVectorAdapter(tenants)
        assert adapter.count() == 4
        assert adapter.count({"tenant_id": "acme"}) == 2

    def test_raw_scope_carries_its_own_parameters(self, tenants):
        adapter = PgVectorAdapter(tenants)
        list(adapter.iter_records(scope=PgVectorScope('"tenant_id" = %s', ("acme",))))
        assert tenants.statements[0][1] == ("acme", 256)


class TestScopeValidation:
    def test_an_empty_predicate_is_refused(self):
        with pytest.raises(ValueError, match="non-empty"):
            PgVectorScope("")

    def test_a_statement_terminator_is_refused(self):
        with pytest.raises(ValueError, match="';'"):
            PgVectorScope("tenant_id = 'acme'; DROP TABLE memories")

    def test_for_columns_validates_column_names(self):
        with pytest.raises(ValueError):
            PgVectorScope.for_columns({"tenant_id); DROP TABLE x --": "acme"})

    def test_for_columns_needs_at_least_one_condition(self):
        """An empty scope must not quietly become 'everything'."""
        with pytest.raises(ValueError, match="at least one"):
            PgVectorScope.for_columns({})

    def test_an_empty_mapping_scope_is_refused(self, connection):
        with pytest.raises(ValueError, match="empty mapping"):
            list(PgVectorAdapter(connection).iter_records(scope={}))

    def test_an_unknown_scope_shape_is_refused(self, connection):
        with pytest.raises(TypeError, match="must be a mapping"):
            list(PgVectorAdapter(connection).iter_records(scope=["tenant", "acme"]))

    def test_mapping_scope_needs_a_jsonb_column(self, now):
        connection = FakeConnection(
            [{"id": "a", "document": "d", "created_at": now, "tenant_id": "acme"}]
        )
        adapter = PgVectorAdapter(
            connection, metadata_column=None, extra_columns=("created_at",)
        )
        with pytest.raises(ValueError, match="for_columns"):
            list(adapter.iter_records(scope={"tenant_id": "acme"}))

    def test_for_columns_works_without_a_jsonb_column(self, now):
        connection = FakeConnection(
            [{"id": "a", "document": "d", "created_at": now, "tenant_id": "acme"}]
        )
        adapter = PgVectorAdapter(
            connection, metadata_column=None, extra_columns=("created_at",)
        )
        scope = PgVectorScope.for_columns({"tenant_id": "acme"})
        assert len(list(adapter.iter_records(scope=scope))) == 1
