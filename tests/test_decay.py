"""Decay scoring: the curve, the signals it reads, and its refusals."""

from datetime import timedelta

import pytest

from conftest import record
from decay_engine import Action, DecayPolicy, FieldMap, MemoryRecord
from decay_engine.models import MemorySignals


class TestStrengthCurve:
    def test_one_half_life_idle_scores_exactly_one_half(self, now):
        policy = DecayPolicy(half_life=timedelta(days=30))
        verdict = policy.evaluate(record("a", idle_days=30), now=now)
        assert verdict.strength == pytest.approx(0.5)

    def test_two_half_lives_idle_scores_one_quarter(self, now):
        policy = DecayPolicy(half_life=timedelta(days=30))
        verdict = policy.evaluate(record("a", idle_days=60), now=now)
        assert verdict.strength == pytest.approx(0.25)

    def test_strength_decreases_monotonically_with_idle_time(self, now):
        policy = DecayPolicy()
        scores = [
            policy.evaluate(record("a", idle_days=days), now=now).strength
            for days in (1, 5, 20, 90, 365)
        ]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 < score <= 1.0 for score in scores)

    def test_future_timestamp_is_treated_as_brand_new(self, now):
        """Clock skew must not produce a strength above 1 or a negative idle time."""
        policy = DecayPolicy()
        verdict = policy.evaluate(record("a", idle_days=-5), now=now)
        assert verdict.strength == 1.0
        assert verdict.annotations["idle_seconds"] == 0.0


class TestUsageBoost:
    def test_access_count_stretches_the_half_life(self):
        policy = DecayPolicy(half_life=timedelta(days=30), usage_boost=0.5)
        cold = policy.effective_half_life(0)
        warm = policy.effective_half_life(50)
        assert cold == timedelta(days=30)
        assert warm > cold

    def test_frequently_used_memory_survives_where_unused_one_dies(self, now):
        """Tenure off, so this isolates the half-life stretch itself."""
        policy = DecayPolicy(
            half_life=timedelta(days=30), never_evict_above_accesses=None
        )
        unused = policy.evaluate(record("cold", idle_days=200), now=now)
        used = policy.evaluate(record("hot", idle_days=200, access_count=50), now=now)
        assert unused.action is Action.EVICT
        assert used.action is not Action.EVICT
        assert used.strength > unused.strength

    def test_usage_boost_zero_makes_scoring_pure_recency(self, now):
        policy = DecayPolicy(usage_boost=0.0)
        quiet = policy.evaluate(record("a", idle_days=45), now=now)
        busy = policy.evaluate(record("b", idle_days=45, access_count=999), now=now)
        assert quiet.strength == pytest.approx(busy.strength)

    def test_max_usage_multiplier_caps_the_stretch(self):
        policy = DecayPolicy(half_life=timedelta(days=10), max_usage_multiplier=2.0)
        assert policy.effective_half_life(10**9) == timedelta(days=20)


class TestProtections:
    def test_pinned_records_are_always_kept(self, now):
        policy = DecayPolicy()
        verdict = policy.evaluate(record("a", idle_days=10_000, pinned=True), now=now)
        assert verdict.action is Action.KEEP
        assert verdict.reason == "pinned"

    def test_respect_pinned_false_ignores_the_flag(self, now):
        policy = DecayPolicy(respect_pinned=False)
        verdict = policy.evaluate(record("a", idle_days=10_000, pinned=True), now=now)
        assert verdict.action is Action.EVICT

    def test_records_inside_the_grace_period_are_kept(self, now):
        """A memory written moments ago must survive even with no access history."""
        policy = DecayPolicy(half_life=timedelta(minutes=1), grace_period=timedelta(days=1))
        fresh = record("a", idle_days=0.2, age_days=0.2)
        verdict = policy.evaluate(fresh, now=now)
        assert verdict.action is Action.KEEP
        assert verdict.reason == "within grace period"

    def test_unreadable_timestamps_are_skipped_not_evicted(self, now):
        policy = DecayPolicy()
        verdict = policy.evaluate(
            MemoryRecord(id="a", metadata={"note": "no timestamps here"}), now=now
        )
        assert verdict.action is Action.SKIP
        assert verdict.strength is None

    def test_garbage_timestamp_is_skipped(self, now):
        policy = DecayPolicy()
        verdict = policy.evaluate(
            MemoryRecord(id="a", metadata={"created_at": "not-a-date"}), now=now
        )
        assert verdict.action is Action.SKIP


