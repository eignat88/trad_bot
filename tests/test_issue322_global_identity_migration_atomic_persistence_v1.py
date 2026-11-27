"""Issue #322 — atomic alias + dual-record persistence contract (review B2).

One capture is ONE logical change: an :class:`IdentityBatch` (alias edge +
dual-identity record) delivered through a single ``save_identity_batch`` call.
ALL_OR_NOTHING: a failed batch leaves no trace anywhere (no in-memory alias, no
persisted row); delivery is idempotent under redelivery, safe under concurrent
capture, and crash/restart recovery converges on the same identity.

Fault injection lives entirely in the in-test reference store — no database,
no network, no production writes.
"""

import threading

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    PHASE_1_SHADOW_DUAL_IDENTITY,
    AliasEdge,
    AliasIntegrityError,
    DualIdentityRecord,
    IdentityBatch,
    IdentityPersistenceError,
    MigrationConfig,
    MigrationIdentityCore,
    SignalIdentityAliasMap,
    stable_source_signal_id,
)

CANDLE = 1795680000000
SCANNER = "SUPPORT_RESISTANCE_REACTION"
ISO_TIME = "2026-11-25T10:00:07.123456+00:00"


def phase1_config():
    return MigrationConfig(
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
        families_enabled=frozenset({"srr"}),
        dual_write=True,
    )


class AtomicIdentityStore:
    """Reference ALL_OR_NOTHING, idempotent, crash-safe store (test stub).

    Writes are staged and committed atomically; fault injection
    (``fail_alias_write`` / ``fail_record_write`` / ``crash_before_commit``)
    aborts BEFORE the commit point, discarding staged rows — exactly the
    guarantee a real transactional hook must provide.
    """

    def __init__(self):
        self.edges = {}      # (stable_key, legacy_key) -> family_slug
        self.records = {}    # idempotency_key -> DualIdentityRecord
        self.calls = 0
        self.fail_alias_write = False
        self.fail_record_write = False
        self.crash_before_commit = False
        self._lock = threading.Lock()

    def save_identity_batch(self, batch):
        with self._lock:
            self.calls += 1
            if batch.idempotency_key in self.records:
                return  # idempotent redelivery: no-op, no duplicate
            # -- stage (nothing visible yet) --
            staged_edges = {}
            for edge in batch.alias_edges:
                key = (edge.stable_key, edge.legacy_key)
                if self.fail_alias_write:
                    raise RuntimeError("alias row write failed")
                staged_edges[key] = edge.family_slug
            if self.fail_record_write:
                raise RuntimeError("dual-record row write failed")
            staged_record = {batch.idempotency_key: batch.record}
            if self.crash_before_commit:
                raise RuntimeError("process crash before commit")
            # -- atomic commit --
            self.edges.update(staged_edges)
            self.records.update(staged_record)

    def rebuild_alias_map(self) -> SignalIdentityAliasMap:
        """Crash/restart recovery: replay committed edges into a fresh map."""
        restored = SignalIdentityAliasMap()
        for (stable_key, legacy_key) in self.edges:
            restored.add_alias(stable_key, legacy_key)
        return restored


def capture(core, candle=CANDLE, **kwargs):
    return core.capture_dual(
        SCANNER, "BTCUSDT", "SHORT", "5m", candle, **kwargs
    )


def make_core(store):
    return MigrationIdentityCore(phase1_config(), persistence=store)


# --- one capture = one atomic operation ----------------------------------

def test_one_atomic_batch_call_per_capture():
    store = AtomicIdentityStore()
    core = make_core(store)
    rec = capture(core, legacy_signal_time=ISO_TIME)
    assert store.calls == 1  # a single logical operation, not two writes
    assert store.edges == {(rec.stable_source_signal_id, rec.legacy_source_signal_id): "srr"}
    assert list(store.records.values()) == [rec]
    assert len(core.aliases) == 1


# --- fault injection: alias / record write failure => full rollback -------

