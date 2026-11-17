"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — partial TP ladder contract (Issue #276).

The frozen OOS-2 protocol under test (protocol.py, engine.py):

* OOS-2-LOCAL capacity: protocol.MAX_OPEN_POSITIONS == 12 concurrent OPEN
  positions — ``Settings.max_open_positions`` is NEVER consulted;
* entry-signal TTL: 120 minutes from ``signal_time`` (age >= 120:00 is
  refused, age 119:59 is admitted);
* max hold: 7200 minutes from ``entered_at`` (TIMEOUT at the deadline);
* SHORT geometry from the ACTUAL entry price P: stop = P*1.10, TP1 = P*0.95,
  TP2 = P*0.93, TP3 = P*0.90;
* PARTIAL ladder: TP1 closes 50% of the ORIGINAL quantity, TP2 closes 75%
  of the REMAINING (37.5% of original), TP3 closes ALL remaining (12.5%);
  each stage fires AT MOST ONCE, a partial leg keeps the position OPEN and
  keeps its capacity slot, entry facts stay immutable;
* STOP_FIRST intrabar priority: a bar touching the stop closes the WHOLE
  remaining quantity even when TP levels were crossed in the same bar;
* accounting: each committed leg books (gross − exit fee − exit slippage)
  exactly once; the position's net_pnl equals the sum of its legs minus
  entry fee/slippage and funding; remaining quantity is never negative.