class TestTenure:
    """Usage stretches the half-life but cannot defeat exponential decay, so a
    well-used memory needs an explicit floor to survive long dormancy."""

    def test_a_heavily_used_dormant_memory_is_demoted_not_deleted(self, now):
        policy = DecayPolicy(half_life=timedelta(days=30), evict_below=0.1)
        verdict = policy.evaluate(record("name", idle_days=400, access_count=90), now=now)
        assert verdict.action is Action.DEMOTE
        assert verdict.strength < policy.evict_below
        assert "tenure" in verdict.reason

    def test_tenure_does_not_rescue_a_barely_used_memory(self, now):
        policy = DecayPolicy(
            half_life=timedelta(days=30), evict_below=0.1, never_evict_above_accesses=5
        )
        verdict = policy.evaluate(record("junk", idle_days=400, access_count=4), now=now)
        assert verdict.action is Action.EVICT

    def test_tenure_boundary_is_inclusive(self, now):
        policy = DecayPolicy(
            half_life=timedelta(days=30), evict_below=0.1, never_evict_above_accesses=5
        )
        verdict = policy.evaluate(record("edge", idle_days=400, access_count=5), now=now)
        assert verdict.action is Action.DEMOTE

    def test_tenure_can_be_disabled_for_pure_decay(self, now):
        policy = DecayPolicy(
            half_life=timedelta(days=30),
            evict_below=0.1,
            never_evict_above_accesses=None,
        )
        verdict = policy.evaluate(record("name", idle_days=400, access_count=90), now=now)
        assert verdict.action is Action.EVICT

    def test_negative_tenure_is_rejected(self):
        with pytest.raises(ValueError):
            DecayPolicy(never_evict_above_accesses=-1)


class TestThresholds:
    def test_bands_map_to_keep_demote_evict(self, now):
        policy = DecayPolicy(
            half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1
        )
        assert policy.evaluate(record("a", idle_days=5), now=now).action is Action.KEEP
        assert policy.evaluate(record("b", idle_days=60), now=now).action is Action.DEMOTE
        assert policy.evaluate(record("c", idle_days=200), now=now).action is Action.EVICT

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"half_life": timedelta(0)},
            {"half_life": timedelta(days=-1)},
            {"usage_boost": -0.1},
            {"demote_below": 0.1, "evict_below": 0.5},
            {"demote_below": 1.5},
            {"max_usage_multiplier": 0.5},
            {"grace_period": timedelta(days=-1)},
        ],
    )
    def test_invalid_configuration_is_rejected_at_construction(self, kwargs):
        with pytest.raises(ValueError):
            DecayPolicy(**kwargs)


class TestFieldMap:
    def test_custom_metadata_keys_are_honored(self, now):
        policy = DecayPolicy(
            field_map=FieldMap(
                created_at="born", last_accessed_at="touched", access_count="reads"
            )
        )
        stale = MemoryRecord(
            id="a",
            metadata={
                "born": (now - timedelta(days=400)).isoformat(),
                "touched": (now - timedelta(days=365)).isoformat(),
                "reads": 0,
            },
        )
        verdict = policy.evaluate(stale, now=now)
        assert verdict.action is Action.EVICT

    def test_epoch_seconds_and_camel_case_keys_work_by_default(self, now):
        policy = DecayPolicy()
        stale = MemoryRecord(
            id="a",
            metadata={"createdAt": (now - timedelta(days=400)).timestamp()},
        )
        assert policy.evaluate(stale, now=now).action is Action.EVICT

    def test_last_access_falls_back_to_creation_time(self, now):
        """Stores that do not track reads still get sensible aging."""
        signals = FieldMap().read(
            MemoryRecord(id="a", metadata={"created_at": now.isoformat()})
        )
        assert signals.last_accessed_at is None
        assert signals.reference_time == now

    def test_no_signals_at_all_yields_no_reference_time(self):
        signals = FieldMap().read(MemoryRecord(id="a", metadata={}))
        assert signals.reference_time is None
        assert isinstance(signals, MemorySignals)
