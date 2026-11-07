"""Third Codex review fixes for ME_LONG_V2_INVERSE_SHORT_V1 (PR #267).

P2-1 — funding rate unavailable (tri-state: never a fabricated zero)

    * a MISSING rate is UNKNOWN: no period is advanced, no accrual is
      booked, no zero is fabricated; the ``due - charged`` gap stays on the
      position row and survives restart;
    * a CONFIRMED ``0.0`` settles the due periods as an explicit zero
      exactly once (the counter advances, the balance does not move);
    * multiple missed intervals settle EXACTLY ONCE at the first rate
      actually observed after recovery (backlog policy, logged) — no loss,
      no double charge, across restarts;
    * STOP/TP/TIMEOUT during a funding outage still executes, while the
      unsettled obligation stays recorded on the frozen closed row and is
      reported — never retroactively booked (exit facts unchanged).

P2-2 — DRAINING restart / EXIT_ONLY permissions

    * NEW_ENTRIES and MANAGE_EXISTING are separate grants on the identity;
    * RUNNING + valid fingerprint/freeze_ts → FULL mode;
    * DRAINING with a valid identity → EXIT_ONLY (restart lands here
      directly, never returning to RUNNING): no new capture, no new
      entries, no reuse of CAPTURED events — while exits, funding
      reconciliation, outbox replay and pending-close recovery continue;
    * CANCELLED / unknown status / fingerprint mismatch / corrupt freeze_ts
      fail closed; ``freeze_ts`` and fingerprint are never rewritten.

No PostgreSQL required — everything runs against the in-memory store (with
the fault-injecting subclass for the outbox-replay path).
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import (
    ActivationRecord,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import (
    ACTIVATION_FLAG,
    MODE_EXIT_ONLY,
    MODE_FULL,
    PERMISSION_MANAGE_EXISTING,
    PERMISSION_NEW_ENTRIES,
    evaluate_activation,
)
from app.paper.me_long_v2_inverse_short_v1.capture import (
    CAPTURED,
    CAPTURE_NOT_ACTIVE,
    ROUTE_PRE_FILTER_SOURCE,
    event_from_candidate,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from app.paper.me_long_v2_inverse_short_v1.store import SAVE_NOT_ACTIVE
from app.scanners.models import SetupCandidate, SetupState

PINNED_PROTOCOL_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)
FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
FUNDING_INTERVAL_SECONDS = 8 * 3600


# ======================================================================
# Shared helpers (mirrors the other contour suites)
# ======================================================================

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation(status: str = "RUNNING", *, fingerprint: str | None = None,
                freeze_ts: datetime | None = FREEZE_TS) -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status=status,
        protocol_fingerprint=(
            protocol_fingerprint() if fingerprint is None else fingerprint
        ),
        freeze_ts=freeze_ts,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
) -> SetupCandidate:
    candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=SOURCE_DIRECTION,
        entry_timeframe="5m",
        detected_at=detected_at,
        setup_started_at=detected_at,
        signal_candle_open_time=candle_ms,
        reference_price=price,
        entry_zone_low=price * 0.998,
        entry_zone_high=price * 1.001,
        invalidation_price=price * 0.975,
        target_1=price * 1.03,
        score=45.0,
        market_regime="RANGE",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _capture(store, candidate, *, clock=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"}, clock=clock,
    )


def _open_position(
    store: InMemoryInverseShortStore,
    *,
    clock: _Clock | None = None,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    settings: Settings | None = None,
):
    """Capture one source event and open its SHORT position at `price`."""
    settings = settings or _settings()
    clock = clock or _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert _capture(store, _candidate(symbol, price, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return contour, clock, position


def _env(status: str = "RUNNING", **store_kwargs):
    """Fresh store + contour with one open SHORT position."""
    store = InMemoryInverseShortStore(
        _activation(status), initial_balance=_settings().initial_balance,
        **store_kwargs,
    )
    contour, clock, position = _open_position(store)
    return store, contour, clock, position


def _restart(contour: InverseShortContour, clock: _Clock) -> InverseShortContour:
    return InverseShortContour(
        contour.settings, contour.store, clock=clock, activation_enabled=True,
    )


# ======================================================================
# P2-1: funding rate tri-state
# ======================================================================

class TestFundingRateTriState:
    def test_missing_rate_leaves_period_unsettled_and_books_nothing(self):
        store, contour, clock, position = _env()
        balance_before = contour.engine.balance
        symbol = position.symbol

        # 7h: no whole period due yet.
        clock.advance(hours=7)
        cycle = contour.run_cycle({symbol: 100.0})   # NO funding rates at all
        assert cycle["exits"] == 0
        assert position.funding_periods_charged == 0
        assert contour.engine.unsettled_funding_periods() == 0

        # 8h: one whole period is due — but the rate is UNKNOWN.
        clock.advance(hours=1)
        cycle = contour.run_cycle({symbol: 100.0})
        assert cycle["exits"] == 0
        # (1) period stays unsettled, (2) no fictitious accrual, no zero:
        assert position.funding_periods_charged == 0
        assert position.funding_paid == 0.0
        assert contour.engine.balance == balance_before
        assert contour.engine.accounting_consistent is True
        snap = contour.engine.snapshot({symbol: 100.0})
        assert snap["unsettled_funding_periods"] == 1
        assert snap["exit_only"] is False
        # The DURABLE row carries the unsettled state too.
        stored = store.list_positions(status=POSITION_STATUS_OPEN)[0]
        assert stored.funding_periods_charged == 0
        assert stored.to_dict()["funding_periods"] == 0
        # No warning-free fabrication: the engine recorded the outage.
        assert position.position_key in contour.engine._funding_unknown

    def test_confirmed_zero_settles_exactly_once(self):
        store, contour, clock, position = _env()
        balance_before = contour.engine.balance
        symbol = position.symbol

        clock.advance(hours=8)
        cycle = contour.run_cycle({symbol: 100.0}, {symbol: 0.0})
        assert cycle["exits"] == 0
        # A CONFIRMED zero counts the period — but moves no money.
        assert position.funding_periods_charged == 1
        assert position.funding_paid == 0.0
        assert contour.engine.balance == balance_before
        assert contour.engine.unsettled_funding_periods() == 0
        stored = store.list_positions(status=POSITION_STATUS_OPEN)[0]
        assert stored.funding_periods_charged == 1

        # Restart keeps the confirmed zero counted; replay never double-counts.
        contour2 = _restart(contour, clock)
        pos2 = next(iter(contour2.engine.open_positions.values()))
        assert pos2.funding_periods_charged == 1
        cycle2 = contour2.run_cycle({symbol: 100.0}, {symbol: 0.0})
        assert cycle2["entries"] == 0
        assert pos2.funding_periods_charged == 1
        assert pos2.funding_paid == 0.0
        assert contour2.engine.balance == balance_before
        assert contour2.engine.unsettled_funding_periods() == 0

    def test_multiple_missed_intervals_settle_exactly_once_on_recovery(self):
        store, contour, clock, position = _env()
        symbol = position.symbol
        notional = position.entry_price * position.position_size

        # Two whole intervals missed during the outage.
        clock.advance(hours=16)
        contour.run_cycle({symbol: 100.0})                 # no rate
        assert position.funding_periods_charged == 0
        assert contour.engine.unsettled_funding_periods() == 2
        contour.run_cycle({symbol: 100.0})                 # still no rate
        assert position.funding_periods_charged == 0       # nothing invented
        assert contour.engine.unsettled_funding_periods() == 2

        balance_before = contour.engine.balance
        # Recovery: the FIRST observed rate settles the whole backlog once
        # (explicit policy — logged, never silent substitution).
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        expected_paid = -notional * (0.01 / 100.0) * 2     # SHORT receives
        assert position.funding_periods_charged == 2       # no interval lost
        assert position.funding_paid == pytest.approx(expected_paid)
        assert contour.engine.balance == pytest.approx(
            balance_before - expected_paid,
        )
        assert contour.engine.unsettled_funding_periods() == 0
        assert contour.engine.accounting_consistent is True

        # Replay with the same rate and no time advance → NO double charge.
        paid_after = position.funding_paid
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert position.funding_periods_charged == 2
        assert position.funding_paid == paid_after

        # A further period settles exactly once more at the observed rate.
        clock.advance(hours=8)
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert position.funding_periods_charged == 3
        assert position.funding_paid == pytest.approx(
            paid_after - notional * (0.01 / 100.0),
        )

    def test_restart_mid_outage_preserves_unsettled_then_settles_once(self):
        store, contour, clock, position = _env()
        symbol = position.symbol
        notional = position.entry_price * position.position_size

        clock.advance(hours=16)
        contour.run_cycle({symbol: 100.0})                 # outage: no rate
        assert position.funding_periods_charged == 0
        assert contour.engine.unsettled_funding_periods() == 2

        # RESTART while the outage continues — the gap survives verbatim.
        contour2 = _restart(contour, clock)
        pos2 = next(iter(contour2.engine.open_positions.values()))
        assert pos2.funding_periods_charged == 0
        assert contour2.engine.unsettled_funding_periods() == 2

        # Data recovers AFTER the restart: settle the backlog exactly once.
        contour2.run_cycle({symbol: 100.0}, {symbol: 0.01})
        expected_paid = -notional * (0.01 / 100.0) * 2
        assert pos2.funding_periods_charged == 2
        assert pos2.funding_paid == pytest.approx(expected_paid)
        assert contour2.engine.unsettled_funding_periods() == 0

        # Another restart + replay never re-books anything.
        contour3 = _restart(contour2, clock)
        pos3 = next(iter(contour3.engine.open_positions.values()))
        assert pos3.funding_periods_charged == 2
        assert pos3.funding_paid == pytest.approx(expected_paid)
        contour3.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert pos3.funding_periods_charged == 2
        assert pos3.funding_paid == pytest.approx(expected_paid)
        assert contour3.engine.unsettled_funding_periods() == 0

    @pytest.mark.parametrize("exit_mode", ["STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT"])
    def test_protective_exit_during_funding_outage_preserves_obligation(
        self, exit_mode,
    ):
        store, contour, clock, position = _env()
        symbol = position.symbol

        if exit_mode == "TIMEOUT":
            clock.advance(hours=128)                       # past the deadline
            exit_price = 100.0
        else:
            clock.advance(hours=8)                         # 1 whole period due
            exit_price = (
                position.stop_price if exit_mode == "STOP_LOSS"
                else position.target_price
            )

        # NO funding rates anywhere: the protective exit must still happen.
        cycle = contour.run_cycle({symbol: exit_price})
        assert cycle["exits"] == 1, cycle
        assert cycle["exit_reasons"] == [exit_mode]

        closed = contour.engine.closed_positions()[0]
        due = int(
            (closed.closed_at - closed.entered_at).total_seconds()
            // FUNDING_INTERVAL_SECONDS
        )
        assert due >= 1
        # Funding obligation preserved, never fabricated as a confirmed zero.
        assert closed.funding_periods_charged == 0
        assert closed.funding_paid == 0.0
        assert closed.to_dict()["funding_periods"] == 0
        # PnL: no funding was booked, no exit fact changed.
        expected_net = (
            closed.gross_pnl - closed.entry_fee - closed.exit_fee
            - closed.slippage_cost
        )
        assert closed.net_pnl == pytest.approx(expected_net)

        # The obligation is REPORTED (open + closed), not silently dropped.
        snap = contour.engine.snapshot({symbol: 100.0})
        assert snap["unsettled_funding_periods"] == due

        # It survives a RESTART (derived from the frozen row) …
        contour2 = _restart(contour, clock)
        assert contour2.engine.unsettled_funding_periods() == due
        # … and rate recovery NEVER retroactively settles a CLOSED position
        # (exit facts stay byte-identical, PnL is never double-counted).
        contour2.run_cycle({symbol: 100.0}, {symbol: 0.01})
        stored_closed = contour2.engine.closed_positions()[0]
        assert stored_closed.funding_periods_charged == 0
        assert stored_closed.funding_paid == 0.0
        assert stored_closed.net_pnl == closed.net_pnl
        assert stored_closed.closed_at == closed.closed_at
        assert contour2.engine.unsettled_funding_periods() == due
        assert len(contour2.engine.open_positions) == 0

    def test_nonfinite_feed_value_is_treated_as_unknown(self):
        store, contour, clock, position = _env()
        symbol = position.symbol
        balance_before = contour.engine.balance
        clock.advance(hours=8)
        cycle = contour.run_cycle({symbol: 100.0}, {symbol: float("nan")})
        assert cycle["exits"] == 0
        assert position.funding_periods_charged == 0        # nothing booked
        assert position.funding_paid == 0.0
        assert math.isfinite(contour.engine.balance)
        assert contour.engine.balance == balance_before
        assert contour.engine.unsettled_funding_periods() == 1


# ======================================================================
# P2-2: DRAINING → EXIT_ONLY permissions
# ======================================================================

class TestDrainingExitOnly:
    def test_permissions_are_separated_on_the_identity(self):
        running, reason_r = evaluate_activation(_activation("RUNNING"), enabled=True)
        draining, reason_d = evaluate_activation(_activation("DRAINING"), enabled=True)
        assert running is not None and reason_r == "ACTIVE"
        assert draining is not None and reason_d != reason_r

        # FULL mode: both grants.
        assert running.allows(PERMISSION_NEW_ENTRIES) is True
        assert running.allows(PERMISSION_MANAGE_EXISTING) is True
        assert running.exit_only is False and running.mode == MODE_FULL

        # EXIT_ONLY mode: MANAGE_EXISTING only.
        assert draining.allows(PERMISSION_MANAGE_EXISTING) is True
        assert draining.allows(PERMISSION_NEW_ENTRIES) is False
        assert draining.exit_only is True and draining.mode == MODE_EXIT_ONLY

        # Unknown permissions fail closed for BOTH modes.
        assert running.allows("SOMETHING_ELSE") is False
        assert draining.allows("SOMETHING_ELSE") is False

    def test_running_to_draining_to_restart_exit_only_lifecycle(self):
        store, contour, clock, position = _env()
        # A second source event enters while RUNNING (FULL mode works).
        assert _capture(store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
                        clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle["entries"] == 1 and cycle["exit_only"] is False
        assert len(contour.engine.open_positions) == 2

        # Operator drains: RUNNING -> DRAINING (the script preserves
        # fingerprint + freeze_ts; nothing here rewrites them).
        store.set_activation(_activation("DRAINING"))
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle["activated"] is True                  # still managed
        assert cycle["exit_only"] is True                  # but EXIT_ONLY
        assert cycle["entries"] == 0                       # NEW_ENTRIES off

        # RESTART lands directly in EXIT_ONLY — never back in RUNNING.
        contour2 = _restart(contour, clock)
        assert contour2.activated is True
        assert contour2.exit_only is True
        assert contour2.activation.status == "DRAINING"
        assert contour2.activation.freeze_ts == FREEZE_TS
        assert contour2.activation.protocol_fingerprint == (
            protocol_fingerprint()
        )
        cycle2 = contour2.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle2["exit_only"] is True
        assert cycle2["entries"] == 0
        assert len(contour2.engine.open_positions) == 2

        # MANAGE_EXISTING continues: both positions still reach TIMEOUT.
        clock.advance(hours=128)
        cycle3 = contour2.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle3["exit_reasons"] == ["TIMEOUT", "TIMEOUT"]
        assert len(contour2.engine.open_positions) == 0
        # And the funding obligation of that outage is still reported.
        assert contour2.engine.unsettled_funding_periods() == 32

    def test_exit_only_refuses_capture_and_captured_event_reuse(self):
        store, contour, clock, position = _env()
        # One event captured while RUNNING stays CAPTURED (pending).
        assert _capture(store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
                        clock=clock) == CAPTURED
        assert len(contour.engine.pending_events()) == 1

        store.set_activation(_activation("DRAINING"))
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        # (4) no reuse of CAPTURED events to open trades:
        assert cycle["entries"] == 0
        assert len(contour.engine.pending_events()) == 1      # still CAPTURED
        assert len(contour.engine.open_positions) == 1        # nothing new
        # (4) no new source capture while EXIT_ONLY:
        assert _capture(store, _candidate("SOLUSDT", 20.0, detected_at=clock()),
                        clock=clock) == CAPTURE_NOT_ACTIVE
        assert len(contour.engine.pending_events()) == 1
        # Direct engine use is refused too (defence in depth).
        assert contour.engine.open_entries({"SOLUSDT": 20.0}) == []

    def test_store_save_event_refused_in_exit_only(self):
        store, contour, clock, position = _env()
        event = event_from_candidate(
            _candidate("ETHUSDT", 50.0, detected_at=clock()),
            capture_route=ROUTE_PRE_FILTER_SOURCE,
            activation=evaluate_activation(_activation("RUNNING"), enabled=True)[0],
            captured_at=clock(),
        )
        # RUNNING: the store-level guard accepts the row.
        store.set_activation(_activation("RUNNING"))
        assert store.save_event(event) == "CAPTURED"
        # DRAINING: no NEW source event rows may be written at all.
        store.set_activation(_activation("DRAINING"))
        assert store.save_event(event) == SAVE_NOT_ACTIVE

    def test_exit_only_replays_pending_close_through_outbox(self):
        from tests.test_me_long_v2_inverse_short_review2_durability_v1 import (
            _FaultStore,
        )

        store = _FaultStore(
            _activation(), initial_balance=_settings().initial_balance,
            fail_close_updates=1,                       # first UPDATE fails
        )
        contour, clock, position = _open_position(store)
        symbol = position.symbol
        clock.advance(hours=128)

        cycle = contour.run_cycle({symbol: 100.0})       # NO funding rates
        assert cycle["exits"] == 1
        assert len(contour.engine.pending_closes) == 1   # retained in memory
        assert len(store.list_pending_closes()) == 1     # staged durably
        assert contour.engine.durability_risk is False   # outbox committed

        # Drain + RESTART while the close is pending: EXIT_ONLY continues
        # managing — the pending close is replayed through the outbox.
        store.set_activation(_activation("DRAINING"))
        contour2 = _restart(contour, clock)
        assert contour2.exit_only is True
        contour2.run_cycle({symbol: 100.0})              # flush runs first
        assert len(contour2.engine.closed_positions()) == 1
        assert len(contour2.engine.pending_closes) == 0
        assert len(store.list_pending_closes()) == 0
        snap = contour2.engine.snapshot({symbol: 100.0})
        assert snap["durability_risk"] is False
        assert snap["accounting_consistent"] is True
        assert snap["exit_only"] is True
        # The obligation of the funding outage during the failed close is
        # still reported, not zeroed.
        assert snap["unsettled_funding_periods"] >= 16

    def test_draining_restart_never_rewrites_freeze_or_fingerprint(self):
        store, contour, clock, position = _env()
        before = store.load_activation()
        store.set_activation(_activation("DRAINING"))

        for _ in range(3):                               # repeated restarts
            contour_i = _restart(contour, clock)
            assert contour_i.exit_only is True
            contour_i.run_cycle({position.symbol: 100.0})
            assert contour_i.activation_reason != "ACTIVE"

        after = store.load_activation()
        assert after.status == "DRAINING"                # never re-promoted
        assert after.protocol_fingerprint == before.protocol_fingerprint
        assert after.freeze_ts == before.freeze_ts
        # And no new signals can be activated on restore.
        assert _capture(store, _candidate("SOLUSDT", 20.0, detected_at=clock()),
                        clock=clock) == CAPTURE_NOT_ACTIVE

    def test_fingerprint_mismatch_fails_closed_even_while_draining(self):
        store = InMemoryInverseShortStore(
            _activation("DRAINING", fingerprint="0" * 64),
            initial_balance=_settings().initial_balance,
        )
        identity, reason = evaluate_activation(store.load_activation(), enabled=True)
        assert identity is None
        assert reason == "PROTOCOL_FINGERPRINT_MISMATCH"

        contour = InverseShortContour(
            _settings(), store, clock=_Clock(SIGNAL_AT), activation_enabled=True,
        )
        assert contour.activated is False                # fail closed
        assert contour.exit_only is False
        assert _capture(store, _candidate(), clock=_Clock(SIGNAL_AT)) == (
            CAPTURE_NOT_ACTIVE
        )
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["activated"] is False
        assert cycle["entries"] == 0

    @pytest.mark.parametrize("status", [
        "CANCELLED", "READY_TO_START", "PENDING", "PAUSED", "",
    ])
    def test_non_running_non_draining_statuses_fail_closed(self, status):
        identity, reason = evaluate_activation(_activation(status), enabled=True)
        assert identity is None
        assert reason == "ACTIVATION_NOT_RUNNING"

        store = InMemoryInverseShortStore(
            _activation(status), initial_balance=_settings().initial_balance,
        )
        contour = InverseShortContour(
            _settings(), store, clock=_Clock(SIGNAL_AT), activation_enabled=True,
        )
        assert contour.activated is False
        assert _capture(store, _candidate(), clock=_Clock(SIGNAL_AT)) == (
            CAPTURE_NOT_ACTIVE
        )

    def test_corrupt_freeze_ts_fails_closed_for_draining(self):
        # Naive (corrupt) freeze timestamp: EXIT_ONLY is refused entirely.
        naive = _activation("DRAINING", freeze_ts=datetime(2026, 1, 1))
        identity, reason = evaluate_activation(naive, enabled=True)
        assert identity is None
        assert reason == "FREEZE_TS_INVALID"
        # Missing freeze likewise.
        missing = _activation("DRAINING", freeze_ts=None)
        identity, reason = evaluate_activation(missing, enabled=True)
        assert identity is None
        assert reason == "FREEZE_TS_MISSING"


# ======================================================================
# Frozen protocol guard
# ======================================================================

def test_frozen_protocol_fingerprint_unchanged():
    assert protocol_fingerprint() == PINNED_PROTOCOL_FINGERPRINT
