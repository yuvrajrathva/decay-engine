"""Engine-level scoping: forwarding, opacity, and the unscoped guarantee.

The engine never inspects a scope. These tests pin down what it does instead --
forward it when there is one, omit the keyword entirely when there is not, and
refuse rather than silently widen when an adapter cannot scope at all.
"""

from datetime import timedelta

import pytest

from conftest import record
from decay_engine import (
    DecayEngine,
    DecayPolicy,
    MemoryRecord,
    ScopeNotSupported,
)
from decay_engine.adapters.memory import InMemoryStore


@pytest.fixture
def policy():
    return DecayPolicy(half_life=timedelta(days=30), demote_below=0.5, evict_below=0.1)


@pytest.fixture
def store(now):
    records = []
    for tenant in ("acme", "globex"):
        for name, idle in (("hot", 1), ("dead", 300)):
            records.append(
                record(f"{tenant}-{name}", idle_days=idle,
                       extra={"tenant_id": tenant}, now=now)
            )
    return InMemoryStore(records)


class LegacyAdapter:
    """An adapter written before scoping existed. Must keep working unscoped."""

    def __init__(self, records):
        self._records = list(records)
        self.calls = []

    def iter_records(self, *, batch_size=256):
        self.calls.append(batch_size)
        return iter(self._records)

    def update_metadata(self, updates):
        pass

    def delete(self, ids):
        pass


class KwargSpy(InMemoryStore):
    """Records the exact keywords the engine passed, not just their values."""

    def __init__(self, records):
        super().__init__(records)
        self.calls = []

    def iter_records(self, **kwargs):
        self.calls.append(dict(kwargs))
        return super().iter_records(
            batch_size=kwargs.get("batch_size", 256), scope=kwargs.get("scope")
        )


class ScopeSpy:
    """Accepts any scope without interpreting it, to prove the core's opacity."""

    def __init__(self, records=()):
        self._records = list(records)
        self.seen = []

    def iter_records(self, *, batch_size=256, scope=None):
        self.seen.append(scope)
        return iter(self._records)

    def update_metadata(self, updates):
        pass

    def delete(self, ids):
        pass


class TestUnscopedIsUnchanged:
    def test_no_scope_sweeps_everything(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now)
        assert report.scanned == 4
        assert sorted(report.evicted_ids) == ["acme-dead", "globex-dead"]

    def test_no_scope_omits_the_keyword_entirely(self, store, policy, now):
        """Passing scope=None explicitly would break pre-scope adapters."""
        adapter = KwargSpy(list(store.iter_records()))
        DecayEngine(adapter, policy).sweep(now=now)
        assert adapter.calls == [{"batch_size": 256}]
        assert "scope" not in adapter.calls[0]

    def test_a_scope_adds_the_keyword(self, store, policy, now):
        adapter = KwargSpy(list(store.iter_records()))
        DecayEngine(adapter, policy).sweep(now=now, scope={"tenant_id": "acme"})
        assert adapter.calls == [
            {"batch_size": 256, "scope": {"tenant_id": "acme"}}
        ]

    def test_an_adapter_without_scope_support_still_sweeps(self, store, policy, now):
        adapter = LegacyAdapter(list(store.iter_records()))
        report = DecayEngine(adapter, policy).sweep(now=now)
        assert report.scanned == 4
        assert adapter.calls == [256]

    def test_report_scope_is_none_when_unscoped(self, store, policy, now):
        assert DecayEngine(store, policy).sweep(now=now).scope is None


class TestForwarding:
    def test_the_scope_is_passed_through_untouched(self, store, policy, now):
        """The engine must not normalize, copy, or interpret a scope."""
        sentinel = {"tenant_id": "acme"}
        adapter = ScopeSpy(list(store.iter_records()))
        DecayEngine(adapter, policy).sweep(now=now, scope=sentinel)
        assert adapter.seen[0] is sentinel  # same object, not a copy

    def test_an_arbitrary_object_is_accepted_by_the_core(self, store, policy, now):
        """Scopes are opaque: only the adapter decides what is meaningful."""
        class Whatever:
            pass

        marker = Whatever()
        adapter = ScopeSpy()
        DecayEngine(adapter, policy).sweep(now=now, scope=marker)
        assert adapter.seen == [marker]

    def test_evaluate_also_takes_a_scope(self, store, policy, now):
        report = DecayEngine(store, policy).evaluate(
            now=now, scope={"tenant_id": "globex"}
        )
        assert {v.record_id for v in report.verdicts} == {"globex-hot", "globex-dead"}

    def test_the_report_records_the_scope_it_ran_under(self, store, policy, now):
        scope = {"tenant_id": "acme"}
        assert DecayEngine(store, policy).sweep(now=now, scope=scope).scope == scope


