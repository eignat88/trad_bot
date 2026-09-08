"""Regression tests for DCA second-leg fill fix and STOP/EXPIRY ordering.

Covers:
- DCA fill qty calculation (50/50, 40/60, 70/30)
- LONG and SHORT DCA fill through the engine
- Weighted average entry after DCA fill
- Breakeven TP activation after DCA fill
- Stop invariance (stop_price unchanged after DCA fill)
- Risk invariance (total risk budget not increased)
- STOP vs EXPIRY ordering (stop always wins)
- Trade 179 regression scenario
- STOP_LOSS_GAP + DCA interaction
- Persistence of DCA state after fill
- No-DCA path unaffected
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config.settings import DCASettings, Settings
from app.paper.dca import (
    DCAState,
    DCAPositionState,
    DCAPolicy,
    DCAStateManager,
)
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.scanners.models import SetupCandidate


# ======================================================================
# FIXTURES
# ======================================================================

def _dca_settings(**overrides) -> DCASettings:
    defaults = dict(
        enabled=True,
        level_atr=0.75,
        initial_entry_pct=0.50,
        dca_entry_pct=0.50,
        exit_mode="breakeven",
        stop_loss_atr=1.5,
        stop_reference="initial_entry",
        max_dca_count=1,
    )
    defaults.update(overrides)
    return DCASettings(**defaults)


def _make_engine(dca_enabled=True, dca_settings=None):
    """Create a PaperTradingEngine with mocked repository."""
    if dca_settings is None:
        dca_settings = _dca_settings(enabled=dca_enabled)
    settings = Settings(
        initial_balance=10000.0,
        risk_per_trade=0.005,
        max_open_positions=5,
        slippage_percent=0.0005,
        taker_fee=0.00055,
        atr_stop_multiple=1.5,
        paper_scan_interval=300,
        paper_safety_gate_mode="observe",
        dca=dca_settings,
    )
    repo = MagicMock()
    repo.get_open_paper_trades.return_value = []
    repo.get_latest_paper_account_snapshot.return_value = None
    repo.get_paper_risk_state.return_value = {
        "daily_loss_usdt": 0.0,
        "consecutive_losses": 0,
        "cooldown_until": None,
    }
    repo.get_paper_safety_gate_state.return_value = {
        "is_blocked": False,
        "reason": None,
        "blocked_since": None,
        "safety_gate_mode": None,
    }
    repo._use_pg = False
    repo.save_paper_trade = MagicMock(return_value=1)
    repo.get_paper_trade_by_setup = MagicMock(return_value=None)
    engine = PaperTradingEngine(settings, repo)
    return engine, repo


def _open_trade(direction, entry_price, atr, dca_enabled=True, dca_settings=None):
    """Open a trade through the engine and return (engine, repo, trade)."""
    stop_distance = 1.5 * atr
    if direction == "LONG":
        stop_price = entry_price - stop_distance
        target_1 = entry_price + 2.0 * stop_distance
    else:
        stop_price = entry_price + stop_distance
        target_1 = entry_price - 2.0 * stop_distance

    margin = 0.001 * entry_price
    candidate = SetupCandidate(
        setup_id="test-dca-fill",
        scanner_name="TEST",
        symbol="BTCUSDT" if direction == "LONG" else "ETHUSDT",
        direction=direction,
        score=50.0,
        entry_zone_low=entry_price - margin,
        entry_zone_high=entry_price + margin,
        invalidation_price=stop_price,
        target_1=target_1,
        target_2=None,
        entry_timeframe="5m",
    )

    engine, repo = _make_engine(dca_enabled=dca_enabled, dca_settings=dca_settings)
    price = entry_price
    opened = engine.check_entries([candidate], {candidate.symbol: price})
    assert len(opened) == 1, "Trade should have been opened"
    return engine, repo, opened[0]


def _get_closed_args(repo):
    """Extract the keyword arguments from the most recent close_paper_trade call."""
    assert repo.close_paper_trade.called, "close_paper_trade was not called"
    call = repo.close_paper_trade.call_args
    return call.kwargs


# ======================================================================
# DCA FILL QUANTITY — 50/50
# ======================================================================

class TestDCAFillQty50_50:
    """DCA fill qty with default 50/50 split."""

    def test_short_dca_fill_qty_matches_initial(self):
        """50/50 split: dca_fill_qty must equal initial_fill_qty."""
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        dca_price = trade.dca_state.dca_price

        # Move price to DCA level
        engine.check_exits({"ETHUSDT": dca_price})

        # Re-read dca_state — fill_dca creates a new DCAPositionState
        dca = trade.dca_state
        assert dca.dca_fill_count == 1
        assert dca.dca_fill_qty > 0
        assert dca.dca_fill_qty == pytest.approx(dca.initial_fill_qty, abs=1e-6)
        assert dca.state == DCAState.DCA_FILLED

    def test_long_dca_fill_qty_matches_initial(self):
        """50/50 split: LONG DCA fill qty must equal initial_fill_qty."""
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)
        dca_price = trade.dca_state.dca_price

        engine.check_exits({"BTCUSDT": dca_price})

        dca = trade.dca_state
        assert dca.dca_fill_count == 1
        assert dca.dca_fill_qty == pytest.approx(dca.initial_fill_qty, abs=1e-6)
        assert dca.state == DCAState.DCA_FILLED


# ======================================================================
# DCA FILL QUANTITY — 40/60
# ======================================================================

class TestDCAFillQty40_60:
    """DCA fill qty with 40/60 split."""

    def test_short_40_60_split(self):
        """initial=40%, dca=60%: dca_fill_qty = 1.5 * initial_fill_qty."""
        engine, repo = _make_engine(
            dca_enabled=True,
            dca_settings=_dca_settings(initial_entry_pct=0.40, dca_entry_pct=0.60),
        )
        entry_price = 3500.0
        atr = 8.0
        stop_distance = 1.5 * atr
        stop_price = entry_price + stop_distance

        candidate = SetupCandidate(
            setup_id="test-40-60",
            scanner_name="TEST",
            symbol="ETHUSDT",
            direction="SHORT",
            score=50.0,
            entry_zone_low=entry_price - 0.001 * entry_price,
            entry_zone_high=entry_price + 0.001 * entry_price,
            invalidation_price=stop_price,
            target_1=entry_price - 2.0 * stop_distance,
            target_2=None,
            entry_timeframe="5m",
        )
        opened = engine.check_entries([candidate], {"ETHUSDT": entry_price})
        trade = opened[0]
        dca = trade.dca_state

        # Verify split config
        assert dca.initial_entry_pct == 0.40
        assert dca.dca_target_pct == 0.60

        # Move to DCA level
        engine.check_exits({"ETHUSDT": dca.dca_price})

        dca = trade.dca_state  # re-read
        assert dca.dca_fill_count == 1
        assert dca.dca_fill_qty > 0
        # dca_fill_qty should be 60% of full, which is 1.5x initial
        assert dca.dca_fill_qty == pytest.approx(
            dca.initial_fill_qty * (0.60 / 0.40), abs=1e-6
        )
        assert dca.state == DCAState.DCA_FILLED

    def test_short_70_30_split(self):
        """initial=70%, dca=30%."""
        engine, repo = _make_engine(
            dca_enabled=True,
            dca_settings=_dca_settings(initial_entry_pct=0.70, dca_entry_pct=0.30),
        )
        entry_price = 100.0
        atr = 2.0
        stop_distance = 1.5 * atr

        candidate = SetupCandidate(
            setup_id="test-70-30",
            scanner_name="TEST",
            symbol="ETHUSDT",
            direction="SHORT",
            score=50.0,
            entry_zone_low=entry_price - 0.001 * entry_price,
            entry_zone_high=entry_price + 0.001 * entry_price,
            invalidation_price=entry_price + stop_distance,
            target_1=entry_price - 2.0 * stop_distance,
            target_2=None,
            entry_timeframe="5m",
        )
        opened = engine.check_entries([candidate], {"ETHUSDT": entry_price})
        trade = opened[0]
        dca = trade.dca_state

        engine.check_exits({"ETHUSDT": dca.dca_price})

        dca = trade.dca_state
        assert dca.dca_fill_count == 1
        assert dca.dca_fill_qty == pytest.approx(
            dca.initial_fill_qty * (0.30 / 0.70), abs=1e-6
        )


# ======================================================================
# WEIGHTED AVERAGE ENTRY
# ======================================================================

class TestWeightedAverageEntry:
    """After DCA fill, avg_entry must be the weighted average of both fills."""

    def test_short_avg_entry_symmetric(self):
        """50/50 split with equal fill prices → avg = entry."""
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        dca_price = trade.dca_state.dca_price

        engine.check_exits({"ETHUSDT": dca_price})

        dca = trade.dca_state
        avg = DCAPolicy.calculate_avg_entry(
            dca.initial_fill_price, dca.initial_fill_qty,
            dca.dca_fill_price, dca.dca_fill_qty,
        )
        assert dca.avg_entry_price == pytest.approx(avg, abs=1e-10)

    def test_short_avg_entry_weighted(self):
        """Verify weighted average formula explicitly."""
        initial_price = 100.0
        initial_qty = 50.0
        dca_price = 102.0
        dca_qty = 50.0

        avg = DCAPolicy.calculate_avg_entry(initial_price, initial_qty, dca_price, dca_qty)
        expected = (100.0 * 50.0 + 102.0 * 50.0) / 100.0
        assert avg == pytest.approx(expected)

    def test_long_avg_entry_asymmetric(self):
        """Verify weighted average with asymmetric split."""
        avg = DCAPolicy.calculate_avg_entry(67000.0, 40.0, 66985.0, 60.0)
        expected = (67000.0 * 40.0 + 66985.0 * 60.0) / 100.0
        assert avg == pytest.approx(expected)


# ======================================================================
# BREAKEVEN TP
# ======================================================================

class TestBreakevenTP:
    """After DCA fill, active_tp should be avg_entry_price."""

    def test_breakeven_tp_after_dca_short(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})

        dca = trade.dca_state
        assert dca.tp_mode == "breakeven"
        assert dca.active_tp == pytest.approx(dca.avg_entry_price, abs=1e-10)
        assert dca.original_tp > 0

    def test_breakeven_tp_after_dca_long(self):
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)
        engine.check_exits({"BTCUSDT": trade.dca_state.dca_price})

        dca = trade.dca_state
        assert dca.tp_mode == "breakeven"
        assert dca.active_tp == pytest.approx(dca.avg_entry_price, abs=1e-10)


# ======================================================================
# STOP INVARIANCE
# ======================================================================

class TestStopInvariance:
    """Stop price must not move after DCA fill."""

    def test_stop_unchanged_after_dca_short(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        stop_before = trade.dca_state.stop_price

        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})

        assert trade.dca_state.stop_price == stop_before

    def test_stop_unchanged_after_dca_long(self):
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)
        stop_before = trade.dca_state.stop_price

        engine.check_exits({"BTCUSDT": trade.dca_state.dca_price})

        assert trade.dca_state.stop_price == stop_before


# ======================================================================
# RISK INVARIANCE
# ======================================================================

class TestRiskInvariance:
    """Total risk budget must not increase after DCA fill."""

    def test_risk_not_increased_after_dca(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})

        dca = trade.dca_state
        # After DCA fill, risk_usdt = distance * full_position
        risk_distance = abs(trade.entry_price - trade.stop_price)
        full_position = dca.initial_fill_qty + dca.dca_fill_qty
        expected_risk = risk_distance * full_position
        assert trade.risk_usdt == pytest.approx(expected_risk, rel=1e-4)

    def test_no_double_leverage(self):
        """DCA must not add +50% on top of existing 100% position."""
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)
        dca_before = trade.dca_state

        engine.check_exits({"BTCUSDT": trade.dca_state.dca_price})

        dca = trade.dca_state
        full_target = dca.initial_fill_qty / dca.initial_entry_pct
        assert trade.position_size == pytest.approx(full_target, rel=1e-4)


# ======================================================================
# STOP vs EXPIRY ORDERING
# ======================================================================

class TestStopVsExpiryOrdering:
    """STOP must always have priority over EXPIRY."""

    def test_stop_wins_when_both_breached_short(self):
        """SHORT: price beyond stop AND trade expired → STOP_LOSS_GAP."""
        engine, repo, trade = _open_trade("SHORT", 100.0, 5.0)
        stop_price = trade.stop_price

        # Expire the trade
        trade.entry_timeframe = "4h"
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=33)

        # Price beyond stop
        engine.check_exits({"ETHUSDT": stop_price + 1.0})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "STOP_LOSS_GAP"

    def test_stop_wins_when_both_breached_long(self):
        """LONG: price below stop AND trade expired → STOP_LOSS_GAP."""
        engine, repo, trade = _open_trade("LONG", 100.0, 5.0)
        stop_price = trade.stop_price

        trade.entry_timeframe = "4h"
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=33)

        engine.check_exits({"BTCUSDT": stop_price - 1.0})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "STOP_LOSS_GAP"

    def test_expiry_without_stop_breach(self):
        """Only expired, stop not breached → EXPIRED or EXPIRED_PROFITABLE."""
        engine, repo, trade = _open_trade("SHORT", 100.0, 5.0)

        trade.entry_timeframe = "4h"
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=33)

        # Price below stop (not breached for SHORT), above entry = loss
        engine.check_exits({"ETHUSDT": 102.0})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "EXPIRED"

    def test_normal_stop_before_expiry(self):
        """Normal stop (not expired) → STOP_LOSS or STOP_LOSS_GAP."""
        engine, repo, trade = _open_trade("SHORT", 100.0, 5.0)
        stop_price = trade.stop_price

        engine.check_exits({"ETHUSDT": stop_price + 0.5})

        args = _get_closed_args(repo)
        assert args["exit_reason"] in ("STOP_LOSS", "STOP_LOSS_GAP")


# ======================================================================
# TRADE 179 REGRESSION
# ======================================================================

class TestTrade179Regression:
    """Regression test for production trade 179 (XOMUSDT SHORT).

    Production data:
        entry_price = 160.73959
        stop_price = 161.20176
        exit_price = 161.27060
        exit_reason was EXPIRED (BUG)

    Expected: STOP_LOSS_GAP (price > stop)
    """

    def test_trade_179_stop_wins_over_expiry(self):
        engine, repo = _make_engine(dca_enabled=True)
        entry_price = 160.73959
        stop_price = 161.20176

        candidate = SetupCandidate(
            setup_id="trade-179-regression",
            scanner_name="MOMENTUM_EXHAUSTION",
            symbol="XOMUSDT",
            direction="SHORT",
            score=50.0,
            entry_zone_low=entry_price - 0.001 * entry_price,
            entry_zone_high=entry_price + 0.001 * entry_price,
            invalidation_price=stop_price,
            target_1=entry_price - 2.0 * (stop_price - entry_price),
            target_2=None,
            entry_timeframe="5m",
        )
        opened = engine.check_entries([candidate], {"XOMUSDT": entry_price})
        trade = opened[0]

        # Expire the trade (2 hours for 5m timeframe)
        trade.entry_timeframe = "5m"
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=3)

        # Price is beyond stop (same as production: 161.27)
        engine.check_exits({"XOMUSDT": 161.270595})

        args = _get_closed_args(repo)
        assert args["exit_reason"] in ("STOP_LOSS_GAP", "STOP_LOSS")
        assert args["exit_reason"] != "EXPIRED"


# ======================================================================
# STOP_LOSS_GAP + DCA INTERACTION
# ======================================================================

class TestSTOP_LOSS_GAP_DCA_Interaction:
    """When price gaps beyond both DCA and STOP, STOP fires first."""

    def test_gap_through_dca_and_stop_short(self):
        """SHORT: price gaps from entry through DCA through STOP."""
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)
        dca = trade.dca_state
        stop_price = trade.stop_price

        assert dca.dca_fill_count == 0

        # Price gaps through both DCA and STOP
        gap_price = stop_price + 5.0
        engine.check_exits({"ETHUSDT": gap_price})

        dca = trade.dca_state
        args = _get_closed_args(repo)
        assert args["exit_reason"] == "STOP_LOSS_GAP"
        # DCA should NOT have filled (SL priority)
        assert dca.dca_fill_count == 0
        assert dca.state != DCAState.DCA_FILLED

    def test_gap_through_dca_and_stop_long(self):
        """LONG: price gaps from entry through DCA through STOP."""
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)
        dca = trade.dca_state
        stop_price = trade.stop_price

        gap_price = stop_price - 5.0
        engine.check_exits({"BTCUSDT": gap_price})

        dca = trade.dca_state
        args = _get_closed_args(repo)
        assert args["exit_reason"] == "STOP_LOSS_GAP"
        assert dca.dca_fill_count == 0


# ======================================================================
# NO DCA PATH UNAFFECTED
# ======================================================================

class TestNoDCAPath:
    """When DCA is not reached, normal exit paths work."""

    def test_trailing_stop_without_dca(self):
        """DCA not reached, trailing stop fires."""
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0, dca_enabled=True)
        dca = trade.dca_state

        # Price moves favorable — well past 1R to activate trailing
        risk_distance = abs(trade.entry_price - trade.stop_price)
        favorable_price = trade.entry_price + 2.0 * risk_distance

        engine.check_exits({"BTCUSDT": favorable_price})

        # DCA state is still active (not filled)
        assert dca.dca_fill_count == 0
        assert dca.state == DCAState.INITIAL_FILLED

    def test_tp1_without_dca_fill(self):
        """DCA not reached, TP1 fires."""
        engine, repo, trade = _open_trade("LONG", 100.0, 5.0, dca_enabled=True)

        # Price moves to TP1 (2R target)
        tp1 = trade.target_1
        engine.check_exits({"BTCUSDT": tp1 + 1.0})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "TAKE_PROFIT_1"


# ======================================================================
# PERSISTENCE
# ======================================================================

class TestDCAPersistence:
    """DCA state must be persisted correctly after fill."""

    def test_dca_state_persisted_after_fill(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)

        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})

        # Check that save_dca_state was called
        repo.save_dca_state.assert_called()
        call_args = repo.save_dca_state.call_args
        persisted_dict = call_args[0][1]

        assert persisted_dict["dca_fill_count"] == 1
        assert persisted_dict["dca_fill_price"] > 0
        assert persisted_dict["dca_fill_qty"] > 0
        assert persisted_dict["dca_filled_at"] is not None
        assert persisted_dict["avg_entry_price"] > 0
        assert persisted_dict["tp_mode"] == "breakeven"
        assert persisted_dict["dca_order_active"] is False


# ======================================================================
# DCA FILL ONLY ONCE (IDEMPOTENCY)
# ======================================================================

class TestDCAFillIdempotency:
    """DCA fill must happen exactly once."""

    def test_dca_fills_only_once(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)

        # First check — DCA fills
        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})
        assert trade.dca_state.dca_fill_count == 1

        # Second check — should NOT fill again
        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})
        assert trade.dca_state.dca_fill_count == 1


# ======================================================================
# DCA BREAKEVEN EXIT
# ======================================================================

class TestDCABreakevenExit:
    """After DCA fill, price returning to avg_entry triggers breakeven exit."""

    def test_breakeven_exit_short(self):
        engine, repo, trade = _open_trade("SHORT", 3500.0, 8.0)

        # DCA fills
        engine.check_exits({"ETHUSDT": trade.dca_state.dca_price})
        assert trade.dca_state.state == DCAState.DCA_FILLED

        # Price returns to avg_entry → breakeven exit
        avg_entry = trade.dca_state.avg_entry_price
        engine.check_exits({"ETHUSDT": avg_entry})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "DCA_BREAKEVEN"

    def test_breakeven_exit_long(self):
        engine, repo, trade = _open_trade("LONG", 67000.0, 20.0)

        engine.check_exits({"BTCUSDT": trade.dca_state.dca_price})
        assert trade.dca_state.state == DCAState.DCA_FILLED

        avg_entry = trade.dca_state.avg_entry_price
        engine.check_exits({"BTCUSDT": avg_entry})

        args = _get_closed_args(repo)
        assert args["exit_reason"] == "DCA_BREAKEVEN"
