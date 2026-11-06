"""ME_LONG_V2_INVERSE_SHORT_V1 — contour contract tests (no DB required).

Covers the ten implementation requirements at unit/integration level:

* frozen protocol (TP 10% / SL 10% of the ACTUAL entry, 7200-minute hold,
  DCA/trailing/BE off, prospective-only, paper-only) + pinned fingerprint;
* stable ``event_key`` + full lineage to the source signal;
* isolated SHORT position state (namespaced keys, never
  ``PaperTradingEngine.open_trades[symbol]``);
* realistic entry/TP/SL/TIMEOUT model with fees, slippage and funding;
* immutable ``entered_at`` / ``deadline_at`` across restarts;
* duplicate delivery idempotency, simultaneous TP+SL STOP_FIRST priority,
  missing-market-data fail-safe hold;
* two-factor activation (flag + RUNNING row with matching fingerprint and
  freeze_ts) and the strict no-backfill boundary;
* the pre-activation dry-run readiness evidence.

Everything runs against the in-memory store — no PostgreSQL in this suite.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.paper.me_long_v2_inverse_short_v1 import (
    STRATEGY_ID,
    ActivationIdentity,
    ActivationRecord,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    event_from_candidate,
    event_key,
    position_key,
    protocol_fingerprint,
    run_dry_run,
)
from app.paper.me_long_v2_inverse_short_v1.activation import (
    ACTIVATION_FLAG,
    evaluate_activation,
    is_post_freeze,
)
from app.paper.me_long_v2_inverse_short_v1.capture import (
    CAPTURE_DISABLED,
    CAPTURE_DUPLICATE,
    CAPTURE_INVALID_ROUTE,
    CAPTURE_INVALID_SCANNER,
    CAPTURE_INVALID_EVENT,
    CAPTURE_NOT_ACTIVE,
    CAPTURE_PRE_FREEZE,
    CAPTURED,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    MAX_HOLD_MINUTES,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    deadline_for,
    short_geometry,
)
from app.scanners.models import SetupCandidate, SetupState

# The frozen protocol fingerprint at implementation time.  Any change to the
# frozen parameters re-hashes the protocol and MUST fail this test first —
# that is the signal to re-fix the fingerprint + freeze_ts separately
# (scripts/me_inverse_short_v1_activate.py) after the review passes.
PINNED_PROTOCOL_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)


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
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation(
    status: str = "RUNNING",
    fingerprint: str | None = None,
    freeze_ts: datetime | None = FREEZE_TS,
) -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status=status,
        protocol_fingerprint=fingerprint if fingerprint is not None else protocol_fingerprint(),
        freeze_ts=freeze_ts,
    )


def _store(
    settings: Settings | None = None,
    activation: ActivationRecord | None | object = ...,
) -> InMemoryInverseShortStore:
    if activation is ...:
        activation = _activation()
    return InMemoryInverseShortStore(
        activation, initial_balance=(settings or _settings()).initial_balance,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
    candle_ms: int | None = None,
    scanner: str = SOURCE_SCANNER,
    direction: str = SOURCE_DIRECTION,
) -> SetupCandidate:
    if candle_ms is None:
        candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=scanner,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=direction,
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


def _capture(store, candidate, *, clock=None, env=None, route=ROUTE_PRE_FILTER_SOURCE) -> str:
    return capture_with_status(
        store, candidate, capture_route=route,
        env={ACTIVATION_FLAG: "1"} if env is None else env,
        clock=clock,
    )


def _contour(store, settings=None, clock=None) -> InverseShortContour:
    return InverseShortContour(
        settings or _settings(), store, clock=clock, activation_enabled=True,
    )


def _open_one(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    clock: _Clock | None = None,
    candidate: SetupCandidate | None = None,
):
    """Capture one source event and open its SHORT position at `price`."""
    settings = _settings()
    store = _store(settings)
    clock = clock or _Clock(SIGNAL_AT)
    contour = _contour(store, settings, clock)
    cand = candidate or _candidate(symbol, price, detected_at=clock())
    assert _capture(store, cand, clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return settings, store, contour, clock, position


# A minimal repository for the PRODUCTION Paper engine (isolation tests).
class _FakePaperRepo:
    def __init__(self, open_rows=None, account_snapshot=None):
        self.open_rows = list(open_rows or [])
        self.saved: list[PaperTradeRecord] = []
        self.closed: list[dict] = []
        self.safety_gate_modes: list[str] = []
        self.account_snapshot = account_snapshot
        self.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.safety_gate_state = {
            "is_blocked": False, "reason": None, "blocked_since": None,
        }

    def get_open_paper_trades(self):
        return list(self.open_rows)

    def get_paper_risk_state(self):
        return self.risk_state

    def get_paper_safety_gate_state(self):
        return self.safety_gate_state

    def set_paper_safety_gate_mode(self, mode):
        self.safety_gate_modes.append(mode)

    def insert_paper_safety_event(self, event):
        pass

    def block_paper_safety_gate(self, reason, blocked_since):
        self.safety_gate_state = {
            "is_blocked": True, "reason": reason, "blocked_since": blocked_since,
        }

    def get_latest_paper_account_snapshot(self):
        return self.account_snapshot

    def get_paper_trade_by_setup(self, setup_id):
        for index, trade in enumerate(self.saved, start=1):
            if trade.setup_id == setup_id:
                return index
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def save_dca_state(self, *args):
        pass

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass


def _me_short_candidate(symbol: str = "BTCUSDT") -> SetupCandidate:
    """Production MOMENTUM_EXHAUSTION SHORT (for the isolation test)."""
    return SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION",
        symbol=symbol,
        direction="SHORT",
        score=38.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=110.0,
        target_1=85.0,
    )


# ---------------------------------------------------------------------------
# Protocol freeze
# ---------------------------------------------------------------------------

class TestProtocolFreeze:
    def test_frozen_parameters(self):
        assert STRATEGY_ID == "ME_LONG_V2_INVERSE_SHORT_V1"
        assert SOURCE_SCANNER == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
        assert SOURCE_DIRECTION == "LONG"          # source events are LONG
        assert TAKE_PROFIT_PCT == 0.10
        assert STOP_LOSS_PCT == 0.10
        assert MAX_HOLD_MINUTES == 7200

    def test_protocol_fingerprint_is_pinned(self):
        """Changing any frozen parameter must fail HERE first (re-fix needed)."""
        assert protocol_fingerprint() == PINNED_PROTOCOL_FINGERPRINT

    def test_protocol_fingerprint_is_stable_and_sha256_shaped(self):
        first = protocol_fingerprint()
        second = protocol_fingerprint()
        assert first == second
        assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)

    def test_short_geometry_is_10pct_around_actual_entry(self):
        stop, target = short_geometry(100.0)
        assert stop == pytest.approx(110.0)
        assert target == pytest.approx(90.0)

    def test_deadline_is_exactly_7200_minutes(self):
        entered = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        assert deadline_for(entered) == entered + timedelta(minutes=7200)


# ---------------------------------------------------------------------------
# Event identity + lineage (requirement 2)
# ---------------------------------------------------------------------------

class TestEventIdentity:
    def test_event_key_is_stable_across_redelivery(self):
        first = _candidate(detected_at=SIGNAL_AT)
        # Same signal candle, later detection, different reference price:
        second = _candidate(detected_at=SIGNAL_AT + timedelta(minutes=4), price=101.0)
        assert event_key(first) == event_key(second)

    def test_event_key_differs_per_symbol_and_candle(self):
        base = _candidate()
        other_symbol = _candidate(symbol="ETHUSDT")
        other_candle = _candidate(candle_ms=1_800_000_000_000)
        keys = {event_key(base), event_key(other_symbol), event_key(other_candle)}
        assert len(keys) == 3

    def test_event_key_rejects_foreign_source(self):
        with pytest.raises(ValueError):
            event_key(_candidate(scanner="SUPPORT_RESISTANCE_REACTION"))
        with pytest.raises(ValueError):
            event_key(_candidate(direction="SHORT"))

    def test_position_key_is_namespaced(self):
        key = event_key(_candidate())
        namespaced = position_key(key)
        assert namespaced.startswith(f"{STRATEGY_ID}:")
        assert namespaced != key

    def test_lineage_links_full_source_signal(self):
        settings = _settings()
        store = _store(settings)
        candidate = _candidate()
        assert _capture(store, candidate) == CAPTURED
        event = store.list_events()[0]

        lineage = event.lineage
        assert lineage["source_setup_id"] == str(candidate.setup_id)
        assert lineage["source_signal_time"] == SIGNAL_AT.isoformat()
        assert lineage["source_scanner"] == SOURCE_SCANNER
        assert lineage["source_direction"] == SOURCE_DIRECTION
        assert lineage["source_identity"] == (
            f"{SOURCE_SCANNER}|BTCUSDT|LONG|5m|"
            f"{int(SIGNAL_AT.timestamp() // 300) * 300_000}"
        )
        assert lineage["capture_route"] == ROUTE_PRE_FILTER_SOURCE
        assert lineage["capture_independent_of"] == [
            "direction_gate", "expectancy_filter",
        ]
        assert lineage["source_pipeline_fingerprint"] == candidate.fingerprint
        assert lineage["protocol_fingerprint"] == protocol_fingerprint()
        assert event.event_key == event_key(candidate)
        assert event.signal_candle_open_time == int(
            SIGNAL_AT.timestamp() // 300
        ) * 300_000


# ---------------------------------------------------------------------------
# Capture statuses + prospective boundary (requirements 1, 10)
# ---------------------------------------------------------------------------

class TestCapture:
    def test_disabled_without_runtime_flag(self):
        store = _store()
        status = _capture(store, _candidate(), env={})
        assert status == CAPTURE_DISABLED
        assert store.list_events() == []

    def test_not_active_without_activation_row(self):
        store = _store(activation=None)
        status = _capture(store, _candidate())
        assert status == CAPTURE_NOT_ACTIVE
        assert store.list_events() == []

    def test_not_active_on_fingerprint_mismatch(self):
        store = _store(activation=_activation(fingerprint="0" * 64))
        status = _capture(store, _candidate())
        assert status == CAPTURE_NOT_ACTIVE
        assert store.list_events() == []

    def test_not_active_when_activation_not_running(self):
        store = _store(activation=_activation(status="READY_TO_START", fingerprint=None))
        status = _capture(store, _candidate())
        assert status == CAPTURE_NOT_ACTIVE

    def test_pre_freeze_event_refused(self):
        store = _store()
        historical = _candidate(detected_at=datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc))
        status = _capture(store, historical)
        assert status == CAPTURE_PRE_FREEZE
        assert store.list_events() == []

    def test_capture_and_duplicate_delivery(self):
        store = _store()
        candidate = _candidate()
        assert _capture(store, candidate) == CAPTURED
        assert _capture(store, candidate) == CAPTURE_DUPLICATE
        # Redelivery through a fresh copy of the same signal candle
        # (+2 minutes still floors to the same 5m candle):
        assert _capture(store, _candidate(detected_at=SIGNAL_AT + timedelta(minutes=2))) == CAPTURE_DUPLICATE
        assert len(store.list_events()) == 1

    def test_invalid_scanner_and_route(self):
        store = _store()
        assert _capture(store, _candidate(scanner="TREND_PULLBACK_V2")) == CAPTURE_INVALID_SCANNER
        assert _capture(store, None) == CAPTURE_INVALID_EVENT
        assert _capture(store, _candidate(), route="NOT_A_ROUTE") == CAPTURE_INVALID_ROUTE

    def test_strict_no_backfill_boundary(self):
        freeze = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert is_post_freeze(freeze + timedelta(microseconds=1), freeze) is True
        assert is_post_freeze(freeze, freeze) is False
        assert is_post_freeze(freeze - timedelta(seconds=1), freeze) is False
        # Naive timestamps fail closed.
        naive = datetime(2026, 6, 1, 12, 0, 1)
        assert is_post_freeze(naive, freeze) is False
        assert is_post_freeze(freeze, naive) is False

    def test_activation_evaluation_reasons(self):
        identity, reason = evaluate_activation(_activation(), enabled=True)
        assert identity is not None and reason == "ACTIVE"
        assert identity.freeze_ts == FREEZE_TS

        _, reason = evaluate_activation(_activation(), enabled=False)
        assert reason == "DISABLED_FLAG"
        _, reason = evaluate_activation(None, enabled=True)
        assert reason == "ACTIVATION_ROW_MISSING"
        _, reason = evaluate_activation(
            _activation(fingerprint="f" * 64), enabled=True,
        )
        assert reason == "PROTOCOL_FINGERPRINT_MISMATCH"
        _, reason = evaluate_activation(
            _activation(freeze_ts=None), enabled=True,
        )
        assert reason == "FREEZE_TS_MISSING"


# ---------------------------------------------------------------------------
# Entry model (requirement 4)
# ---------------------------------------------------------------------------

class TestEntryModel:
    def test_entry_geometry_uses_actual_entry_price(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # SHORT sell fill: adverse slippage moves the fill DOWN.
        expected_entry = 100.0 * (1.0 - settings.slippage_percent)
        assert position.entry_market_price == pytest.approx(100.0)
        assert position.entry_price == pytest.approx(expected_entry)
        # 10% are measured from the ACTUAL entry, not from the market price.
        assert position.stop_price == pytest.approx(expected_entry * 1.10, rel=1e-12)
        assert position.target_price == pytest.approx(expected_entry * 0.90, rel=1e-12)
        assert position.direction == "SHORT"
        assert position.entered_at == clock.now
        assert position.deadline_at == deadline_for(clock.now)
        assert position.position_key.startswith(f"{STRATEGY_ID}:")

    def test_entry_sizing_risk_budget(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        distance = position.stop_price - position.entry_price
        expected_qty = (settings.initial_balance * settings.risk_per_trade) / distance
        assert position.position_size == pytest.approx(expected_qty)
        assert position.risk_usdt == pytest.approx(
            settings.initial_balance * settings.risk_per_trade,
        )
        # Entry costs are charged to the isolated balance immediately.
        expected_fee = position.entry_price * position.position_size * settings.taker_fee
        expected_slip = (100.0 - position.entry_price) * position.position_size
        assert position.entry_fee == pytest.approx(expected_fee)
        assert position.entry_slippage_cost == pytest.approx(expected_slip)
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance - expected_fee - expected_slip,
        )

    def test_entry_refused_without_price(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        cycle = contour.run_cycle({})  # missing market data
        assert cycle["entries"] == 0
        assert contour.engine.open_positions == {}
        assert cycle["pending_events"] == 1  # waits for data, never guesses


# ---------------------------------------------------------------------------
# TP / SL / TIMEOUT exits (requirements 4, 9)
# ---------------------------------------------------------------------------

class TestExitTakeProfit:
    def test_take_profit_exit_and_pnl_model(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        qty = position.position_size
        entry = position.entry_price
        target_ref = entry * 0.90

        # Price falls through the 10% target → SHORT takes profit.
        market = target_ref * 0.99
        closed = contour.engine.check_exits({position.symbol: market})
        assert len(closed) == 1
        result = closed[0]
        assert result.exit_reason == "TAKE_PROFIT_1"
        assert result.status == "CLOSED"
        assert contour.engine.open_positions == {}

        # Fill model: cover with adverse slippage above the reference.
        fill = target_ref * (1.0 + settings.slippage_percent)
        assert result.exit_price == pytest.approx(fill)

        gross = (100.0 - target_ref) * qty
        exit_fee = fill * qty * settings.taker_fee
        exit_slip = (fill - target_ref) * qty
        expected_net = (
            gross
            - position.entry_fee
            - exit_fee
            - (position.entry_slippage_cost + exit_slip)
        )
        assert result.gross_pnl == pytest.approx(gross)
        assert result.net_pnl == pytest.approx(expected_net)
        assert result.net_pnl > 0  # 10% move beats the ~1bp round-trip costs
        # Isolated balance = initial + closed net (no funding in this cycle).
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + expected_net,
        )
        # Persisted terminal state.
        stored = store.list_positions(status="CLOSED")
        assert len(stored) == 1
        assert stored[0].exit_reason == "TAKE_PROFIT_1"
        assert stored[0].net_pnl == pytest.approx(expected_net)


class TestExitStopLoss:
    def test_stop_gap_exit(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        qty = position.position_size
        stop_ref = position.stop_price
        market = stop_ref * 1.01  # mid already beyond the stop → gap

        closed = contour.engine.check_exits({position.symbol: market})
        assert len(closed) == 1
        result = closed[0]
        assert result.exit_reason == "STOP_LOSS_GAP"

        fill = stop_ref * (1.0 + settings.slippage_percent)
        assert result.exit_price == pytest.approx(fill)
        gross = (100.0 - stop_ref) * qty
        exit_fee = fill * qty * settings.taker_fee
        exit_slip = (fill - stop_ref) * qty
        expected_net = (
            gross - position.entry_fee - exit_fee
            - (position.entry_slippage_cost + exit_slip)
        )
        assert result.gross_pnl == pytest.approx(gross)
        assert result.net_pnl == pytest.approx(expected_net)
        assert result.net_pnl < 0
        assert contour.engine.balance == pytest.approx(
            settings.initial_balance + expected_net,
        )
        assert store.list_positions(status="CLOSED")[0].exit_reason == "STOP_LOSS_GAP"

    def test_intrabar_stop_touch_is_clean_stop_loss(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # Mid price below the stop, but the bar high touched it → STOP_LOSS.
        bars = {
            position.symbol: {
                "high": position.stop_price * 1.001,
                "low": 99.0,
            },
        }
        closed = contour.engine.check_exits({position.symbol: 100.0}, bars=bars)
        assert len(closed) == 1
        assert closed[0].exit_reason == "STOP_LOSS"
        # Fill is the stop level itself (stop-market simulation), not the mid.
        fill = position.stop_price * (1.0 + settings.slippage_percent)
        assert closed[0].exit_price == pytest.approx(fill)

    def test_no_exit_before_stop_is_touched(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # Price drifts against the SHORT but stays below the stop.
        up = position.stop_price * 0.999
        closed = contour.engine.check_exits({position.symbol: up})
        assert closed == []
        assert position.position_key in contour.engine.open_positions
        # MAE is tracked.
        assert contour.engine.open_positions[position.position_key].mae > 0


class TestExitTimeout:
    def test_timeout_fires_exactly_at_original_deadline(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        original_deadline = position.deadline_at
        assert original_deadline == position.entered_at + timedelta(minutes=7200)

        # 1 second before the deadline: still open.
        clock.now = original_deadline - timedelta(seconds=1)
        assert contour.engine.check_exits({position.symbol: 100.0}) == []
        assert len(contour.engine.open_positions) == 1

        # At the deadline: TIMEOUT, exit at the market price with full costs.
        clock.now = original_deadline
        closed = contour.engine.check_exits({position.symbol: 100.0})
        assert len(closed) == 1
        result = closed[0]
        assert result.exit_reason == "TIMEOUT"
        assert result.closed_at == original_deadline
        assert result.duration_sec == pytest.approx(7200 * 60)

        settings_vals = settings
        fill = 100.0 * (1.0 + settings_vals.slippage_percent)
        gross = (100.0 - 100.0) * result.position_size
        exit_fee = fill * result.position_size * settings_vals.taker_fee
        exit_slip = (fill - 100.0) * result.position_size
        expected_net = (
            gross - result.entry_fee - exit_fee
            - (result.entry_slippage_cost + exit_slip)
        )
        assert result.gross_pnl == pytest.approx(gross)
        assert result.net_pnl == pytest.approx(expected_net)
        assert result.net_pnl < 0  # flat market: round-trip costs only

    def test_timeout_not_fired_early_over_many_cycles(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        for _ in range(30):
            clock.advance(minutes=39)  # up to ~19.5h, way below 120h
            assert contour.engine.check_exits({position.symbol: 100.0}) == []
        assert len(contour.engine.open_positions) == 1


# ---------------------------------------------------------------------------
# Simultaneous conditions → STOP_FIRST (requirement 9)
# ---------------------------------------------------------------------------

class TestSimultaneousExitPriority:
    def test_stop_wins_over_take_profit_on_shared_bar(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # ONE bar touches both bands (possible only via intrabar extremes).
        bars = {
            position.symbol: {
                "high": position.stop_price * 1.001,
                "low": position.target_price * 0.999,
            },
        }
        closed = contour.engine.check_exits({position.symbol: 100.0}, bars=bars)
        assert len(closed) == 1
        assert closed[0].exit_reason == "STOP_LOSS"  # STOP_FIRST, never TP

    def test_stop_wins_over_timeout_at_deadline(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        clock.now = position.deadline_at + timedelta(minutes=1)
        bars = {position.symbol: {"high": position.stop_price * 1.001, "low": 100.0}}
        closed = contour.engine.check_exits({position.symbol: 100.0}, bars=bars)
        assert len(closed) == 1
        assert closed[0].exit_reason in ("STOP_LOSS", "STOP_LOSS_GAP")

    def test_take_profit_wins_over_timeout_at_deadline(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        clock.now = position.deadline_at + timedelta(minutes=1)
        market = position.target_price * 0.99
        closed = contour.engine.check_exits({position.symbol: market})
        assert len(closed) == 1
        assert closed[0].exit_reason == "TAKE_PROFIT_1"  # STOP → TP → TIMEOUT


# ---------------------------------------------------------------------------
# Missing market data (requirement 9)
# ---------------------------------------------------------------------------

class TestMissingMarketData:
    def test_hold_when_price_disappears_mid_flight(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # No data at all: neither exit nor mark guess a fill.
        assert contour.engine.check_exits({}) == []
        assert contour.engine.check_exits({position.symbol: 0.0}) == []
        assert contour.engine.check_exits({position.symbol: None}) == []
        assert len(contour.engine.open_positions) == 1
        assert position.position_key in contour.engine.open_positions

        # Data returns: normal exit works.
        clock.now = position.deadline_at + timedelta(minutes=1)
        closed = contour.engine.check_exits({position.symbol: 95.0})
        assert [p.exit_reason for p in closed] == ["TIMEOUT"]

    def test_event_without_price_stays_pending_then_enters(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED

        cycle = contour.run_cycle({"ETHUSDT": 2000.0})  # wrong symbol only
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == 1

        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1
        assert cycle["pending_events"] == 0


# ---------------------------------------------------------------------------
# Restart recovery + immutability (requirements 3, 5)
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    def test_entered_at_and_deadline_survive_restart(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        pkey = position.position_key
        original_entered = position.entered_at
        original_deadline = position.deadline_at
        balance_before = contour.engine.balance

        # --- simulated process restart (new objects, same store) -----------
        clock.advance(minutes=45)
        restarted = _contour(store, settings, clock)
        assert pkey in restarted.engine.open_positions
        recovered = restarted.engine.open_positions[pkey]
        assert recovered.entered_at == original_entered
        assert recovered.deadline_at == original_deadline
        assert recovered.stop_price == pytest.approx(position.stop_price)
        assert recovered.target_price == pytest.approx(position.target_price)
        assert recovered.entry_price == pytest.approx(position.entry_price)
        assert restarted.engine.balance == pytest.approx(balance_before)

        # The original deadline is NOT recomputed from the restart time.
        assert recovered.deadline_at == original_entered + timedelta(minutes=7200)
        assert recovered.deadline_at != clock.now + timedelta(minutes=7200)

    def test_deadline_closes_on_original_deadline_after_restart(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        original_deadline = position.deadline_at

        clock.advance(minutes=45)
        restarted = _contour(store, settings, clock)
        clock.now = original_deadline
        closed = restarted.engine.check_exits({position.symbol: 100.0})
        assert [p.exit_reason for p in closed] == ["TIMEOUT"]
        # Exactly 7200 minutes after the ORIGINAL entry — restart added none.
        assert closed[0].duration_sec == pytest.approx(7200 * 60)

    def test_store_refuses_deadline_tampering(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        original_deadline = position.deadline_at

        reopened = next(iter(store.load_open_positions()))
        reopened.deadline_at = original_deadline + timedelta(hours=3)
        reopened.entered_at = reopened.entered_at + timedelta(hours=1)
        store.update_position(reopened)
        assert store.immutability_repairs >= 2

        reloaded = next(iter(store.load_open_positions()))
        assert reloaded.deadline_at == original_deadline
        assert reloaded.entered_at == position.entered_at

        restarted = _contour(store, settings, clock)
        recovered = restarted.engine.open_positions[position.position_key]
        assert recovered.deadline_at == original_deadline

    def test_recovery_rejects_invalid_or_foreign_positions(self):
        """Non-SHORT / invalid-deadline rows are refused, not crashed on."""
        # Craft a foreign row directly (bypassing the engine's own writes).
        settings, store, contour, clock, good = _open_one(price=100.0)
        bad = dataclasses.replace(
            good,
            position_key=f"{STRATEGY_ID}:deadbeef",
            event_key="deadbeef",
            direction="LONG",
        )
        store._positions[bad.position_key] = bad

        from app.paper.me_long_v2_inverse_short_v1.engine import (
            InverseShortPaperEngine,
        )
        engine = InverseShortPaperEngine(settings, store, clock=clock)
        keys = set(engine.open_positions)
        assert good.position_key in keys
        assert bad.position_key not in keys  # non-SHORT refused


# ---------------------------------------------------------------------------
# Duplicate delivery (requirement 9)
# ---------------------------------------------------------------------------

class TestDuplicateDelivery:
    def test_double_capture_and_double_entry_are_idempotent(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        candidate = _candidate()

        assert _capture(store, candidate, clock=clock) == CAPTURED
        assert _capture(store, candidate, clock=clock) == CAPTURE_DUPLICATE

        cycle = contour.run_cycle({candidate.symbol: 100.0})
        assert cycle["entries"] == 1

        # Same event delivered again (same cycle, next cycle, next process):
        assert contour.run_cycle({candidate.symbol: 100.0})["entries"] == 0
        assert _capture(store, candidate, clock=clock) == CAPTURE_DUPLICATE
        restarted = _contour(store, settings, clock)
        assert restarted.run_cycle({candidate.symbol: 100.0})["entries"] == 0

        assert len(store.list_events()) == 1
        assert len(store.list_positions()) == 1
        assert len(contour.engine.open_positions) == 1

    def test_has_position_guard_when_event_mark_fails(self):
        """Even if mark_event_entered lost, the position guard dedups."""
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        candidate = _candidate()
        assert _capture(store, candidate, clock=clock) == CAPTURED

        assert contour.run_cycle({candidate.symbol: 100.0})["entries"] == 1

        # Simulate a failed mark-entered (event still CAPTURED after a crash).
        stored_event = store.get_event(event_key(candidate))
        stored_event.status = "CAPTURED"
        stored_event.position_key = None

        restarted = _contour(store, settings, clock)
        assert restarted.run_cycle({candidate.symbol: 100.0})["entries"] == 0
        assert len(store.list_positions()) == 1

    def test_distinct_signal_candles_open_distinct_positions(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        assert (
            _capture(
                store,
                _candidate(candle_ms=int(SIGNAL_AT.timestamp() // 300) * 300_000 + 300_000),
                clock=clock,
            )
            == CAPTURED
        )
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 2
        assert len(contour.engine.open_positions) == 2


# ---------------------------------------------------------------------------
# Isolation from the production Paper engine (requirement 3)
# ---------------------------------------------------------------------------

class TestIsolationFromProductionPaper:
    def test_contour_never_shares_production_trade_state(self):
        settings = _settings()
        repo = _FakePaperRepo()
        clock = _Clock(SIGNAL_AT)

        # Production Paper engine opens its own LONG... SHORT (ME) trade.
        main = PaperTradingEngine(settings, repo, clock=clock)
        main_opened = main.check_entries([_me_short_candidate()], {"BTCUSDT": 100.0})
        assert len(main_opened) == 1
        main_trade = main.open_trades["BTCUSDT"]
        main_balance = main.balance

        # The independent contour opens its own SHORT on the SAME symbol.
        store = _store(settings)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1

        # Production state is byte-for-byte untouched.
        assert set(main.open_trades) == {"BTCUSDT"}
        assert main.open_trades["BTCUSDT"] is main_trade
        assert main_trade.status == "OPEN"
        assert main.balance == main_balance
        assert not hasattr(main, "open_positions")

        contour_key = next(iter(contour.engine.open_positions))
        assert contour_key.startswith(f"{STRATEGY_ID}:")
        assert contour_key not in main.open_trades
        assert not any(
            isinstance(k, str) and k.startswith(STRATEGY_ID)
            for k in main.open_trades
        )

        # Production allows ONE trade per symbol; the contour allows many
        # positions per symbol (different source events).
        assert main.check_entries([_me_short_candidate()], {"BTCUSDT": 100.0}) == []

        second_candle = (
            int(SIGNAL_AT.timestamp() // 300) * 300_000 + 300_000
        )
        assert (
            _capture(store, _candidate(candle_ms=second_candle), clock=clock)
            == CAPTURED
        )
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1
        assert len(contour.engine.open_positions) == 2
        assert len(main.open_trades) == 1

        # Closing the contour leaves the production trade completely alone.
        closed = contour.run_cycle({"BTCUSDT": 85.0})
        assert closed["exits"] == 2
        assert main.open_trades["BTCUSDT"] is main_trade
        assert main_trade.status == "OPEN"
        assert main.balance == main_balance
        assert repo.closed == []

        # Closing the production trade leaves the contour's store alone.
        main_closed = main.check_exits({"BTCUSDT": 85.0})
        assert len(main_closed) == 1
        assert repo.closed and repo.closed[0]["exit_reason"] in (
            "TAKE_PROFIT_1", "TAKE_PROFIT_2",
        )
        assert len(store.list_positions(status="CLOSED")) == 2
        assert contour.engine.open_positions == {}

    def test_contour_balance_is_computed_only_from_contour_positions(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        # Production initial balance participates only through the contour's
        # own closed/open ledger — recomputed identically after a restart.
        restarted = _contour(store, settings, clock)
        assert restarted.engine.balance == pytest.approx(contour.engine.balance)
        assert restarted.engine.initial_balance == pytest.approx(
            settings.initial_balance,
        )


# ---------------------------------------------------------------------------
# Funding model (requirement 4)
# ---------------------------------------------------------------------------

class TestFundingModel:
    def test_short_receives_positive_funding_per_whole_interval(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        notional = position.entry_price * position.position_size
        balance_before = contour.engine.balance

        clock.advance(hours=7)  # < 1 whole 8h interval
        contour.run_cycle({position.symbol: 100.0}, {"BTCUSDT": 0.01})
        assert contour.engine.balance == pytest.approx(balance_before)
        assert position.funding_periods_charged == 0

        clock.advance(hours=1)  # exactly 1 whole interval
        contour.run_cycle({position.symbol: 100.0}, {"BTCUSDT": 0.01})
        expected_credit = notional * 0.0001  # +0.01% → SHORT receives
        assert position.funding_periods_charged == 1
        assert position.funding_paid == pytest.approx(-expected_credit)
        assert contour.engine.balance == pytest.approx(
            balance_before + expected_credit,
        )
        # Persisted for restart accounting.
        stored = store.list_positions(status="OPEN")[0]
        assert stored.funding_periods_charged == 1
        assert stored.funding_paid == pytest.approx(-expected_credit)

    def test_short_pays_negative_funding(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        notional = position.entry_price * position.position_size
        balance_before = contour.engine.balance

        clock.advance(hours=8)
        contour.run_cycle({position.symbol: 100.0}, {"BTCUSDT": -0.01})
        expected_debit = notional * 0.0001
        assert position.funding_paid == pytest.approx(+expected_debit)
        assert contour.engine.balance == pytest.approx(
            balance_before - expected_debit,
        )

    def test_funding_is_part_of_net_pnl_at_exit(self):
        settings, store, contour, clock, position = _open_one(price=100.0)
        notional = position.entry_price * position.position_size
        clock.advance(hours=8)
        contour.run_cycle({position.symbol: 100.0}, {"BTCUSDT": 0.01})
        assert position.funding_paid < 0  # SHORT receives

        clock.now = position.deadline_at + timedelta(minutes=1)
        closed = contour.engine.check_exits(
            {position.symbol: 100.0}, {"BTCUSDT": 0.01},
        )
        assert len(closed) == 1
        result = closed[0]
        # 7200 minutes = exactly 15 whole 8-hour funding intervals.
        assert result.funding_periods_charged == 15
        expected_funding = -notional * 0.0001 * 15
        assert result.funding_paid == pytest.approx(expected_funding)

        fill = 100.0 * (1.0 + settings.slippage_percent)
        gross = (100.0 - 100.0) * result.position_size
        exit_fee = fill * result.position_size * settings.taker_fee
        exit_slip = (fill - 100.0) * result.position_size
        expected_net = (
            gross - result.entry_fee - exit_fee - expected_funding
            - (result.entry_slippage_cost + exit_slip)
        )
        assert result.net_pnl == pytest.approx(expected_net)
        # net = gross − all costs; the received funding (negative) ADDS back.
        assert result.net_pnl > -(
            result.entry_fee + exit_fee + result.entry_slippage_cost + exit_slip
        )


# ---------------------------------------------------------------------------
# Fail-closed entries without activation (requirement 10)
# ---------------------------------------------------------------------------

class TestEntriesFailClosed:
    def test_entries_refused_when_activation_row_missing(self):
        settings = _settings()
        store = _store(settings)  # active for capture...
        clock = _Clock(SIGNAL_AT)
        candidate = _candidate()
        assert _capture(store, candidate, clock=clock) == CAPTURED
        # ...but the row disappears before entry (fail closed):
        store.set_activation(None)

        contour = _contour(store, settings, clock)
        assert contour.activated is False
        assert contour.activation_reason == "ACTIVATION_ROW_MISSING"
        cycle = contour.run_cycle({candidate.symbol: 100.0})
        assert cycle["entries"] == 0
        assert contour.engine.open_positions == {}
        assert cycle["pending_events"] == 1  # preserved for later

    def test_entries_refused_on_fingerprint_mismatch(self):
        settings = _settings()
        store = _store(settings, activation=_activation(fingerprint="0" * 64))
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert contour.activated is False
        assert contour.activation_reason == "PROTOCOL_FINGERPRINT_MISMATCH"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0

    def test_engine_double_checks_pre_freeze_events(self):
        """Defense in depth: a stored pre-freeze event never opens."""
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)

        stale = _candidate(
            detected_at=FREEZE_TS - timedelta(hours=1),
        )
        # Bypass capture's own pre-freeze check and write directly:
        event = event_from_candidate(
            stale, capture_route=ROUTE_PRE_FILTER_SOURCE,
            activation=ActivationIdentity(
                experiment_id=EXPERIMENT_ID,
                status="RUNNING",
                protocol_fingerprint=protocol_fingerprint(),
                freeze_ts=FREEZE_TS,
            ),
            captured_at=SIGNAL_AT,
        )
        assert store.save_event(event) == "CAPTURED"

        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == 1  # kept, never entered


# ---------------------------------------------------------------------------
# Dry-run readiness evidence (requirement: pre-activation dry-run)
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_exercises_all_three_exits(self):
        report = run_dry_run(_settings())
        assert report["ok"] is True, report
        assert report["entries"] == 3
        reasons = sorted(c["exit_reason"] for c in report["closed"])
        assert reasons == ["STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT"]
        assert all(
            s == "CAPTURED" for s in report["capture"].values()
        ), report["capture"]
        # SHORT geometry survives the dry run (10% around the ACTUAL entry).
        slip = _settings().slippage_percent
        for closed in report["closed"]:
            expected_entry = closed["entry_market"] * (1.0 - slip)
            assert closed["stop"] == pytest.approx(expected_entry * 1.10, rel=1e-9)
            assert closed["target"] == pytest.approx(expected_entry * 0.90, rel=1e-9)
        snapshot = report["snapshot"]
        assert snapshot["strategy_id"] == "ME_LONG_V2_INVERSE_SHORT_V1"
        assert snapshot["direction"] == "SHORT"
        assert snapshot["closed_trades"] == 3
        assert snapshot["open_positions"] == 0
        # Balance reconstruction: initial + Σ closed net PnL.
        assert snapshot["balance"] == pytest.approx(
            _settings().initial_balance + report["net_pnl"],
            abs=0.05,
        )
        # Entry/deadline immutability was never repaired during the dry run.
        assert report["immutability_repairs"] == 0

    def test_dry_run_is_deterministic(self):
        first = run_dry_run(_settings())
        second = run_dry_run(_settings())
        assert first["ok"] and second["ok"]
        assert first["net_pnl"] == pytest.approx(second["net_pnl"])
        assert [c["exit_reason"] for c in first["closed"]] == [
            c["exit_reason"] for c in second["closed"]
        ]
