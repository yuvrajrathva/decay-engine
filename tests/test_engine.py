"""Sweep orchestration: what a sweep decides versus what it changes."""

from datetime import timedelta

import pytest

from conftest import record
from decay_engine import (
    COLD_FLAG,
    STRENGTH_FIELD,
    SWEPT_AT_FIELD,
    Action,
    DecayEngine,
    DecayPolicy,
    EvictionLimitExceeded,
    MemoryRecord,
)
from decay_engine.adapters.memory import InMemoryStore


@pytest.fixture
def policy():
    return DecayPolicy(
        half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1
    )


@pytest.fixture
def store(now):
    return InMemoryStore(
        [
            record("hot", idle_days=1, now=now),
            record("warm", idle_days=60, now=now),
            record("dead", idle_days=300, now=now),
            record("pinned", idle_days=300, pinned=True, now=now),
            MemoryRecord(id="unknown", metadata={"note": "no timestamps"}),
        ]
    )


class TestDryRun:
    def test_dry_run_is_the_default(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert report.applied is False
        assert len(store) == 5
        assert "dead" in store

    def test_dry_run_still_reports_every_decision(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert report.scanned == 5
        assert report.counts == {"keep": 2, "demote": 1, "evict": 1, "skip": 1}

    def test_dry_run_predicts_exactly_what_applying_does(self, store, policy, now):
        preview = DecayEngine(store, policy).sweep(now=now)
        applied = DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert preview.counts == applied.counts
        assert [v.record_id for v in preview.by_action(Action.EVICT)] == applied.evicted_ids

    def test_dry_run_populates_the_id_lists(self, store, policy, now):
        """Previewing via report.evicted_ids must not silently return []."""
        report = DecayEngine(store, policy).sweep(now=now)
        assert report.evicted_ids == ["dead"]
        assert report.demoted_ids == ["warm"]
        assert report.applied is False

    def test_id_lists_are_identical_across_dry_run_and_apply(self, policy, now):
        """Including promotions, which only the apply path used to record."""
        records = [
            record("hot", idle_days=1, now=now),
            record("warm", idle_days=60, now=now),
            record("dead", idle_days=300, now=now),
            record("revived", idle_days=1, extra={COLD_FLAG: True}, now=now),
        ]
        preview = DecayEngine(InMemoryStore(records), policy).sweep(now=now)
        applied = DecayEngine(InMemoryStore(records), policy).sweep(
            dry_run=False, now=now
        )
        assert preview.evicted_ids == applied.evicted_ids == ["dead"]
        assert preview.demoted_ids == applied.demoted_ids == ["warm"]
        assert preview.promoted_ids == applied.promoted_ids == ["revived"]

    def test_applied_is_the_only_flag_distinguishing_the_two(self, store, policy, now):
        preview = DecayEngine(store, policy).sweep(now=now)
        assert preview.applied is False
        assert preview.evicted_ids  # non-empty, yet nothing was deleted
        assert "dead" in store


class TestApply:
    def test_evicts_only_the_dead_record(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert report.applied is True
        assert report.evicted_ids == ["dead"]
        assert "dead" not in store
        assert len(store) == 4

    def test_demotion_writes_the_score_back_without_deleting(self, store, policy, now):
        DecayEngine(store, policy).sweep(dry_run=False, now=now)
        warm = store.get("warm")
        assert warm is not None
        assert warm.metadata[COLD_FLAG] is True
        assert warm.metadata[STRENGTH_FIELD] == pytest.approx(0.25, abs=1e-3)
        assert SWEPT_AT_FIELD in warm.metadata

    def test_demotion_preserves_preexisting_metadata(self, policy, now):
        store = InMemoryStore([record("warm", idle_days=60, extra={"source": "slack"}, now=now)])
        DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert store.get("warm").metadata["source"] == "slack"

    def test_skipped_and_pinned_records_are_never_touched(self, store, policy, now):
        DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert "unknown" in store
        assert "pinned" in store
        assert COLD_FLAG not in store.get("unknown").metadata

    def test_kept_records_are_not_written_unless_write_scores(self, store, policy, now):
        DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert STRENGTH_FIELD not in store.get("hot").metadata

    def test_write_scores_annotates_kept_records(self, store, policy, now):
        DecayEngine(store, policy, write_scores=True).sweep(dry_run=False, now=now)
        hot = store.get("hot")
        assert hot.metadata[COLD_FLAG] is False
        assert hot.metadata[STRENGTH_FIELD] > 0.9


class TestPromotion:
    def test_a_reused_memory_loses_its_cold_flag(self, policy, now):
        """The flag is cleared even with write_scores off, or the retrieval layer
        would keep down-ranking a memory that is being actively used."""
        store = InMemoryStore(
            [record("revived", idle_days=1, extra={COLD_FLAG: True}, now=now)]
        )
        report = DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert report.promoted_ids == ["revived"]
        assert store.get("revived").metadata[COLD_FLAG] is False

    def test_still_cold_record_stays_flagged(self, policy, now):
        store = InMemoryStore(
            [record("warm", idle_days=60, extra={COLD_FLAG: True}, now=now)]
        )
        report = DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert report.promoted_ids == []
        assert store.get("warm").metadata[COLD_FLAG] is True

    def test_string_cold_flag_from_a_store_is_understood(self, policy, now):
        """Chroma has no null, so flags round-trip as strings."""
        store = InMemoryStore(
            [record("revived", idle_days=1, extra={COLD_FLAG: "true"}, now=now)]
        )
        report = DecayEngine(store, policy).sweep(dry_run=False, now=now)
        assert report.promoted_ids == ["revived"]


class TestGuards:
    def test_max_evictions_blocks_the_sweep_before_any_write(self, now):
        store = InMemoryStore(
            [record(f"old-{i}", idle_days=500, now=now) for i in range(10)]
        )
        engine = DecayEngine(store, DecayPolicy(), max_evictions=3)
        with pytest.raises(EvictionLimitExceeded, match="would evict 10"):
            engine.sweep(dry_run=False, now=now)
        assert len(store) == 10

    def test_dry_run_reports_the_breach_instead_of_raising(self, now):
        """Previewing an over-limit sweep must still return the eviction list, so
        the half-life can be tuned against real data."""
        store = InMemoryStore(
            [record(f"old-{i}", idle_days=500, now=now) for i in range(10)]
        )
        report = DecayEngine(store, DecayPolicy(), max_evictions=3).sweep(now=now)
        assert report.eviction_limit_exceeded is True
        assert len(report.by_action(Action.EVICT)) == 10
        assert len(report.evicted_ids) == 10
        assert len(store) == 10

    def test_eviction_limit_at_the_boundary_is_allowed(self, now):
        store = InMemoryStore([record(f"old-{i}", idle_days=500, now=now) for i in range(3)])
        report = DecayEngine(store, DecayPolicy(), max_evictions=3).sweep(dry_run=False, now=now)
        assert len(report.evicted_ids) == 3

    @pytest.mark.parametrize("kwargs", [{"batch_size": 0}, {"max_evictions": -1}])
    def test_invalid_engine_configuration_is_rejected(self, store, kwargs):
        with pytest.raises(ValueError):
            DecayEngine(store, **kwargs)


class TestReporting:
    def test_on_verdict_sees_every_record(self, store, policy, now):
        seen = []
        DecayEngine(store, policy, on_verdict=seen.append).sweep(now=now)
        assert len(seen) == 5
        assert {v.record_id for v in seen} == set(store.ids)

    def test_every_verdict_carries_a_reason(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert all(verdict.reason for verdict in report.verdicts)
        assert all(verdict.policy == "decay" for verdict in report.verdicts)

    def test_skipped_records_are_easy_to_audit(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert [v.record_id for v in report.skipped] == ["unknown"]

    def test_summary_is_human_readable(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert report.summary() == (
            "swept 5 records (dry run): 2 keep, 1 demote, 1 evict, 1 skip"
        )

    def test_empty_store_sweeps_cleanly(self, policy, now):
        report = DecayEngine(InMemoryStore(), policy).sweep(dry_run=False, now=now)
        assert report.scanned == 0
        assert report.evicted_ids == []
        assert "swept 0 records" in report.summary()