Everything runs against the in-memory store — no PostgreSQL in this suite.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_oos2_v1 import (
    ENTRY_MAX_AGE_MINUTES,
    MAX_HOLD_MINUTES,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    event_from_candidate,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.activation import (
    ACTIVATION_FLAG,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_EXPIRED,
    ActivationRecord,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    EXPERIMENT_ID,
    MAX_OPEN_POSITIONS,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    close_quantity,
    protocol_fingerprint,
    short_geometry,
)
from app.scanners.models import SetupCandidate, SetupState

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
PINNED_FINGERPRINT = (
    "1d1e7b4d87c6125183887c8f054a4edb1ff3f0711e70da1feeb9b7e249d7b7ec"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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
        max_open_positions=3,       # must be IGNORED by this contour
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation() -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


def _candidate(
    symbol: str,
    price: float = 100.0,
    *,
    detected_at: datetime = NOW,
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
        features={
            "exhaustion_magnitude": 0.01,
            "rsi_14": 70.0,
            "rsi_delta_3": 2.0,
            "entry_confirmation_rsi_rising": True,
        },
    )


def _contour(
    settings: Settings | None = None, clock: _Clock | None = None,
) -> tuple[InverseShortContour, InMemoryInverseShortStore, _Clock]:
    settings = settings or _settings()
    clock = clock or _Clock(NOW)
    store = InMemoryInverseShortStore(
        _activation(), initial_balance=settings.initial_balance,
    )
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert contour.activated, contour.activation_reason
    return contour, store, clock


def _capture(store: InMemoryInverseShortStore, candidate: SetupCandidate) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"},
    )


def _open_one(
    contour: InverseShortContour,
    store: InMemoryInverseShortStore,
    clock: _Clock,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
) -> InverseShortPosition:
    """Capture one fresh event and enter it at ``price`` (age 0)."""
    assert _capture(store, _candidate(symbol, price, detected_at=clock())) \
        == "CAPTURED"
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    opened = [
        p for p in contour.engine.open_positions.values() if p.symbol == symbol
    ]
    assert len(opened) == 1
    return opened[0]


def _position(
    contour: InverseShortContour, symbol: str,
) -> InverseShortPosition:
    matches = [
        p for p in contour.engine.open_positions.values() if p.symbol == symbol
    ]
    assert len(matches) == 1, f"expected exactly one OPEN {symbol}"
    return matches[0]


def _assert_ledger_consistent(contour: InverseShortContour) -> None:
    """balance == initial + Σclosed net + Σopen(realized legs − costs)."""
    engine = contour.engine
    expected = engine.initial_balance
    for p in engine.store.list_positions():
        if p.status == "CLOSED":
            expected += p.net_pnl
        else:
            expected += (
                p.realized_legs_net
                - p.entry_fee
                - p.entry_slippage_cost
                - p.funding_paid
            )
            assert p.remaining_size > 0, "OPEN position with no remainder"
            assert p.remaining_size <= p.position_size + 1e-12
    expected += sum(engine._uncommitted_deltas.values())
    assert expected == pytest.approx(engine.balance, abs=1e-6), (
        engine.balance, expected,
    )


# ---------------------------------------------------------------------------
# Protocol constants / geometry / ladder math
# ---------------------------------------------------------------------------

class TestLadderProtocol:
    def test_fingerprint_is_pinned(self):
        assert protocol_fingerprint() == PINNED_FINGERPRINT

    def test_capacity_is_local_12(self):
        assert MAX_OPEN_POSITIONS == 12

    def test_ttl_and_max_hold(self):
        assert ENTRY_MAX_AGE_MINUTES == 120
        assert MAX_HOLD_MINUTES == 7200

    def test_short_geometry(self):
        stop, tp1, tp2, tp3 = short_geometry(100.0)
        assert stop == pytest.approx(110.0)
        assert tp1 == pytest.approx(95.0)
        assert tp2 == pytest.approx(93.0)
        assert tp3 == pytest.approx(90.0)

    def test_close_quantity_ladder(self):
        size = 8.0
        assert close_quantity("TAKE_PROFIT_1", size, size) == pytest.approx(4.0)
        after_tp1 = size - close_quantity("TAKE_PROFIT_1", size, size)
        assert close_quantity("TAKE_PROFIT_2", size, after_tp1) \
            == pytest.approx(3.0)
        after_tp2 = after_tp1 - close_quantity("TAKE_PROFIT_2", size, after_tp1)
        assert after_tp2 == pytest.approx(1.0)
        assert close_quantity("TAKE_PROFIT_3", size, after_tp2) \
            == pytest.approx(1.0)

    def test_close_quantity_is_deterministic_and_never_overcloses(self):
        for size in (1.0, 0.333333333333, 12345.678901234567, 1e-6):
            for stage in ("TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3"):
                qty = close_quantity(stage, size, size)
                assert qty == close_quantity(stage, size, size)  # deterministic
                assert qty >= 0.0
                assert qty <= size + 1e-12                      # never overclose

    def test_execution_spec_carries_the_ladder(self):
        from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
            execution_spec,
        )
        spec = execution_spec()
        assert spec["max_open_positions"] == 12
        assert spec["entry_max_age_minutes"] == 120
        assert spec["max_hold_minutes"] == 7200
        stages = {s["stage"]: s for s in spec["take_profit_stages"]}
        assert stages["TAKE_PROFIT_1"]["close_basis"] == "ORIGINAL"
        assert stages["TAKE_PROFIT_1"]["close_fraction"] == 0.50
        assert stages["TAKE_PROFIT_2"]["close_basis"] == "REMAINING"
        assert stages["TAKE_PROFIT_2"]["close_fraction"] == 0.75
        assert stages["TAKE_PROFIT_3"]["close_fraction"] == 1.00


# ---------------------------------------------------------------------------
# TP1 alone: PARTIAL leg, position stays OPEN, slot kept, once only
# ---------------------------------------------------------------------------