class TestScopeNotSupported:
    def test_scoping_an_unscopable_adapter_raises(self, store, policy, now):
        """Silently sweeping everything could delete excluded records."""
        adapter = LegacyAdapter(list(store.iter_records()))
        with pytest.raises(ScopeNotSupported, match="LegacyAdapter"):
            DecayEngine(adapter, policy).sweep(now=now, scope={"tenant_id": "acme"})

    def test_it_raises_before_reading_anything(self, store, policy, now):
        adapter = LegacyAdapter(list(store.iter_records()))
        with pytest.raises(ScopeNotSupported):
            DecayEngine(adapter, policy).sweep(
                dry_run=False, now=now, scope={"tenant_id": "acme"}
            )
        assert adapter.calls == []

    def test_an_adapter_taking_kwargs_is_assumed_capable(self, store, policy, now):
        class Flexible:
            def __init__(self, records):
                self._records = list(records)
                self.seen = None

            def iter_records(self, **kwargs):
                self.seen = kwargs.get("scope")
                return iter(self._records)

            def update_metadata(self, updates):
                pass

            def delete(self, ids):
                pass

        adapter = Flexible(list(store.iter_records()))
        DecayEngine(adapter, policy).sweep(now=now, scope={"tenant_id": "acme"})
        assert adapter.seen == {"tenant_id": "acme"}


class TestScopedApply:
    def test_out_of_scope_records_are_not_touched(self, store, policy, now):
        DecayEngine(store, policy).sweep(
            dry_run=False, now=now, scope={"tenant_id": "acme"}
        )
        assert sorted(store.ids) == ["acme-hot", "globex-dead", "globex-hot"]

    def test_out_of_scope_records_are_absent_from_the_report(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now, scope={"tenant_id": "acme"})
        assert {v.record_id for v in report.verdicts} == {"acme-hot", "acme-dead"}
        assert report.counts == {"keep": 1, "demote": 0, "evict": 1, "skip": 0}

    def test_max_evictions_applies_within_the_scope_only(self, now):
        """The ceiling counts the scoped sweep's deletions, not the whole store."""
        records = [
            record(f"{tenant}-{i}", idle_days=500, extra={"tenant_id": tenant}, now=now)
            for tenant in ("acme", "globex")
            for i in range(5)
        ]
        store = InMemoryStore(records)
        engine = DecayEngine(store, DecayPolicy(), max_evictions=6)

        scoped = engine.sweep(now=now, scope={"tenant_id": "acme"})
        assert len(scoped.evicted_ids) == 5
        assert scoped.eviction_limit_exceeded is False

        unscoped = engine.sweep(now=now)
        assert len(unscoped.evicted_ids) == 10
        assert unscoped.eviction_limit_exceeded is True

    def test_scoped_demotion_writes_only_scoped_records(self, now, policy):
        store = InMemoryStore([
            record("acme-warm", idle_days=60, extra={"tenant_id": "acme"}, now=now),
            record("globex-warm", idle_days=60, extra={"tenant_id": "globex"}, now=now),
        ])
        DecayEngine(store, policy).sweep(
            dry_run=False, now=now, scope={"tenant_id": "acme"}
        )
        assert "decay_cold" in store.get("acme-warm").metadata
        assert "decay_cold" not in store.get("globex-warm").metadata


class TestInMemoryScopes:
    def test_a_predicate_scope_filters(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(
            now=now, scope=lambda r: r.id.startswith("globex")
        )
        assert {v.record_id for v in report.verdicts} == {"globex-hot", "globex-dead"}

    def test_a_mapping_scope_matches_all_keys(self, now, policy):
        store = InMemoryStore([
            record("a", idle_days=300, extra={"tenant_id": "acme", "kind": "note"}, now=now),
            record("b", idle_days=300, extra={"tenant_id": "acme", "kind": "task"}, now=now),
        ])
        report = DecayEngine(store, policy).sweep(
            now=now, scope={"tenant_id": "acme", "kind": "task"}
        )
        assert report.evicted_ids == ["b"]

    def test_a_scope_matching_nothing_sweeps_nothing(self, store, policy, now):
        report = DecayEngine(store, policy).sweep(now=now, scope={"tenant_id": "nope"})
        assert report.scanned == 0
        assert report.evicted_ids == []
        assert "swept 0 records" in report.summary()

    def test_an_unknown_scope_shape_is_refused(self, store, policy, now):
        with pytest.raises(TypeError, match="predicate or a mapping"):
            DecayEngine(store, policy).sweep(now=now, scope=42)

    def test_a_missing_metadata_key_simply_does_not_match(self, now, policy):
        store = InMemoryStore([MemoryRecord(id="bare", metadata={})])
        report = DecayEngine(store, policy).sweep(now=now, scope={"tenant_id": "acme"})
        assert report.scanned == 0

    def test_scope_narrows_before_the_policy_runs(self, now, policy):
        """Out-of-scope records never reach the policy at all."""
        seen = []
        store = InMemoryStore([
            record("acme-dead", idle_days=300, extra={"tenant_id": "acme"}, now=now),
            record("globex-dead", idle_days=300, extra={"tenant_id": "globex"}, now=now),
        ])
        engine = DecayEngine(store, policy, on_verdict=lambda v: seen.append(v.record_id))
        engine.sweep(now=now, scope={"tenant_id": "acme"})
        assert seen == ["acme-dead"]