def test_alias_write_failure_rolls_back_everything():
    store = AtomicIdentityStore()
    store.fail_alias_write = True
    core = make_core(store)
    with pytest.raises(IdentityPersistenceError):
        capture(core, legacy_signal_time=ISO_TIME)
    assert store.edges == {} and store.records == {}  # no orphan alias, no record
    assert len(core.aliases) == 0  # in-memory alias map rolled back too
    # retry after the fault clears converges on the SAME identity
    store.fail_alias_write = False
    rec = capture(core, legacy_signal_time=ISO_TIME)
    assert len(store.edges) == 1 and len(store.records) == 1
    assert core.resolve_legacy(rec.legacy_source_signal_id) == rec.stable_source_signal_id


def test_dual_record_write_failure_rolls_back_staged_alias():
    store = AtomicIdentityStore()
    store.fail_record_write = True  # alias rows already staged, record fails
    core = make_core(store)
    with pytest.raises(IdentityPersistenceError):
        capture(core, legacy_signal_time=ISO_TIME)
    assert store.edges == {}  # staged alias row rolled back, never orphaned
    assert store.records == {}
    assert len(core.aliases) == 0
    store.fail_record_write = False
    rec = capture(core, legacy_signal_time=ISO_TIME)
    assert set(store.edges) == {(rec.stable_source_signal_id, rec.legacy_source_signal_id)}


def test_failure_leaves_no_partial_state_across_repeated_attempts():
    store = AtomicIdentityStore()
    core = make_core(store)
    for fault in ("alias", "record", "crash"):
        store.fail_alias_write = fault == "alias"
        store.fail_record_write = fault == "record"
        store.crash_before_commit = fault == "crash"
        with pytest.raises(IdentityPersistenceError):
            capture(core, legacy_signal_time=ISO_TIME)
        assert store.edges == {} and store.records == {}
        assert len(core.aliases) == 0
    store.fail_alias_write = store.fail_record_write = store.crash_before_commit = False
    capture(core, legacy_signal_time=ISO_TIME)
    assert len(store.edges) == 1 and len(store.records) == 1 and len(core.aliases) == 1


# --- crash between operations / crash recovery ---------------------------

def test_crash_before_commit_leaves_no_partial_save():
    store = AtomicIdentityStore()
    store.crash_before_commit = True
    core = make_core(store)
    with pytest.raises(IdentityPersistenceError):
        capture(core, legacy_signal_time=ISO_TIME)
    assert store.edges == {} and store.records == {}
    assert store.calls == 1  # exactly one operation existed — no "between" window


def test_restart_after_crash_recovers_with_unchanged_identity():
    # NOTE (review D4): "restart" here is a fresh core over the same store in
    # one process.  Cross-PYTHONHASHSEED (true process) stability of the
    # STABLE id is asserted in ..._subprocess_v1.py; a real restart under a
    # new hash seed legitimately re-derives a DIFFERENT legacy id for the same
    # signal (one stable key owns many legacy keys) and aliases it — never
    # re-points history.
    store = AtomicIdentityStore()
    store.crash_before_commit = True
    with pytest.raises(IdentityPersistenceError):
        capture(make_core(store), legacy_signal_time=ISO_TIME)
    # process "restarts": a fresh core (empty in-memory alias map) over the
    # same store; redelivery must converge on the same identity, no duplicates
    store.crash_before_commit = False
    core2 = make_core(store)
    rec = capture(core2, legacy_signal_time=ISO_TIME)
    assert len(store.edges) == 1 and len(store.records) == 1
    assert core2.resolve_legacy(rec.legacy_source_signal_id) == rec.stable_source_signal_id