class TestPartialTP1:
    def test_tp1_closes_half_and_keeps_position_open(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price
        facts = (pos.entered_at, pos.deadline_at, pos.entry_price, size)

        # Touch ONLY TP1 (between 0.95 and 0.93 of entry).
        cycle = contour.run_cycle({pos.symbol: entry * 0.945})
        assert cycle["exits"] == 0, "a partial leg is NOT a close"
        assert cycle["open_positions"] == 1

        pos = _position(contour, pos.symbol)
        assert pos.status == "OPEN"
        assert pos.tp1_executed and not pos.tp2_executed and not pos.tp3_executed
        assert pos.remaining_size == pytest.approx(size * 0.5, rel=1e-9)
        # capacity slot kept
        assert len(contour.engine.open_positions) == 1
        # entry facts immutable
        assert (pos.entered_at, pos.deadline_at, pos.entry_price,
                pos.position_size) == facts
        # one committed leg, exactly
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == ["TAKE_PROFIT_1"]
        assert legs[0].is_terminal is False
        assert legs[0].remaining_after == pytest.approx(size * 0.5, rel=1e-9)
        _assert_ledger_consistent(contour)

    def test_tp1_never_fires_twice(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        # price revisits the TP1 level twice more
        contour.run_cycle({pos.symbol: entry * 0.945})
        contour.run_cycle({pos.symbol: entry * 0.940})
        legs = store.list_close_legs()
        assert [l.exit_reason for l in legs] == ["TAKE_PROFIT_1"]
        assert _position(contour, pos.symbol).remaining_size == pytest.approx(
            pos.position_size * 0.5, rel=1e-9,
        )
        _assert_ledger_consistent(contour)

    def test_tp1_recheck_does_not_reenter_or_move_entry_time(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entered_at, deadline = pos.entered_at, pos.deadline_at
        contour.run_cycle({pos.symbol: pos.entry_price * 0.945})
        contour.run_cycle({pos.symbol: pos.entry_price * 0.945})
        pos = _position(contour, pos.symbol)
        assert pos.entered_at == entered_at
        assert pos.deadline_at == deadline


# ---------------------------------------------------------------------------
# TP2 after TP1: 75% of the REMAINING
# ---------------------------------------------------------------------------

class TestPartialTP2:
    def test_tp2_closes_75_percent_of_remaining(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price

        contour.run_cycle({pos.symbol: entry * 0.945})          # TP1
        cycle = contour.run_cycle({pos.symbol: entry * 0.925})  # TP2
        assert cycle["exits"] == 0

        pos = _position(contour, pos.symbol)
        assert pos.status == "OPEN"
        assert pos.tp1_executed and pos.tp2_executed and not pos.tp3_executed
        assert pos.remaining_size == pytest.approx(size * 0.125, rel=1e-9)
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2",
        ]
        # TP2 quantity = 75% of the 50% remainder = 37.5% of original
        assert legs[1].quantity == pytest.approx(size * 0.375, rel=1e-9)
        assert legs[1].remaining_after == pytest.approx(size * 0.125, rel=1e-9)
        _assert_ledger_consistent(contour)

    def test_partial_leg_net_pnl_is_booked_into_position_balance(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        pos = _position(contour, pos.symbol)
        legs = store.list_close_legs(pos.position_key)
        leg = legs[0]
        # Leg accounting contract: net = gross − exit fee − exit slippage −
        # this leg's entry-cost share; the position's cumulative realized
        # legs ledger carries gross − exit fee − exit slippage.
        expected_leg_net = (
            leg.gross_pnl - leg.exit_fee - leg.exit_slippage
            - leg.entry_cost_share
        )
        assert leg.net_pnl == pytest.approx(expected_leg_net, abs=1e-9)
        assert pos.realized_legs_net == pytest.approx(
            leg.gross_pnl - leg.exit_fee - leg.exit_slippage, abs=1e-9,
        )
        assert leg.entry_cost_share > 0
        assert leg.net_pnl > 0   # TP1 at +5% on a SHORT is a win
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Level skipping: one bar below TP3 fires all three legs, ONE closed trade
# ---------------------------------------------------------------------------

class TestLevelSkipping:
    def test_single_bar_below_tp3_fires_whole_ladder(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price

        cycle = contour.run_cycle({pos.symbol: entry * 0.89})
        assert cycle["exits"] == 1
        assert cycle["exit_reasons"] == ["TAKE_PROFIT_3"]
        assert len(contour.engine.open_positions) == 0

        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3",
        ]
        assert [l.is_terminal for l in legs] == [False, False, True]
        assert legs[0].quantity == pytest.approx(size * 0.50, rel=1e-9)
        assert legs[1].quantity == pytest.approx(size * 0.375, rel=1e-9)
        assert legs[2].quantity == pytest.approx(size * 0.125, rel=1e-9)
        assert legs[2].remaining_after == 0.0

        # exactly ONE closed trade — the partial legs are not trades
        closed = store.list_positions("CLOSED")
        assert len(closed) == 1
        assert closed[0].exit_reason == "TAKE_PROFIT_3"
        assert closed[0].tp1_executed and closed[0].tp2_executed \
            and closed[0].tp3_executed
        _assert_ledger_consistent(contour)

    def test_terminal_net_pnl_equals_legs_minus_entry_costs_and_funding(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        contour.run_cycle({pos.symbol: pos.entry_price * 0.89})
        closed = store.list_positions("CLOSED")[0]
        legs = store.list_close_legs(closed.position_key)
        # Position net_pnl == Σ leg net − funding (each leg already carries
        # its exact entry-cost share — nothing is counted twice).
        legs_net = sum(l.net_pnl for l in legs)
        assert closed.net_pnl == pytest.approx(
            legs_net - closed.funding_paid, abs=1e-6,
        )
        assert closed.realized_legs_net == pytest.approx(
            sum(l.gross_pnl - l.exit_fee - l.exit_slippage for l in legs),
            abs=1e-9,
        )
        assert closed.entry_cost_allocated == pytest.approx(
            closed.entry_fee + closed.entry_slippage_cost, abs=1e-9,
        )
        assert sum(l.entry_cost_share for l in legs) == pytest.approx(
            closed.entry_fee + closed.entry_slippage_cost, abs=1e-9,
        )
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# STOP_FIRST intrabar priority
# ---------------------------------------------------------------------------

class TestStopFirst:
    def test_stop_before_any_tp_closes_full_remainder(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price
        bars = {
            pos.symbol: {"high": pos.stop_price * 1.001, "low": entry * 0.995},
        }
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == ["STOP_LOSS"]
        assert legs[0].quantity == pytest.approx(size, rel=1e-9)
        assert legs[0].remaining_after == 0.0
        _assert_ledger_consistent(contour)

    def test_stop_first_wins_in_a_bar_that_also_crosses_all_tps(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        # high touches the stop, low is far below TP3 — same bar
        bars = {
            pos.symbol: {"high": pos.stop_price * 1.001, "low": entry * 0.88},
        }
        cycle = contour.run_cycle({pos.symbol: entry * 0.88}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == ["STOP_LOSS"], legs
        closed = store.list_positions("CLOSED")
        assert len(closed) == 1 and closed[0].exit_reason == "STOP_LOSS"
        _assert_ledger_consistent(contour)

    def test_stop_after_tp1_closes_only_the_remaining_half(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})          # TP1 partial
        bars = {
            pos.symbol: {"high": pos.stop_price * 1.001, "low": entry * 0.99},
        }
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "STOP_LOSS",
        ]
        assert legs[1].is_terminal
        assert legs[1].quantity == pytest.approx(size * 0.5, rel=1e-9)
        assert legs[1].remaining_after == 0.0
        _assert_ledger_consistent(contour)

    def test_stop_after_tp2_closes_only_the_last_eighth(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})          # TP1
        contour.run_cycle({pos.symbol: entry * 0.925})          # TP2
        bars = {
            pos.symbol: {"high": pos.stop_price * 1.001, "low": entry * 0.99},
        }
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "STOP_LOSS",
        ]
        assert legs[2].quantity == pytest.approx(size * 0.125, rel=1e-9)
        _assert_ledger_consistent(contour)

    def test_protective_stop_never_partial(self):
        """A STOP_LOSS leg is always terminal and always the full remainder."""
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        contour.run_cycle({pos.symbol: pos.entry_price * 0.945})
        bars = {
            pos.symbol: {
                "high": pos.stop_price * 1.001,
                "low": pos.entry_price * 0.99,
            },
        }
        contour.run_cycle({pos.symbol: pos.entry_price}, bars=bars)
        terminal = [
            l for l in store.list_close_legs() if l.exit_reason == "STOP_LOSS"
        ]
        assert len(terminal) == 1
        assert terminal[0].is_terminal and terminal[0].remaining_after == 0.0


# ---------------------------------------------------------------------------
# TIMEOUT at the immutable 7200-minute deadline
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_position_survives_until_the_deadline(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        deadline = pos.deadline_at
        clock.now = deadline - timedelta(minutes=1)
        cycle = contour.run_cycle({pos.symbol: pos.entry_price})
        assert cycle["exits"] == 0
        assert pos.position_key in contour.engine.open_positions

    def test_timeout_at_exactly_7200_minutes(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        deadline = pos.deadline_at
        clock.now = deadline            # 7200 minutes exactly (>= rule)
        cycle = contour.run_cycle({pos.symbol: pos.entry_price})
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == ["TIMEOUT"]
        assert legs[0].is_terminal
        _assert_ledger_consistent(contour)

    def test_timeout_preserves_realized_tp_pnl_after_partials(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        size = pos.position_size
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})          # TP1
        contour.run_cycle({pos.symbol: entry * 0.925})          # TP2
        pos = _position(contour, pos.symbol)
        realized_before = pos.realized_legs_net

        clock.now = pos.deadline_at
        cycle = contour.run_cycle({pos.symbol: entry})
        assert cycle["exit_reasons"] == ["TIMEOUT"]

        closed = store.list_positions("CLOSED")[0]
        assert closed.exit_reason == "TIMEOUT"
        # realized TP PnL preserved: the position carries the same legs
        legs = store.list_close_legs(closed.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TIMEOUT",
        ]
        partial_legs = [l for l in legs if not l.is_terminal]
        partial_net = sum(l.net_pnl for l in partial_legs)
        # realized_before = Σ(gross − exit fee − exit slip) of the partial
        # legs = their net + their entry-cost shares.
        assert realized_before == pytest.approx(
            partial_net + sum(l.entry_cost_share for l in partial_legs),
            abs=1e-9,
        )
        assert closed.realized_legs_net == pytest.approx(
            sum(l.gross_pnl - l.exit_fee - l.exit_slippage for l in legs),
            abs=1e-9,
        )
        # terminal TIMEOUT closes only the last 12.5%
        assert legs[-1].quantity == pytest.approx(size * 0.125, rel=1e-9)
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Entry TTL boundaries: 119:59 admitted, 120:00 refused
# ---------------------------------------------------------------------------

class TestEntryTtlBoundary:
    def test_age_119_59_is_admitted(self):
        contour, store, clock = _contour()
        detected = clock() - timedelta(minutes=119, seconds=59)
        assert _capture(store, _candidate("BTCUSDT", detected_at=detected)) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_age_120_00_is_refused_and_swept_expired(self):
        contour, store, clock = _contour()
        detected = clock() - timedelta(minutes=120)
        assert _capture(store, _candidate("BTCUSDT", detected_at=detected)) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert not contour.engine.open_positions
        # generic TTL sweep (age >= TTL, no position) -> EXPIRED/ENTRY_TTL
        event = store.list_events()[0]
        assert event.status == EVENT_STATUS_EXPIRED
        assert event.status_reason == "ENTRY_TTL"
        assert event.status_at is not None


# ---------------------------------------------------------------------------
# OOS-2-LOCAL capacity 12 / 13
# ---------------------------------------------------------------------------

class TestCapacityTwelve:
    def test_settings_capacity_is_ignored(self):
        """Settings.max_open_positions=3 must NOT cap this contour."""
        contour, store, clock = _contour(_settings(max_open_positions=3))
        for i in range(5):
            _open_one(contour, store, clock, symbol=f"SYM{i}USDT")
        assert len(contour.engine.open_positions) == 5

    def test_thirteenth_event_is_refused_while_full(self):
        contour, store, clock = _contour()
        for i in range(MAX_OPEN_POSITIONS):
            _open_one(contour, store, clock, symbol=f"SYM{i:02d}USDT")
        assert len(contour.engine.open_positions) == 12

        # 13th FRESH event: refused (no position), stays CAPTURED
        assert _capture(
            store, _candidate("EXTRAUSDT", detected_at=clock()),
        ) == "CAPTURED"
        cycle = contour.run_cycle({f"SYM{i:02d}USDT": 100.0
                                  for i in range(MAX_OPEN_POSITIONS)})
        cycle["EXTRAUSDT"] = 100.0
        assert cycle["entries"] == 0, cycle
        assert len(contour.engine.open_positions) == 12

        # once its TTL elapses while the book is still full -> terminal
        # CAPACITY_REJECTED with cause + timestamp
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES + 1)
        prices = {f"SYM{i:02d}USDT": 100.0 for i in range(MAX_OPEN_POSITIONS)}
        prices["EXTRAUSDT"] = 100.0
        contour.run_cycle(prices)
        extra = [
            e for e in store.list_events() if e.symbol == "EXTRAUSDT"
        ][0]
        assert extra.status == EVENT_STATUS_CAPACITY_REJECTED
        assert extra.status_reason == "ENTRY_CAP"
        assert extra.status_at is not None
        _assert_ledger_consistent(contour)

    def test_freed_slot_is_reused_by_a_fresh_event_only(self):
        contour, store, clock = _contour()
        for i in range(MAX_OPEN_POSITIONS):
            _open_one(contour, store, clock, symbol=f"SYM{i:02d}USDT")
        # a STALE event that was refused while full
        stale_detected = clock() - timedelta(minutes=ENTRY_MAX_AGE_MINUTES + 5)
        assert _capture(
            store, _candidate("STALEUSDT", detected_at=stale_detected),
        ) == "CAPTURED"

        # free one slot (stop out SYM00)
        sym0 = "SYM00USDT"
        pos0 = _position(contour, sym0)
        bars = {sym0: {"high": pos0.stop_price * 1.001,
                       "low": pos0.entry_price * 0.995}}
        prices = {f"SYM{i:02d}USDT": 100.0 for i in range(MAX_OPEN_POSITIONS)}
        prices["STALEUSDT"] = 100.0
        cycle = contour.run_cycle(prices, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert len(contour.engine.open_positions) == 11

        # stale backlog event must NOT take the freed slot (age >= TTL)
        cycle = contour.run_cycle(prices)
        assert cycle["entries"] == 0, cycle
        stale = [e for e in store.list_events()
                 if e.symbol == "STALEUSDT"][0]
        assert stale.status == EVENT_STATUS_EXPIRED
        assert stale.status_reason == "ENTRY_TTL"

        # a FRESH event does take it
        assert _capture(
            store, _candidate("FRESHUSDT", detected_at=clock()),
        ) == "CAPTURED"
        prices["FRESHUSDT"] = 100.0
        cycle = contour.run_cycle(prices)
        assert cycle["entries"] == 1, cycle
        assert len(contour.engine.open_positions) == 12
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Funding accrues on the REMAINING quantity only
# ---------------------------------------------------------------------------

class TestFundingOnRemaining:
    def test_funding_after_tp1_is_half_of_the_unaffected_position(self):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos_a = _open_one(contour, store, clock, symbol="AAAAUSDT")
        pos_b = _open_one(contour, store, clock, symbol="BBBBUSDT")
        # sizing derives from the live balance, so allow a tiny drift
        assert pos_a.position_size == pytest.approx(
            pos_b.position_size, rel=1e-3,
        )
        entry = pos_a.entry_price

        # TP1 on A only
        contour.run_cycle({
            "AAAAUSDT": entry * 0.945, "BBBBUSDT": entry,
        })
        assert _position(contour, "AAAAUSDT").remaining_size == pytest.approx(
            pos_a.position_size * 0.5, rel=1e-9,
        )

        # one whole funding interval for both
        clock.advance(hours=8)
        contour.run_cycle(
            {"AAAAUSDT": entry * 0.945, "BBBBUSDT": entry},
            funding_rates_percent={"AAAAUSDT": 0.01, "BBBBUSDT": 0.01},
        )
        fund_a = _position(contour, "AAAAUSDT").funding_paid
        fund_b = _position(contour, "BBBBUSDT").funding_paid
        assert fund_b != 0, "control position must accrue funding"
        # funding accrues on the REMAINING quantity: A holds ~50% of B's
        # quantity (sign convention aside, the MAGNITUDE scales with the
        # outstanding size; balance-derived sizing drift allowed at 1%)
        remaining_ratio = (
            _position(contour, "AAAAUSDT").remaining_size
            / _position(contour, "BBBBUSDT").remaining_size
        )
        assert abs(fund_a) == pytest.approx(
            abs(fund_b) * remaining_ratio, rel=0.01,
        )
        assert abs(fund_a) < abs(fund_b)
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Dirty-leg retry: a failed leg write is replayed exactly once
# ---------------------------------------------------------------------------

class TestDirtyLegRetry:
    def test_failed_leg_write_is_retried_without_double_booking(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        original = store.commit_partial_exit
        calls = {"n": 0}

        def flaky(position, leg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated connection loss")
            return original(position, leg)

        store.commit_partial_exit = flaky

        # cycle 1: leg applied in memory, write fails -> DIRTY
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert contour.engine.durability_risk
        assert pos.position_key in contour.engine.dirty_leg_keys

        # while DIRTY, new entries fail closed (second capture refused)
        assert _capture(store, _candidate("MOREUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({pos.symbol: entry * 0.945})
        assert cycle["entries"] == 0, cycle

        # cycle 2: the retry confirms the SAME leg row
        cycle = contour.run_cycle({pos.symbol: entry * 0.945})
        assert not contour.engine.durability_risk
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == ["TAKE_PROFIT_1"]
        assert len(legs) == 1, "the retry must never book the leg twice"

        # entry reopens now that the ledger is clean
        cycle = contour.run_cycle({pos.symbol: entry * 0.945,
                                   "MOREUSDT": 100.0})
        assert cycle["entries"] == 1, cycle
        _assert_ledger_consistent(contour)

    def test_leg_write_failure_never_blocks_protective_exits(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        original = store.commit_partial_exit

        def always_fail(position, leg):
            raise RuntimeError("ledger down")

        store.commit_partial_exit = always_fail
        contour.run_cycle({pos.symbol: entry * 0.945})   # TP1 dirty

        # protective STOP must still execute while the ledger is dirty
        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        store.commit_partial_exit = original
        # and the dirty TP1 leg is replayed by the recovery path
        contour.run_cycle({pos.symbol: entry})
        legs = store.list_close_legs(pos.position_key)
        assert [l.exit_reason for l in legs] == [
            "TAKE_PROFIT_1", "STOP_LOSS",
        ], legs
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Telemetry / snapshot / mark persistence
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_snapshot_exposes_capacity_and_ladder(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        contour.run_cycle({pos.symbol: pos.entry_price * 0.945})
        snap = contour.engine.snapshot({pos.symbol: pos.entry_price * 0.945})
        assert snap["capacity_limit"] == 12
        assert snap["capacity_used_pct"] == pytest.approx(
            1 / 12 * 100.0, abs=1e-4,   # telemetry rounds to 4 decimals
        )
        assert snap["dirty_legs"] == 0   # count of owed leg rows
        row = [r for r in snap["positions"]
               if r["symbol"] == pos.symbol][0]
        assert row["tp1"] == pytest.approx(pos.target_price)
        assert row["tp2"] == pytest.approx(pos.tp2_price)
        assert row["tp3"] == pytest.approx(pos.tp3_price)
        assert row["position_size"] == pytest.approx(pos.position_size)
        assert row["remaining_size"] == pytest.approx(
            pos.position_size * 0.5, rel=1e-9,
        )
        assert row["stages_executed"] == ["TAKE_PROFIT_1"]

    def test_last_mark_price_is_persisted_for_unrealized(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        mark = pos.entry_price * 0.97
        contour.run_cycle({pos.symbol: mark})
        stored = store.get_position(pos.position_key)
        assert stored is not None
        assert stored.last_mark_price == pytest.approx(mark)

    def test_position_row_round_trips_ladder_state(self):
        contour, store, clock = _contour()
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        contour.run_cycle({pos.symbol: entry * 0.925})
        loaded = InverseShortPosition.from_dict(
            store.get_position(pos.position_key).to_dict(),
        )
        assert loaded.tp1_executed and loaded.tp2_executed
        assert not loaded.tp3_executed
        assert loaded.remaining_size == pytest.approx(
            pos.position_size * 0.125, rel=1e-9,
        )
        assert loaded.executed_stages == ("TAKE_PROFIT_1", "TAKE_PROFIT_2")
