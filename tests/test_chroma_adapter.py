"""Chroma adapter: paging, metadata merging, and Chroma's type constraints.

The double below imitates the parts of Chroma's collection API the adapter uses,
including the behaviors that make the adapter necessary: `update` replaces a
record's metadata wholesale, and metadata values must be scalars.
"""

from datetime import timedelta

import pytest

from conftest import record
from decay_engine import COLD_FLAG, DecayEngine, DecayPolicy
from decay_engine.adapters.chroma import ChromaAdapter

SCALARS = (str, int, float, bool)


class FakeCollection:
    """A minimal stand-in for a chromadb collection."""

    def __init__(self, records=()):
        self._ids = []
        self._docs = {}
        self._meta = {}
        self.update_calls = []
        self.delete_calls = []
        self.get_calls = []
        for item in records:
            self._ids.append(item.id)
            self._docs[item.id] = item.text
            self._meta[item.id] = dict(item.metadata)

    def get(self, ids=None, limit=None, offset=None, include=None):
        self.get_calls.append({"ids": ids, "limit": limit, "offset": offset})
        selected = ids if ids is not None else self._ids[offset or 0 :][:limit]
        selected = [i for i in selected if i in self._meta]
        return {
            "ids": selected,
            "documents": [self._docs.get(i) for i in selected],
            "metadatas": [dict(self._meta[i]) for i in selected],
        }

    def update(self, ids=None, metadatas=None):
        self.update_calls.append({"ids": list(ids), "metadatas": [dict(m) for m in metadatas]})
        for record_id, metadata in zip(ids, metadatas):
            for key, value in metadata.items():
                if not isinstance(value, SCALARS):
                    raise ValueError(
                        f"chroma rejects metadata {key!r} of type {type(value).__name__}"
                    )
            self._meta[record_id] = dict(metadata)  # Chroma replaces, not merges

    def delete(self, ids=None):
        self.delete_calls.append(list(ids))
        for record_id in ids:
            self._meta.pop(record_id, None)
            self._docs.pop(record_id, None)
            if record_id in self._ids:
                self._ids.remove(record_id)

    def count(self):
        return len(self._ids)


@pytest.fixture
def collection(now):
    return FakeCollection(
        [
            record("hot", idle_days=1, extra={"source": "slack"}, now=now),
            record("warm", idle_days=60, extra={"source": "email"}, now=now),
            record("dead", idle_days=300, now=now),
        ]
    )


class TestContract:
    def test_rejects_an_object_that_is_not_a_collection(self):
        with pytest.raises(TypeError, match="missing .get"):
            ChromaAdapter(object())

    def test_reads_ids_documents_and_metadata(self, collection):
        records = list(ChromaAdapter(collection).iter_records())
        assert [r.id for r in records] == ["hot", "warm", "dead"]
        assert records[0].text == "memory hot"
        assert records[0].metadata["source"] == "slack"

    def test_pages_through_a_collection_larger_than_one_batch(self, now):
        big = FakeCollection([record(f"m-{i}", idle_days=1, now=now) for i in range(250)])
        records = list(ChromaAdapter(big).iter_records(batch_size=100))
        assert len(records) == 250
        assert len({r.id for r in records}) == 250
        assert [call["offset"] for call in big.get_calls] == [0, 100, 200]

    def test_exact_multiple_of_batch_size_terminates(self, now):
        """The page loop must not stop early or spin on a perfectly full last page."""
        big = FakeCollection([record(f"m-{i}", idle_days=1, now=now) for i in range(200)])
        records = list(ChromaAdapter(big).iter_records(batch_size=100))
        assert len(records) == 200

    def test_empty_collection_yields_nothing(self):
        assert list(ChromaAdapter(FakeCollection()).iter_records()) == []


