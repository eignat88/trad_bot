"""Issue #324 / H1-E — review fix B1: durable pending-retry for DEFERRED.

The pending queue is independent of the source cursor, survives restart,
is bounded (terminal fail-closed on budget exhaustion), is updated
atomically with the classification journal and checkpoint, and never
re-executes terminal or conflicted identities.  Retries run at the
ORIGINAL ``signal_time`` ASOF (no later candles).

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CaptureCursor,
    PendingRetry,
    PendingRetryUpdate,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    HtfLimiterDeniedError,
    NONELIGIBLE_RETRY_BUDGET_EXHAUSTED,
    RetryPolicy,
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
    classify_h1e_event,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    ledger_identity,
)
from app.research.me_entry_quality_h1e_v1.ledger import (
    FaultInjectingStore,
    InMemoryH1EStore,
)
from tests.h1e_v1_fixtures import (
    DAY_MS,
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    H4_MS,
    FakeReader,
    make_classification_record,
    make_source_row,
    timeframe_payload,
    zigzag_rows,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
CFG = CaptureConfig(allow_fixture_freeze=True, retry_backoff_us=1_000_000)
T0 = FIXTURE_FREEZE_US + 500_000_000


def _rows(n: int = 1):
    return [
        make_source_row(
            signal_time_us=FIXTURE_ASOF_US + i * 10_000_000,
            captured_us=T0 + i * 1_000_000,
            candle_open_ms=1_700_000_000_000 + i * 300_000,
        )
        for i in range(n)
    ]


def _defer_classifier(event, prior_deferrals=0):
    return make_classification_record(
        event, eligible=False, status=STATUS_DEFERRED,
        non_eligible_codes=("H1E_HTF_LIMITER_DENIED",))


def _ok_classifier(event, prior_deferrals=0):
    return make_classification_record(event)


def _event_key(rows, i: int = 0) -> str:
    return rows[i]["source_event_key"]


# ─── acceptance: DEFERRED → checkpoint advance → restart → re-processed ────


def test_deferred_survives_checkpoint_restart_and_is_reprocessed():
    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)

    store = InMemoryH1EStore(activation=FREEZE)
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    assert first.committed
    assert first.deferred_keys == (key,)
    assert store.checkpoint > CaptureCursor()  # checkpoint ADVANCED

    # restart: the durable state is reconstructed (new store object)
    store = InMemoryH1EStore.from_dict(store.to_dict(), activation=FREEZE)
    assert [p.ledger_identity for p in store.pending_retries()] == [identity]

    # the event is re-processed from the durable queue — the reader now
    # serves only NEW rows and the cursor has advanced past the event
    new_rows = [
        make_source_row(
            signal_time_us=FIXTURE_ASOF_US + 1_000_000_000,
            captured_us=T0 + 10_000_000,
            candle_open_ms=1_800_000_000_000,
        )
    ]
    second = run_capture_cycle(
        reader=FakeReader(new_rows), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=store.checkpoint, config=CFG, now_us=T0 + 2_000_000)
    assert second.committed
    assert second.retried_keys == (key,)
    entry = store.lookup(identity)
    assert entry.current_status == STATUS_CLASSIFIED
    assert entry.attempt_count == 2
    # the retry is NOT counted as a new delivery
    assert second.admitted_count == 1  # only the genuinely new row
    assert store.pending_retries() == ()


# ─── repeated HTF denial: bounded budget, reason preserved ─────────────────


def test_repeated_denial_respects_budget_and_preserves_reason():
    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)
    store = InMemoryH1EStore(activation=FREEZE)
    calls = {"n": 0}

    def denying_classifier(event, prior_deferrals=0):
        calls["n"] += 1
        return _defer_classifier(event, prior_deferrals)

    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=1_000_000, max_attempts=3)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=denying_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    assert store.current_status(identity) == STATUS_DEFERRED

    for round_no, now in enumerate((T0 + 2_000_000, T0 + 4_000_000), start=2):
        report = run_capture_cycle(
            reader=FakeReader([]), store=store, classifier=denying_classifier,
            freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
            config=cfg, now_us=now)
        assert report.retried_keys == (key,)
        pending = store.pending_retries()
        assert len(pending) == 1
        assert pending[0].attempt_count == round_no
        # the deferral reason is preserved in the durable queue
        assert pending[0].deferred_reason == "H1E_HTF_LIMITER_DENIED"

    # budget exhausted (3 attempts made) — terminal, no 4th classifier call
    terminal = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=denying_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 6_000_000)
    assert terminal.retry_terminal_keys == (key,)
    assert calls["n"] == 3  # bounded — the terminal record needs no call
    entry = store.lookup(identity)
    assert entry.current_status == STATUS_CLASSIFIED
    final = entry.final_classification
    assert final["eligible"] is False
    assert final["non_eligible_codes"] == [NONELIGIBLE_RETRY_BUDGET_EXHAUSTED]
    assert store.pending_retries() == ()

    # later cycles are quiet — the terminal event is never re-executed
    quiet = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=denying_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 8_000_000)
    assert quiet.retried_keys == ()
    assert calls["n"] == 3


def test_budget_exhaustion_never_loops_even_with_a_stubborn_classifier():
    rows = _rows(1)
    key = _event_key(rows)
    store = InMemoryH1EStore(activation=FREEZE)
    calls = {"n": 0}

    def stubborn(event, prior_deferrals=0):
        calls["n"] += 1
        return _defer_classifier(event, prior_deferrals)

    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, max_attempts=2)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=stubborn,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    for now in range(T0 + 1_000_000, T0 + 6_000_000, 1_000_000):
        run_capture_cycle(
            reader=FakeReader([]), store=store, classifier=stubborn,
            freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
            config=cfg, now_us=now)
    # 2 attempts total, then terminal — no endless retries across cycles
    assert calls["n"] == 2
    assert store.pending_retries() == ()
    assert store.current_status(ledger_identity(key)) == STATUS_CLASSIFIED


# ─── redelivery after finalization ─────────────────────────────────────────


def test_redelivery_after_terminal_is_idempotent_no_reclassification():
    rows = _rows(1)
    identity = ledger_identity(_event_key(rows))
    store = InMemoryH1EStore(activation=FREEZE)
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, max_attempts=1)
    # first cycle defers once; the next due retry exhausts max_attempts=1
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 1_000_000)
    entry = store.lookup(identity)
    assert entry.final_classification is not None

    journal_len = len(entry.journal)
    redelivered = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0 + 2_000_000)
    assert redelivered.duplicates == 1
    assert redelivered.admitted_count == 0
    assert redelivered.retried_keys == ()
    assert len(store.lookup(identity).journal) == journal_len


# ─── fixed-signal-time ASOF on retry (no later candles) ────────────────────


class _PhaseTransport:
    """Deny until flipped; then serve payloads INCLUDING future candles."""

    def __init__(self) -> None:
        self.deny = True
        self.calls: list[tuple[str, int]] = []
        rows4 = zigzag_rows(
            "BEARISH", timeframe_ms=H4_MS,
            first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS,
            extra_future_rows=3)
        rows1 = zigzag_rows(
            "NEUTRAL", timeframe_ms=DAY_MS,
            first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS,
            extra_future_rows=3)
        self.payloads = {
            "240": timeframe_payload(rows4, interval="240"),
            "D": timeframe_payload(rows1, interval="D"),
        }

    def fetch_htf_window(self, *, symbol, interval, asof_us, lookback=60):
        self.calls.append((interval, int(asof_us)))
        if self.deny:
            raise HtfLimiterDeniedError("limiter denied (offline)")
        return self.payloads[interval]


def test_retry_classifies_at_original_signal_time_no_later_candles():
    rows = [
        make_source_row(
            signal_time_us=FIXTURE_ASOF_US,
            captured_us=T0,
            candle_open_ms=1_700_000_000_000,
        )
    ]
    key = _event_key(rows)
    identity = ledger_identity(key)
    transport = _PhaseTransport()
    outcomes = []

    def classifier(event, prior_deferrals=0):
        outcome = classify_h1e_event(
            event, transport,
            retry_policy=RetryPolicy(max_attempts_per_cycle=1),
            prior_deferrals=prior_deferrals)
        outcomes.append(outcome)
        return outcome

    store = InMemoryH1EStore(activation=FREEZE)
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    assert first.deferred_keys == (key,)
    assert transport.deny is True

    # HTF becomes available LATER (wall-clock), with extra FUTURE candles
    transport.deny = False
    retried = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=CFG, now_us=T0 + 5_000_000)
    assert retried.retried_keys == (key,)
    entry = store.lookup(identity)
    final = entry.final_classification
    assert final["eligible"] is True
    # ASOF is the ORIGINAL signal_time — never the retry instant
    assert final["asof_us"] == FIXTURE_ASOF_US
    assert final["signal_time_us"] == FIXTURE_ASOF_US
    # the later candles were DROPPED, not used (no look-ahead on retry)
    retry_record = outcomes[-1].record
    assert retry_record.asof_us == FIXTURE_ASOF_US
    assert retry_record.h1d_diagnostics["dropped_future_rows_4h"] == 3
    assert retry_record.h1d_diagnostics["dropped_future_rows_1d"] == 3
    # transport was asked at the ORIGINAL asof only
    assert all(asof == FIXTURE_ASOF_US for _, asof in transport.calls)


# ─── terminal / conflict protection & self-heal ────────────────────────────


def test_stale_pending_for_terminal_identity_is_skipped_not_executed():
    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)
    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    assert store.current_status(identity) == STATUS_CLASSIFIED

    # a stale queue entry for an ALREADY terminal identity (corruption /
    # crash artifact) must never re-execute the classification
    stale = PendingRetry(
        ledger_identity=identity, source_event_key=key,
        attempt_count=1, deferred_reason="H1E_STALE",
        next_attempt_us=T0, preserved_hash=store.lookup(identity).preserved_hash)
    store.commit_batch([], store.checkpoint, pending=[
        PendingRetryUpdate(identity, stale)])
    calls = {"n": 0}

    def counting(event, prior_deferrals=0):
        calls["n"] += 1
        return make_classification_record(event)

    report = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=counting,
        freeze=FREEZE, cursor=store.checkpoint, config=CFG, now_us=T0 + 1_000_000)
    assert report.retry_skipped_keys == (key,)
    assert calls["n"] == 0
    assert store.pending_retries() == ()


def test_pending_identity_mismatch_fails_closed():
    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)
    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    # tamper the durable queue state (different preserved identity)
    pending = store.pending_retries()[0]
    tampered = PendingRetry(
        ledger_identity=pending.ledger_identity,
        source_event_key=pending.source_event_key,
        attempt_count=pending.attempt_count,
        deferred_reason=pending.deferred_reason,
        next_attempt_us=pending.next_attempt_us,
        preserved_hash="0" * 64)
    store.commit_batch([], store.checkpoint, pending=[
        PendingRetryUpdate(identity, tampered)])
    with pytest.raises(ValueError, match="H1E_PENDING_IDENTITY_MISMATCH"):
        run_capture_cycle(
            reader=FakeReader([]), store=store, classifier=_ok_classifier,
            freeze=FREEZE, cursor=store.checkpoint, config=CFG, now_us=T0 + 1_000_000)


def test_retry_state_is_atomic_with_the_checkpoint_on_crash():
    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)
    store = FaultInjectingStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    checkpoint_before = store.checkpoint
    pending_before = store.pending_retries()

    # the retry batch crashes — queue state and checkpoint roll back TOGETHER
    store.fail_next_commit = True
    crashed = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=checkpoint_before, config=CFG, now_us=T0 + 2_000_000)
    assert not crashed.committed
    assert store.checkpoint == checkpoint_before
    assert store.pending_retries() == pending_before
    assert store.lookup(identity).attempt_count == 1

    # restart re-processes the retry exactly once
    restarted = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=checkpoint_before, config=CFG, now_us=T0 + 3_000_000)
    assert restarted.retried_keys == (key,)
    assert store.lookup(identity).current_status == STATUS_CLASSIFIED
    assert store.lookup(identity).attempt_count == 2


def test_self_heal_requeues_a_deferred_event_with_a_lost_queue_entry():
    from app.research.me_entry_quality_h1e_v1.capture import LedgerWrite
    from app.research.me_entry_quality_h1e_v1.contracts import admit_source_row

    rows = _rows(1)
    key = _event_key(rows)
    identity = ledger_identity(key)
    store = InMemoryH1EStore(activation=FREEZE)

    # simulate a deferred event whose queue entry was lost (never the event)
    event = admit_source_row(rows[0], freeze=FREEZE).event
    defer_record = make_classification_record(
        event, eligible=False, status=STATUS_DEFERRED,
        non_eligible_codes=("H1E_HTF_LIMITER_DENIED",))
    store.commit_batch(
        [LedgerWrite(
            ledger_identity=identity,
            source_event_key=key,
            protocol_fingerprint=FREEZE.protocol_fingerprint,
            event_kind="DEFERRED",
            preserved_hash=event.preserved_hash,
            payload=event.to_dict(),
            classification=defer_record.to_dict(),
            attempt_count=1,
            captured_at_us=event.captured_at_us,
            activation_id="ME_ENTRY_QUALITY_H1E_PROSPECTIVE_OBSERVER_V1",
            h1e_freeze_ts_us=FREEZE.freeze_ts_us,
        )],
        CaptureCursor.after_rows(rows),
    )
    assert store.pending_retries() == ()

    # redelivery self-heals the durable queue — the event is NOT lost
    healed = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG, now_us=T0)
    assert healed.admitted_count == 1
    assert [p.ledger_identity for p in store.pending_retries()] == [identity]

    retried = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=_ok_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=CFG, now_us=T0 + 2_000_000)
    assert retried.retried_keys == (key,)
    assert store.lookup(identity).current_status == STATUS_CLASSIFIED


# ─── B4: bounded pending-retry processing per cycle ────────────────────────


def _backlog_rows(n: int, base: int = 0):
    """n distinct deliverable events (unique identities, post-freeze)."""
    return [
        make_source_row(
            signal_time_us=FIXTURE_FREEZE_US + 10_000_000 + (base + i) * 10_000_000,
            captured_us=T0 + (base + i) * 1_000_000,
            candle_open_ms=1_700_000_000_000 + (base + i) * 300_000,
        )
        for i in range(n)
    ]


def test_backlog_larger_than_retry_batch_limit_is_drained_bounded():
    """Mandatory Test 1 — 120 due, retry_batch_limit=50 → 50/50/20."""
    rows = _backlog_rows(120)
    keys = [row["source_event_key"] for row in rows]
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=1_000_000,
        retry_batch_limit=50)
    calls = {"n": 0}

    def counting_ok(event, prior_deferrals=0):
        calls["n"] += 1
        return make_classification_record(event)

    store = InMemoryH1EStore(activation=FREEZE)
    # cycle 0 — deliver 120 events, every one DEFERRED into the queue
    first = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    assert first.committed
    assert len(store.pending_retries()) == 120

    expected_remaining = [70, 20, 0]
    expected_processed = [50, 50, 20]
    all_retried: list[str] = []
    for round_no, (exp_processed, exp_remaining) in enumerate(
            zip(expected_processed, expected_remaining), start=1):
        queue_before = tuple(store.pending_retries())
        now = T0 + 2_000_000 * round_no
        report = run_capture_cycle(
            reader=FakeReader([]), store=store, classifier=counting_ok,
            freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
            config=cfg, now_us=now)
        assert report.committed
        # no cycle exceeds the limit
        assert report.pending_processed == exp_processed
        assert report.pending_processed <= cfg.retry_batch_limit
        assert len(report.retried_keys) == exp_processed
        # exact queue state after commit (not an estimate)
        assert report.pending_due_total == 120 - 50 * (round_no - 1)
        assert report.pending_remaining == exp_remaining
        assert len(store.pending_retries()) == exp_remaining
        assert report.retry_batch_limit_reached is (exp_remaining > 0)
        # deterministic order: the current queue order prefix is processed
        expected_order = [
            p.source_event_key for p in sorted(
                queue_before, key=lambda p: (p.next_attempt_us, p.ledger_identity))
        ]
        assert list(report.retried_keys) == expected_order[:exp_processed]
        all_retried.extend(report.retried_keys)

    # all 120 events processed exactly once, none lost, none duplicated
    assert sorted(all_retried) == sorted(keys)
    assert len(set(all_retried)) == 120
    assert calls["n"] == 120
    assert store.pending_retries() == ()
    assert len(store.entries()) == 120  # unique ledger identities
    for key in keys:
        entry = store.lookup(ledger_identity(key))
        assert entry.current_status == STATUS_CLASSIFIED
        assert entry.attempt_count == 2
        # retried at the ORIGINAL signal_time ASOF
        assert entry.final_classification["asof_us"] == entry.payload["signal_time_us"]
        assert len(entry.journal) == 2  # DEFERRED + CLASSIFIED, append-only

    # already-terminal events are never re-classified by later cycles
    quiet = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=counting_ok,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 20_000_000)
    assert quiet.retried_keys == ()
    assert quiet.retry_terminal_keys == ()
    assert calls["n"] == 120  # unchanged


def test_retry_limit_reached_does_not_consume_budget_of_unprocessed():
    rows = _backlog_rows(10)
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, retry_batch_limit=4)
    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    report = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 1_000_000)
    assert report.pending_processed == 4
    assert report.retry_batch_limit_reached is True
    assert report.pending_remaining == 10  # re-deferred again
    # the 6 unprocessed entries kept their attempt budget untouched
    processed_ids = set(report.retried_keys)
    for pending in store.pending_retries():
        if pending.source_event_key in processed_ids:
            assert pending.attempt_count == 2  # a real attempt happened
        else:
            assert pending.attempt_count == 1  # skipped cycles cost NOTHING


def test_retry_batch_limit_rejects_invalid_values():
    rows = _backlog_rows(1)
    for bad in (True, False, None, 0, -1, "50", 2.5):
        cfg = CaptureConfig(allow_fixture_freeze=True, retry_batch_limit=bad)
        with pytest.raises(ValueError, match="retry_batch_limit"):
            run_capture_cycle(
                reader=FakeReader(rows), store=InMemoryH1EStore(activation=FREEZE),
                classifier=_ok_classifier, freeze=FREEZE,
                cursor=CaptureCursor(), config=cfg, now_us=T0)


# ─── B4 mandatory Test 2 — concurrent retry + source redelivery ────────────


def test_concurrent_retry_and_source_redelivery_classify_exactly_once():
    rows = _backlog_rows(1)
    key = rows[0]["source_event_key"]
    identity = ledger_identity(key)
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, retry_batch_limit=50)
    calls = {"n": 0}

    def counting_ok(event, prior_deferrals=0):
        calls["n"] += 1
        return make_classification_record(event)

    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    assert [p.ledger_identity for p in store.pending_retries()] == [identity]

    # E1 is due in the queue AND redelivered by the source in the SAME cycle
    # (at-least-once delivery: the source re-serves the row regardless of
    # the checkpoint, so the reader is given the full window again)
    report = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=counting_ok,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg,
        now_us=T0 + 1_000_000)
    assert report.committed
    # 1. classified exactly once (queue-driven), 2. redelivery = DUPLICATE
    assert report.retried_keys == (key,)
    assert report.duplicates == 1
    assert report.admitted_count == 0
    assert calls["n"] == 1
    # 3. no ledger duplicates — append-only evidence for one identity
    entry = store.lookup(identity)
    assert len(store.entries()) == 1
    assert len(entry.journal) == 2
    # 4./5. retry-state and checkpoint are atomic and correct
    assert store.pending_retries() == ()
    assert report.pending_remaining == 0
    assert store.checkpoint == report.cursor_after

    # 6. re-running the observer performs NO further (terminal) classification
    rerun = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=counting_ok,
        freeze=FREEZE, cursor=report.cursor_after, config=cfg,
        now_us=T0 + 2_000_000)
    assert rerun.retried_keys == ()
    assert calls["n"] == 1
    assert len(store.lookup(identity).journal) == 2


def test_redelivery_while_redeferred_is_duplicate_without_second_pass():
    rows = _backlog_rows(1)
    key = rows[0]["source_event_key"]
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=1_000_000, retry_batch_limit=50)
    calls = {"n": 0}

    def counting_defer(event, prior_deferrals=0):
        calls["n"] += 1
        return _defer_classifier(event, prior_deferrals)

    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=counting_defer,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)
    # retry re-DEFERS while the source redelivers the same event
    report = run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=counting_defer,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg,
        now_us=T0 + 2_000_000)
    assert report.retried_keys == (key,)
    assert report.duplicates == 1
    assert calls["n"] == 2  # 1 initial + 1 retry — never a third pass
    pending = store.pending_retries()
    assert len(pending) == 1
    assert pending[0].attempt_count == 2


def test_backlog_crash_restart_loses_nothing_duplicates_nothing():
    rows = _backlog_rows(10)
    keys = [row["source_event_key"] for row in rows]
    cfg = CaptureConfig(
        allow_fixture_freeze=True, retry_backoff_us=0, retry_batch_limit=4)
    calls = {"n": 0}

    def counting_ok(event, prior_deferrals=0):
        calls["n"] += 1
        return make_classification_record(event)

    store = FaultInjectingStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader(rows), store=store, classifier=_defer_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=cfg, now_us=T0)

    # crash in the MIDDLE of the backlog (cycle 1 of 3)
    store.fail_next_commit = True
    crashed = run_capture_cycle(
        reader=FakeReader([]), store=store, classifier=counting_ok,
        freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
        config=cfg, now_us=T0 + 1_000_000)
    assert not crashed.committed
    assert crashed.pending_processed == 4
    # documented failure semantics: the durable queue is the pre-cycle size
    assert crashed.pending_remaining == 10
    assert len(store.pending_retries()) == 10
    assert calls["n"] == 4  # classification attempted, effects rolled back

    # restart from durable state — resume WITHOUT loss or duplication
    restarted = InMemoryH1EStore.from_dict(
        store.to_dict(), activation=FREEZE)
    retried_all: list[str] = []
    for _ in range(10):  # bounded guard — 10 entries / limit 4 => <= 3 cycles
        report = run_capture_cycle(
            reader=FakeReader([]), store=restarted, classifier=counting_ok,
            freeze=FREEZE, cursor=CaptureCursor.after_rows(rows),
            config=cfg, now_us=T0 + 2_000_000)
        assert report.committed
        assert report.pending_processed <= cfg.retry_batch_limit
        retried_all.extend(report.retried_keys)
        if report.pending_remaining == 0:
            break
    else:
        raise AssertionError("backlog did not drain")
    # exactly one final classification per event across crash + restart
    assert sorted(retried_all) == sorted(keys)
    assert len(set(retried_all)) == 10
    # 4 attempts rolled back with the crash + 10 final attempts after restart
    assert calls["n"] == 14
    for key in keys:
        entry = restarted.lookup(ledger_identity(key))
        assert entry.current_status == STATUS_CLASSIFIED
        assert entry.attempt_count == 2
        assert len(entry.journal) == 2