def test_restart_rebuilds_alias_map_without_identity_change():
    # See the review-D4 note on test_restart_after_crash_recovers_with_
    # unchanged_identity: in-process restart simulation; cross-seed stable-id
    # stability lives in ..._subprocess_v1.py.
    store = AtomicIdentityStore()
    rec1 = capture(make_core(store), legacy_signal_time=ISO_TIME)
    # restart: fresh core, alias map replayed from the committed store
    core2 = MigrationIdentityCore(
        phase1_config(), alias_map=store.rebuild_alias_map(), persistence=store
    )
    assert core2.resolve_legacy(rec1.legacy_source_signal_id) == rec1.stable_source_signal_id
    rec2 = capture(core2, legacy_signal_time=ISO_TIME)  # re-scan after restart
    assert rec2.stable_source_signal_id == rec1.stable_source_signal_id
    assert rec2.legacy_source_signal_id == rec1.legacy_source_signal_id
    assert len(store.edges) == 1 and len(store.records) == 1  # no duplication


# --- idempotent redelivery ----------------------------------------------

def test_redelivery_does_not_duplicate():
    store = AtomicIdentityStore()
    core = make_core(store)
    rec1 = capture(core, legacy_signal_time=ISO_TIME)
    rec2 = capture(core, legacy_signal_time=ISO_TIME)
    rec3 = capture(core, legacy_signal_time=ISO_TIME)
    assert rec1.canonical_key_hex == rec2.canonical_key_hex == rec3.canonical_key_hex
    assert len(store.records) == 1 and len(store.edges) == 1
    assert len(core.aliases) == 1
    assert core.resolve_stable(rec1.stable_source_signal_id) == (
        rec1.legacy_source_signal_id,
    )


def test_redelivery_with_confirmed_historical_id_adds_one_alias_only():
    store = AtomicIdentityStore()
    core = make_core(store)
    rec1 = capture(core, legacy_signal_time=ISO_TIME)
    rec2 = capture(core, legacy_source_signal_id=987_654_321)  # historical twin
    assert rec2.stable_source_signal_id == rec1.stable_source_signal_id
    assert len(core.aliases) == 2  # one stable key may own many legacy keys
    assert sorted(core.resolve_stable(rec1.stable_source_signal_id)) == sorted(
        (rec1.legacy_source_signal_id, 987_654_321)
    )
    assert len(store.edges) == 2 and len(store.records) == 2  # distinct logical changes


# --- concurrent capture --------------------------------------------------

def test_concurrent_same_signal_single_identity():
    store = AtomicIdentityStore()
    core = make_core(store)
    results, errors = [], []

    def worker():
        try:
            results.append(capture(core, legacy_signal_time=ISO_TIME))
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert {r.stable_source_signal_id for r in results} == {results[0].stable_source_signal_id}
    assert {r.legacy_source_signal_id for r in results} == {results[0].legacy_source_signal_id}
    assert len(store.edges) == 1 and len(store.records) == 1
    assert len(core.aliases) == 1


def test_concurrent_same_signal_across_cores_no_conflicting_identity():
    store = AtomicIdentityStore()
    cores = [make_core(store) for _ in range(2)]
    results, errors = [], []

    def worker(core):
        try:
            results.append(capture(core, legacy_signal_time=ISO_TIME))
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(cores[i % 2],)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len({r.stable_source_signal_id for r in results}) == 1
    assert len({r.legacy_source_signal_id for r in results}) == 1
    assert len(store.edges) == 1 and len(store.records) == 1


def test_concurrent_different_signals_all_committed():
    store = AtomicIdentityStore()
    core = make_core(store)
    results, errors = [], []

    def worker(idx):
        try:
            results.append(core.capture_dual(
                SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE + idx * 300_000,
                # each detection carries its own detected_at (frozen key input)
                legacy_signal_time=f"2026-11-25T10:{idx:02d}:07.123456+00:00",
            ))
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len({r.stable_source_signal_id for r in results}) == 6
    assert len(store.edges) == 6 and len(store.records) == 6
    assert len(core.aliases) == 6


# --- batch invariant: no orphan alias, no record without alias ------------

def _record(stable, legacy):
    return DualIdentityRecord(
        family_slug="srr",
        identity_version="issue322:srr:v1",
        legacy_source_signal_id=legacy,
        stable_source_signal_id=stable,
        canonical_key_hex="0" * 64,
        scanner_name=SCANNER,
        symbol="BTCUSDT",
        direction="SHORT",
        entry_timeframe="5m",
        signal_candle_open_time=CANDLE,
        observed_at_us="2026-11-25T10:00:07.123456+00:00",
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
    )