class TestMetadataMerging:
    def test_patch_preserves_keys_it_does_not_mention(self, collection):
        """Chroma's update replaces metadata, so the adapter must merge first."""
        ChromaAdapter(collection).update_metadata({"hot": {COLD_FLAG: False}})
        stored = collection.get(ids=["hot"])["metadatas"][0]
        assert stored["source"] == "slack"
        assert stored["created_at"]
        assert stored[COLD_FLAG] is False

    def test_none_becomes_empty_string_and_datetimes_become_iso(self, collection, now):
        ChromaAdapter(collection).update_metadata(
            {"hot": {"nothing": None, "when": now, "n": 3}}
        )
        stored = collection.get(ids=["hot"])["metadatas"][0]
        assert stored["nothing"] == ""
        assert stored["when"] == now.isoformat()
        assert stored["n"] == 3

    def test_records_deleted_since_the_read_are_skipped(self, collection):
        adapter = ChromaAdapter(collection)
        collection.delete(ids=["warm"])
        adapter.update_metadata({"warm": {COLD_FLAG: True}, "hot": {COLD_FLAG: True}})
        assert collection.update_calls[-1]["ids"] == ["hot"]

    def test_empty_patches_are_not_written(self, collection):
        ChromaAdapter(collection).update_metadata({"hot": {}})
        assert collection.update_calls == []

    def test_writes_are_batched(self, now):
        big = FakeCollection([record(f"m-{i}", idle_days=1, now=now) for i in range(5)])
        adapter = ChromaAdapter(big, write_batch_size=2)
        adapter.update_metadata({f"m-{i}": {COLD_FLAG: True} for i in range(5)})
        assert [len(call["ids"]) for call in big.update_calls] == [2, 2, 1]

    def test_deletes_are_batched(self, now):
        big = FakeCollection([record(f"m-{i}", idle_days=1, now=now) for i in range(5)])
        ChromaAdapter(big, write_batch_size=2).delete([f"m-{i}" for i in range(5)])
        assert [len(call) for call in big.delete_calls] == [2, 2, 1]


class TestEndToEnd:
    def test_sweep_through_the_adapter_evicts_and_demotes(self, collection, now):
        engine = DecayEngine(
            ChromaAdapter(collection),
            DecayPolicy(half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1),
        )
        report = engine.sweep(dry_run=False, now=now)

        assert report.evicted_ids == ["dead"]
        assert collection.count() == 2
        warm = collection.get(ids=["warm"])["metadatas"][0]
        assert warm[COLD_FLAG] is True
        assert warm["source"] == "email"  # untouched by the demotion

    def test_dry_run_writes_nothing_to_the_collection(self, collection, now):
        DecayEngine(ChromaAdapter(collection), DecayPolicy()).sweep(now=now)
        assert collection.update_calls == []
        assert collection.delete_calls == []
        assert collection.count() == 3

    def test_no_metadata_value_ever_violates_chromas_type_rules(self, collection, now):
        """FakeCollection raises on a non-scalar, so a clean sweep proves the point."""
        engine = DecayEngine(ChromaAdapter(collection), DecayPolicy(), write_scores=True)
        engine.sweep(dry_run=False, now=now)
        for call in collection.update_calls:
            for metadata in call["metadatas"]:
                assert all(isinstance(v, SCALARS) for v in metadata.values())


class TestAgainstRealChroma:
    """Runs only where chromadb is installed; proves the adapter against the real API."""

    def test_roundtrip_with_a_live_collection(self, tmp_path, now):
        chromadb = pytest.importorskip("chromadb")
        client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        collection = client.create_collection("memories")
        collection.add(
            ids=["hot", "dead"],
            documents=["a fresh memory", "an abandoned memory"],
            embeddings=[[0.1, 0.2], [0.3, 0.4]],
            metadatas=[
                {"created_at": (now - timedelta(days=2)).isoformat(),
                 "last_accessed_at": (now - timedelta(days=1)).isoformat(),
                 "source": "slack"},
                {"created_at": (now - timedelta(days=400)).isoformat(),
                 "last_accessed_at": (now - timedelta(days=380)).isoformat()},
            ],
        )
        engine = DecayEngine(
            ChromaAdapter(collection), DecayPolicy(), write_scores=True
        )
        report = engine.sweep(dry_run=False, now=now)

        assert report.evicted_ids == ["dead"]
        assert collection.count() == 1
        remaining = collection.get(ids=["hot"], include=["metadatas"])["metadatas"][0]
        assert remaining["source"] == "slack"
        assert remaining[COLD_FLAG] is False
