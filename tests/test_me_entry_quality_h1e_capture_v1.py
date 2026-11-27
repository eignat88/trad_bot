"""Issue #324 / H1-E.2 — prospective event capture: cursor, idempotency,
atomic checkpoint, redelivery, crash/restart, collision fail-closed.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CaptureCursor,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.classification import STATUS_DEFERRED
from app.research.me_entry_quality_h1e_v1.contracts import (
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    REASON_LINEAGE_HASH_MISMATCH,
    REASON_PRE_FREEZE,
    H1EFreeze,
    admit_source_row,
    ledger_identity,
)
from app.research.me_entry_quality_h1e_v1.ledger import (
    FaultInjectingStore,
    InMemoryH1EStore,
    SimulatedCrash,
)
from tests.h1e_v1_fixtures import (
    FIXTURE_FREEZE_US,
    FakeReader,
    make_classification_record,
    make_source_row,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
CFG = CaptureConfig(allow_fixture_freeze=True)


def row_signal_us(row):
    from app.research.me_entry_quality_h1c_v1.event_adapter import parse_utc_us
    return parse_utc_us(row["signal_time"], field_name="signal_time")


def _rows(n: int = 3, *, captured_stride_us: int = 1_000_000):
    return [
        make_source_row(
            signal_time_us=FIXTURE_FREEZE_US + 10_000_000 + i * 10_000_000,
            captured_us=FIXTURE_FREEZE_US + 100_000_000 + i * captured_stride_us,
            candle_open_ms=1_700_000_000_000 + i * 300_000,
        )
        for i in range(n)
    ]


def _classifier(event, prior_deferrals=0):
    return make_classification_record(event)


def test_fixture_freeze_refused_outside_offline_tests():
    with pytest.raises(ValueError, match="fixture freeze refused"):
        run_capture_cycle(
            reader=FakeReader([]),
            store=InMemoryH1EStore(activation=FREEZE),
            classifier=_classifier,
            freeze=FREEZE,
            cursor=CaptureCursor(),
            config=CaptureConfig(allow_fixture_freeze=False),
        )


def test_cycle_is_deterministic_and_fully_accounted():
    rows = _rows(5)
    reader = FakeReader(rows)
    store = InMemoryH1EStore(activation=FREEZE)
    report = run_capture_cycle(
        reader=reader, store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert report.committed
    assert report.read_count == 5
    assert report.accounted_count == report.read_count  # no silent skips
    assert report.admitted_count == 5
    assert report.cursor_after == CaptureCursor.after_rows(
        sorted(rows, key=lambda r: (r["captured_at"], r["source_event_key"])))
    assert len(store.entries()) == 5

    # determinism: the same reader/store state replays identically
    store2 = InMemoryH1EStore(activation=FREEZE)
    report2 = run_capture_cycle(
        reader=FakeReader(rows), store=store2, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert report.to_dict() == report2.to_dict()
    assert [e.payload for e in store.entries()] == [
        e.payload for e in store2.entries()]


def test_redelivery_is_idempotent_exactly_once_effect():
    rows = _rows(3)
    store = InMemoryH1EStore(activation=FREEZE)
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert first.committed and first.admitted_count == 3

    # full redelivery from the OLD cursor (at-least-once delivery)
    second = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert second.committed
    assert second.duplicates == 3
    assert second.admitted_count == 0
    assert second.accounted_count == second.read_count
    assert len(store.entries()) == 3  # exactly-once effect
    for entry in store.entries():
        assert len(entry.classifications) == 1  # first capture kept verbatim


def test_pre_freeze_and_invalid_rows_are_explicit_rejections():
    rows = [
        make_source_row(signal_time_us=FIXTURE_FREEZE_US),  # == freeze
        make_source_row(
            signal_time_us=FIXTURE_FREEZE_US + 20_000_000,
            captured_us=FIXTURE_FREEZE_US + 200_000_000,
            candle_open_ms=1_800_000_000_000,
            corrupt="lineage_hash",
        ),
    ]
    store = InMemoryH1EStore(activation=FREEZE)
    report = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert report.read_count == 2
    assert report.admitted_count == 0
    assert len(report.rejections) == 2
    reasons = sorted(r.reason for r in report.rejections)
    assert reasons == sorted([REASON_PRE_FREEZE, REASON_LINEAGE_HASH_MISMATCH])
    assert len(store.entries()) == 0
    assert len(store.reject_events) == 2  # durable rejection evidence


def test_out_of_order_signal_times_are_delivered_in_capture_order():
    # signal times DESCEND while captured_at ASCENDS — delivery follows the
    # CAPTURE order cursor, never signal_time
    rows = [
        make_source_row(
            signal_time_us=FIXTURE_FREEZE_US + 300_000_000 - i * 100_000_000,
            captured_us=FIXTURE_FREEZE_US + 500_000_000 + i * 1_000_000,
            candle_open_ms=1_900_000_000_000 + i * 300_000,
        )
        for i in range(3)
    ]
    store = InMemoryH1EStore(activation=FREEZE)
    seen: list[int] = []

    def ordered_classifier(event, prior_deferrals=0):
        seen.append(event.signal_time_us)
        return make_classification_record(event)

    report = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=ordered_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert report.admitted_count == 3
    # classification order == capture order (ascending captured_at) while
    # the signal times arrive in DESCENDING order
    assert seen == [row_signal_us(r) for r in
                    sorted(rows, key=lambda r: r["captured_at"])]
    assert seen == sorted(seen, reverse=True)


def test_identity_collision_is_isolated_fail_closed():
    row_a = make_source_row(
        signal_time_us=FIXTURE_FREEZE_US + 10_000_000,
        captured_us=FIXTURE_FREEZE_US + 100_000_000,
        candle_open_ms=1_700_000_000_000,
    )
    # same key/identity, DIFFERENT preserved facts (mutated lineage + hash)
    row_b = make_source_row(
        signal_time_us=FIXTURE_FREEZE_US + 10_000_000,
        captured_us=FIXTURE_FREEZE_US + 100_000_000,
        candle_open_ms=1_700_000_000_000,
        lineage_overrides={"source_scanner_version": "MUTATED"},
    )
    assert row_a["source_event_key"] == row_b["source_event_key"]
    assert row_a["lineage_hash"] != row_b["lineage_hash"]

    store = InMemoryH1EStore(activation=FREEZE)
    first = run_capture_cycle(
        reader=FakeReader([row_a]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert first.admitted_count == 1

    second = run_capture_cycle(
        reader=FakeReader([row_b]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert second.committed
    assert len(second.conflicts) == 1
    assert second.conflicts[0].delivered_preserved_hash != (
        second.conflicts[0].existing_preserved_hash)
    assert len(store.conflict_events) == 1
    # original record preserved VERBATIM — never overwritten
    identity = ledger_identity(row_a["source_event_key"])
    entry = store.lookup(identity)
    assert entry.payload["lineage_hash"] == row_a["lineage_hash"]
    assert entry.preserved_hash == second.conflicts[0].existing_preserved_hash


def test_commit_crash_is_all_or_nothing_then_restart_recovers():
    rows = _rows(3)
    store = FaultInjectingStore(activation=FREEZE)
    store.fail_next_commit = True
    crashed = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert not crashed.committed
    assert crashed.committed_count == 0
    assert "injected commit crash" in crashed.commit_error
    # NOTHING applied — not the ledger, not the checkpoint
    assert store.entries() == ()
    assert store.checkpoint == CaptureCursor()

    # restart (at-least-once redelivery from the unadvanced checkpoint)
    restarted = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert restarted.committed
    assert restarted.admitted_count == 3
    assert len(store.entries()) == 3
    assert store.checkpoint == restarted.cursor_after
    for entry in store.entries():
        assert len(entry.classifications) == 1


def test_classifier_crash_between_read_and_commit_leaves_no_effects():
    rows = _rows(2)
    store = InMemoryH1EStore(activation=FREEZE)
    calls = {"n": 0}

    def flaky(event, prior_deferrals=0):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("classifier crash")
        return make_classification_record(event)

    with pytest.raises(RuntimeError, match="classifier crash"):
        run_capture_cycle(
            reader=FakeReader(rows), store=store, classifier=flaky,
            freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert store.entries() == ()
    assert store.checkpoint == CaptureCursor()

    restarted = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert restarted.committed
    assert len(store.entries()) == 2
    unchanged = [e.final_classification for e in store.entries()]
    assert all(c is not None for c in unchanged)


def test_retry_on_interrupted_transaction_yields_identical_outcome():
    rows = _rows(2)
    store = FaultInjectingStore(activation=FREEZE)
    store.fail_next_commit = True
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    outcome_a = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    # a later full redelivery is an idempotent no-op
    outcome_b = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert outcome_a.admitted_count == 2
    assert outcome_b.duplicates == 2
    classifications = [
        e.final_classification for e in store.entries()]
    assert len(classifications) == 2
    # unchanged outcome across the interrupted transaction
    store2 = InMemoryH1EStore(activation=FREEZE)
    clean = run_capture_cycle(
        reader=FakeReader(rows), store=store2, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert clean.committed
    assert sorted(map(str, classifications)) == sorted(
        str(e.final_classification) for e in store2.entries())


def test_reader_order_violation_fails_closed():
    rows = _rows(2)

    class StrictOrderReader:
        """Returns rows VERBATIM (violates the capture-order contract)."""

        def __init__(self, delivered):
            self.delivered = list(delivered)

        def read_after(self, cursor, *, limit, ceiling_captured_us=None):
            return self.delivered[: int(limit)]

    reader = StrictOrderReader(list(reversed(rows)))
    store = InMemoryH1EStore(activation=FREEZE)
    with pytest.raises(ValueError, match="H1E_READER_ORDER_VIOLATION"):
        run_capture_cycle(
            reader=reader, store=store, classifier=_classifier,
            freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert store.entries() == ()


def test_batch_bound_is_respected_and_cursor_advances_by_batch():
    rows = _rows(5)
    store = InMemoryH1EStore(activation=FREEZE)
    cfg = CaptureConfig(allow_fixture_freeze=True, batch_limit=2)
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg)
    assert first.read_count == 2
    assert first.cursor_after > CaptureCursor()
    second = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=first.cursor_after, config=cfg)
    assert second.read_count == 2
    assert second.cursor_before == first.cursor_after
    assert second.cursor_after > first.cursor_after
    assert len(store.entries()) == 4


def test_deferred_classification_is_queued_and_retried_not_lost():
    """B1: a DEFERRED event lives in the durable pending queue — redelivery
    is idempotent and the retry is driven by the queue, independent of the
    source cursor."""
    rows = _rows(1)
    store = InMemoryH1EStore(activation=FREEZE)
    state = {"defer": True}
    cfg = CaptureConfig(allow_fixture_freeze=True, retry_backoff_us=1_000_000)

    def flaky_classifier(event, prior_deferrals=0):
        if state["defer"]:
            return make_classification_record(
                event, eligible=False, status=STATUS_DEFERRED)
        return make_classification_record(event)

    t0 = FIXTURE_FREEZE_US + 500_000_000
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=flaky_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=t0)
    assert first.deferred_keys == (rows[0]["source_event_key"],)
    identity = ledger_identity(rows[0]["source_event_key"])
    assert store.current_status(identity) == STATUS_DEFERRED
    queued = store.pending_retries()
    assert [p.ledger_identity for p in queued] == [identity]
    assert queued[0].attempt_count == 1
    assert queued[0].deferred_reason != ""
    assert queued[0].next_attempt_us == t0 + 1_000_000

    # redelivery BEFORE the retry is due: idempotent duplicate, the
    # classification is NOT re-executed and the queue is untouched
    redelivered = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=flaky_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=t0 + 500_000)
    assert redelivered.duplicates == 1
    assert redelivered.admitted_count == 0
    assert redelivered.retried_keys == ()
    assert len(store.pending_retries()) == 1

    # due: the durable queue re-processes the event WITHOUT redelivery
    state["defer"] = False
    retried = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=flaky_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=t0 + 2_000_000)
    assert retried.retried_keys == (rows[0]["source_event_key"],)
    entry = store.lookup(identity)
    assert entry.current_status == "CLASSIFIED"
    assert entry.attempt_count == 2
    assert len(entry.classifications) == 2  # append-only evidence
    assert store.pending_retries() == ()

    # redelivery AFTER finalization: no re-classification, no ledger
    # duplicates (exactly-once effect)
    final_redelivery = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=flaky_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg,
        now_us=t0 + 3_000_000)
    assert final_redelivery.duplicates == 1
    assert final_redelivery.retried_keys == ()
    assert len(store.lookup(identity).classifications) == 2


def test_cycle_report_keeps_reconciliation_contract_with_b4_diagnostics():
    """B4 diagnostics are ADDITIVE — the reconciliation contract is intact."""
    from app.research.me_entry_quality_h1e_v1.classification import STATUS_DEFERRED as DEF

    rows = [
        make_source_row(
            signal_time_us=FIXTURE_FREEZE_US + 10_000_000,
            captured_us=FIXTURE_FREEZE_US + 100_000_000,
            candle_open_ms=1_700_000_000_000,
        )
    ]
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, retry_batch_limit=1)

    def defer_classifier(event, prior_deferrals=0):
        return make_classification_record(
            event, eligible=False, status=DEF,
            non_eligible_codes=("H1E_HTF_LIMITER_DENIED",))

    store = InMemoryH1EStore(activation=FREEZE)
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=FIXTURE_FREEZE_US + 200_000_000)
    report = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows), config=cfg,
        now_us=FIXTURE_FREEZE_US + 300_000_000)
    # pre-existing reconciliation contract: unchanged
    assert report.accounted_count == report.read_count
    assert set(report.to_dict()) >= {
        "version", "cursor_before", "cursor_after", "read_count",
        "admitted_count", "rejections", "duplicates", "conflicts",
        "deferred_keys", "committed_count", "committed", "commit_error",
        "retried_keys", "retry_terminal_keys", "retry_skipped_keys",
    }
    # B4 diagnostics: exact queue accounting, derived from durable state
    assert report.pending_due_total == 1
    assert report.pending_processed == 1
    assert report.pending_remaining == len(store.pending_retries()) == 1
    assert report.retry_batch_limit_reached is False
    assert report.to_dict()["pending_remaining"] == report.pending_remaining