STABLE = -(2**31 + 1)


def test_batch_rejects_record_without_alias():
    with pytest.raises(AliasIntegrityError, match="BATCH_WITHOUT_ALIAS"):
        IdentityBatch(record=_record(STABLE, 1), alias_edges=()).validate()
    with pytest.raises(AliasIntegrityError, match="RECORD_WITHOUT_MATCHING_ALIAS"):
        IdentityBatch(
            record=_record(STABLE, 1),
            alias_edges=(AliasEdge(STABLE, 2, "srr"),),
        ).validate()


def test_batch_rejects_orphan_alias_edge():
    with pytest.raises(AliasIntegrityError, match="ORPHAN_ALIAS_EDGE"):
        IdentityBatch(
            record=_record(STABLE, 1),
            alias_edges=(AliasEdge(STABLE - 1, 1, "srr"),),
        ).validate()
    with pytest.raises(AliasIntegrityError, match="BATCH_FAMILY_SLUG_MISMATCH"):
        IdentityBatch(
            record=_record(STABLE, 1),
            alias_edges=(AliasEdge(STABLE, 1, "vc"),),
        ).validate()


def test_batch_rejects_invalid_edge_keys():
    with pytest.raises(AliasIntegrityError):
        IdentityBatch(
            record=_record(STABLE, 1),
            alias_edges=(AliasEdge(-(2**30), 1, "srr"),),  # not below -2**31
        ).validate()
    with pytest.raises(AliasIntegrityError):
        IdentityBatch(
            record=_record(STABLE, 1),
            alias_edges=(AliasEdge(STABLE, 2**31, "srr"),),  # outside legacy band
        ).validate()


def test_batch_idempotency_key_is_deterministic():
    batch = IdentityBatch(
        record=_record(STABLE, 1),
        alias_edges=(AliasEdge(STABLE, 1, "srr"),),
    )
    twin = IdentityBatch(
        record=_record(STABLE, 1),
        alias_edges=(AliasEdge(STABLE, 1, "srr"),),
    )
    other = IdentityBatch(
        record=_record(STABLE, 2),
        alias_edges=(AliasEdge(STABLE, 2, "srr"),),
    )
    batch.validate()
    assert batch.idempotency_key == twin.idempotency_key
    assert batch.idempotency_key != other.idempotency_key


# --- split-write hooks are outside the contract --------------------------

def test_split_write_hook_is_rejected_at_construction():
    class LegacySplitHook:
        def save_alias(self, stable_key, legacy_key, family_slug):
            pass

        def save_dual_record(self, record):
            pass

    with pytest.raises(TypeError, match="save_identity_batch"):
        MigrationIdentityCore(phase1_config(), persistence=LegacySplitHook())


def test_hook_without_atomic_entrypoint_is_rejected():
    class EmptyHook:
        pass

    with pytest.raises(TypeError, match="save_identity_batch"):
        MigrationIdentityCore(phase1_config(), persistence=EmptyHook())


# --- historical OOS compatibility ----------------------------------------

def test_preexisting_historical_edges_are_never_repointed():
    store = AtomicIdentityStore()
    frozen_stable = stable_source_signal_id(SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE)
    frozen_legacy = 1_111_222_333
    store.edges[(frozen_stable, frozen_legacy)] = "srr"
    core = MigrationIdentityCore(
        phase1_config(), alias_map=store.rebuild_alias_map(), persistence=store
    )
    # same candle, re-delivered: converges on the historical alias, no rewrite
    rec = capture(
        core, legacy_source_signal_id=frozen_legacy,
    )
    assert rec.stable_source_signal_id == frozen_stable
    assert core.resolve_legacy(frozen_legacy) == frozen_stable
    with pytest.raises(AliasIntegrityError):
        core.aliases.add_alias(frozen_stable - 1, frozen_legacy)  # re-point forbidden
    assert core.resolve_legacy(frozen_legacy) == frozen_stable
