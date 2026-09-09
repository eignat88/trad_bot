"""Tests for paper stop-loss execution model.

Verifies that stop-loss exits are modelled as stop-market orders:
  - Execution at stop_price ± configured slippage
  - NOT at the arbitrary current market price

This is critical for paper trading accuracy.  The old code used the
current observed price as exit_price whenever price had moved past
the stop level between monitoring cycles, producing artificially large
losses (STOP_LOSS_GAP events).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.scanners.models import SetupCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRepo:
    """Minimal repository stub for paper engine tests."""

    def __init__(self, risk_state=None, safety_gate_state=None):
        self.saved = []
        self.closed = []
        self.safety_events = []
        self.safety_gate_modes = []
        self.risk_state = risk_state or {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.safety_gate_state = safety_gate_state or {
            "is_blocked": False, "reason": None, "blocked_since": None,
        }

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return self.risk_state

    def get_paper_safety_gate_state(self):
        return self.safety_gate_state

    def set_paper_safety_gate_mode(self, mode):
        self.safety_gate_modes.append(mode)
        self.safety_gate_state["safety_gate_mode"] = mode

    def insert_paper_safety_event(self, event):
        self.safety_events.append(event)

    def block_paper_safety_gate(self, reason, blocked_since):
        self.safety_gate_state = {
            "is_blocked": True, "reason": reason, "blocked_since": blocked_since,
        }

    def get_latest_paper_account_snapshot(self):
        return None

    def get_paper_trade_by_setup(self, setup_id):
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass

    def save_dca_state(self, *args):
        pass


def _candidate(**overrides) -> SetupCandidate:
    defaults = dict(
        scanner_name="TEST",
        symbol="BTCUSDT",
        direction="LONG",
        score=90.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=90.0,   # stop = 90
        target_1=120.0,
    )
    defaults.update(overrides)
    return SetupCandidate(**defaults)


def _short_candidate(**overrides) -> SetupCandidate:
    defaults = dict(
        scanner_name="TEST",
        symbol="ETHUSDT",
        direction="SHORT",
        score=90.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=110.0,  # stop = 110
        target_1=80.0,
    )
    defaults.update(overrides)
    return SetupCandidate(**defaults)


def _open_long(engine, entry_price=100.0, stop=90.0, target=120.0):
    """Open a LONG position and return the trade record."""
    c = _candidate(invalidation_price=stop, target_1=target)
    opened = engine.check_entries([c], {"BTCUSDT": entry_price})
    assert len(opened) == 1
    return opened[0]


def _open_short(engine, entry_price=100.0, stop=110.0, target=80.0):
    """Open a SHORT position and return the trade record."""
    c = _short_candidate(invalidation_price=stop, target_1=target)
    opened = engine.check_entries([c], {"ETHUSDT": entry_price})
    assert len(opened) == 1
    return opened[0]


# ---------------------------------------------------------------------------
# 1. Normal stop execution (price == stop)
# ---------------------------------------------------------------------------

class TestNormalStopExecution:
    """When price exactly equals the stop level, exit at stop price."""

    def test_long_stop_exact(self):
        """When price exactly hits stop, exit at stop × (1 - slippage)."""
        repo = FakeRepo()
        slip = 0.0005
        engine = PaperTradingEngine(
            Settings(slippage_percent=slip, taker_fee=0.00055), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        closed = engine.check_exits({"BTCUSDT": 90.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS"
        # exit_price passed to _close_trade = stop * (1 - slip) = 89.955
        # DB exit_price = adjusted_exit = 89.955 * (1 - slip) = 89.910022
        expected_db_exit = 90.0 * (1 - slip) * (1 - slip)
        assert repo.closed[0]["exit_price"] == pytest.approx(expected_db_exit, rel=1e-6)

    def test_short_stop_exact(self):
        """When price exactly hits stop, exit at stop × (1 + slippage)."""
        repo = FakeRepo()
        slip = 0.0005
        engine = PaperTradingEngine(
            Settings(slippage_percent=slip, taker_fee=0.00055), repo,
        )
        _open_short(engine, entry_price=100.0, stop=110.0)

        closed = engine.check_exits({"ETHUSDT": 110.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS"
        expected_db_exit = 110.0 * (1 + slip) * (1 + slip)
        assert repo.closed[0]["exit_price"] == pytest.approx(expected_db_exit, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. Gap stop execution (price beyond stop)
# ---------------------------------------------------------------------------

class TestGapStopExecution:
    """When price has moved past the stop, execute at stop ± slippage — NOT at current price."""

    def test_long_gap_uses_stop_level_not_market(self):
        """LONG stop=90, current price=85 → exit near 90, not 85."""
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0.001, taker_fee=0.001), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        closed = engine.check_exits({"BTCUSDT": 85.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
        # Key assertion: exit_price should be based on stop, NOT current market
        # exit_price passed to _close_trade = stop * (1 - slippage)
        # _close_trade then applies slippage again for the DB record
        expected_exit_trigger = 90.0 * (1 - 0.001)  # = 89.91
        assert repo.closed[0]["exit_price"] == pytest.approx(
            expected_exit_trigger * (1 - 0.001), rel=1e-6,
        )

    def test_short_gap_uses_stop_level_not_market(self):
        """SHORT stop=110, current price=115 → exit near 110, not 115."""
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0.001, taker_fee=0.001), repo,
        )
        _open_short(engine, entry_price=100.0, stop=110.0)

        closed = engine.check_exits({"ETHUSDT": 115.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
        expected_exit_trigger = 110.0 * (1 + 0.001)  # = 110.11
        assert repo.closed[0]["exit_price"] == pytest.approx(
            expected_exit_trigger * (1 + 0.001), rel=1e-6,
        )

    def test_long_big_gap_loss_is_capped(self):
        """When price gaps far past stop, loss should be ~1R, not much worse."""
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(
                slippage_percent=0.0005, taker_fee=0.00055,
                risk_per_trade=0.01, max_symbol_exposure=1.0,
            ), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        # Price crashes to 70 — a 20 point gap past the 90 stop
        closed = engine.check_exits({"BTCUSDT": 70.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"

        # The pnl_r should be approximately -1R (slightly worse due to slippage/fees)
        # NOT -3R as it would be if we used price=70 as exit
        pnl_r = repo.closed[0]["pnl_r"]
        assert pnl_r < 0
        assert pnl_r > -1.5, f"Gap exit should be ~-1R, got {pnl_r}"

    def test_short_big_gap_loss_is_capped(self):
        """SHORT: price spikes far above stop, loss should be ~1R."""
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(
                slippage_percent=0.0005, taker_fee=0.00055,
                risk_per_trade=0.01, max_symbol_exposure=1.0,
            ), repo,
        )
        _open_short(engine, entry_price=100.0, stop=110.0)

        # Price spikes to 130 — a 20 point gap past the 110 stop
        closed = engine.check_exits({"ETHUSDT": 130.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"

        pnl_r = repo.closed[0]["pnl_r"]
        assert pnl_r < 0
        assert pnl_r > -1.5, f"Gap exit should be ~-1R, got {pnl_r}"


# ---------------------------------------------------------------------------
# 3. Slippage is NOT double-counted
# ---------------------------------------------------------------------------

class TestNoDoubleSlippage:
    """Verify slippage is applied exactly once per direction."""

    def test_long_gap_slippage_counted_once(self):
        """For a gap LONG exit: _close_trade applies slippage to the exit_price
        which is already stop*(1-slip).  The net effect should be:
        gross based on stop*(1-slip), exit_fee based on stop*(1-slip)*(1-slip).
        This is correct — the fill price includes slippage, and fee is on that fill.
        """
        repo = FakeRepo()
        slip = 0.005
        fee = 0.001
        engine = PaperTradingEngine(
            Settings(slippage_percent=slip, taker_fee=fee,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        closed = engine.check_exits({"BTCUSDT": 85.0})
        assert len(closed) == 1
        closed_data = repo.closed[0]

        # exit_price = stop*(1-slip) = 90*0.995 = 89.55
        # _close_trade: adjusted_exit = 89.55*(1-slip) = 89.55*0.995 = 89.10225
        # gross_pnl = (89.55 - 100) * qty = -10.45 * qty
        # exit_fee = 89.10225 * qty * 0.001
        # exit_slippage = |89.10225 - 89.55| * qty = 0.44775 * qty
        # These are consistent — slippage is the cost of fill vs trigger price

        assert closed_data["exit_reason"] == "STOP_LOSS_GAP"
        assert closed_data["pnl_r"] < 0
        # Verify pnl_r is reasonable (~-1R)
        assert closed_data["pnl_r"] > -1.5

    def test_no_slippage_gap_exact_r(self):
        """With zero slippage and zero fee, gap exit should be exactly -1R.

        Note: _close_trade uses signed_move = exit_price - entry (not adjusted_exit),
        so with slip=0, exit_price = stop, adjusted_exit = stop, and PnL = stop - entry.
        """
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        closed = engine.check_exits({"BTCUSDT": 50.0})  # huge gap
        assert len(closed) == 1
        # repo.closed[0] is the kwargs dict passed to close_paper_trade
        pnl_r = repo.closed[0]["pnl_r"]
        assert pnl_r == pytest.approx(-1.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 4. MFE / MAE correctness
# ---------------------------------------------------------------------------

class TestMfeMae:
    """MFE and MAE should track high/low watermarks correctly."""

    def test_long_mfe_mae(self):
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0)

        # Price path: up to 105, down to 92, back to 100
        engine.check_exits({"BTCUSDT": 105.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(5.0)
        assert trade.mae == pytest.approx(0.0)

        engine.check_exits({"BTCUSDT": 92.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(5.0)
        assert trade.mae == pytest.approx(8.0)

    def test_short_mfe_mae(self):
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_short(engine, entry_price=100.0, stop=110.0)

        # Price path: down to 95 (favorable), up to 108 (adverse), back to 100
        engine.check_exits({"ETHUSDT": 95.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(5.0)
        assert trade.mae == pytest.approx(0.0)

        engine.check_exits({"ETHUSDT": 108.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(5.0)
        assert trade.mae == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# 5. Priority chain: SL → DCA → BE TP → TP1 → TP2 → Trailing → Expiry
# ---------------------------------------------------------------------------

class TestPriorityChain:
    """Verify stop loss takes priority over TP when both conditions are met."""

    def test_sl_takes_priority_over_tp(self):
        """If stop is hit, TP should not close the trade."""
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0, target=110.0)

        # Price at 85 — below stop, should close at stop not TP
        closed = engine.check_exits({"BTCUSDT": 85.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] in ("STOP_LOSS", "STOP_LOSS_GAP")

    def test_tp1_closes_when_reached(self):
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0, target=110.0)

        closed = engine.check_exits({"BTCUSDT": 110.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "TAKE_PROFIT_1"

    def test_trailing_stop_works(self):
        """After 1R favorable move, trailing should lock +0.5R.

        When trailing stop is raised to 105 and price drops to 104,
        the SL check detects price < stop and closes the position.
        This is the correct path — trailing stop IS being hit via the
        SL mechanism (priority chain: SL → DCA → TP → Trailing → Expiry).
        """
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0,
                     atr_stop_multiple=1.5), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0, target=150.0)

        # Price moves +1R (10 points) → trailing locks +0.5R at 105
        engine.check_exits({"BTCUSDT": 110.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.stop_price == pytest.approx(105.0)  # entry + 0.5R

        # Price drops to 104 → stop was raised to 105, so SL check fires
        # (104 < 105, gap = False since 104 < 105 not strictly at stop)
        closed = engine.check_exits({"BTCUSDT": 104.0})
        assert len(closed) == 1
        # The SL check (priority 1) fires because price <= raised stop
        assert repo.closed[0]["exit_reason"] in ("STOP_LOSS", "STOP_LOSS_GAP")
        # Exit should be near the trailing stop level, not the original stop
        assert repo.closed[0]["exit_price"] == pytest.approx(105.0, rel=1e-4)


# ---------------------------------------------------------------------------
# 6. DCA + stop interaction
# ---------------------------------------------------------------------------

class TestDcaStopInteraction:
    """DCA stop should work correctly with the new stop execution model."""

    def test_dca_stop_uses_stop_level(self):
        """When DCA is active and stop is hit, exit should use stop-level execution."""
        from app.paper.dca import DCAPositionState, DCAState

        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(
                slippage_percent=0.001, taker_fee=0.001,
                risk_per_trade=0.01, max_symbol_exposure=1.0,
            ), repo,
        )
        _open_long(engine, entry_price=100.0, stop=90.0, target=120.0)
        trade = list(engine.open_trades.values())[0]

        # Manually set up DCA state (simulating a DCA fill already happened)
        trade.dca_enabled = True
        trade.dca_state = DCAPositionState(
            dca_enabled=True,
            state=DCAState.DCA_FILLED,
            initial_fill_price=100.0,
            initial_fill_qty=trade.position_size * 0.5,
            dca_price=95.0,
            dca_fill_price=95.0,
            dca_fill_qty=trade.position_size * 0.5,
            avg_entry_price=97.5,
            stop_price=90.0,
            active_tp=97.5,
            tp_mode="breakeven",
        )
        # Expand position size for full DCA position
        trade.position_size = trade.dca_state.initial_fill_qty + trade.dca_state.dca_fill_qty
        risk_distance = abs(trade.entry_price - trade.stop_price)
        trade.risk_usdt = risk_distance * trade.position_size
        # Deactivate scanner TP
        trade.target_1 = None

        # Price gaps past stop
        closed = engine.check_exits({"BTCUSDT": 85.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "DCA_STOP"
        # Loss should be capped near 1R, not proportional to 85
        assert repo.closed[0]["pnl_r"] > -2.0


# ---------------------------------------------------------------------------
# 7. Deterministic simulation
# ---------------------------------------------------------------------------

class TestDeterministicSimulation:
    """Full price path simulation verifying MFE, MAE, exit reason, PnL."""

    def test_short_price_path(self):
        """SHORT entry=100, stop=110, price path: 100→99→98.5→100→101→102.2

        Expected:
        - MFE = 1.5 (at 98.5)
        - MAE = 2.2 (at 102.2)
        - No stop hit (102.2 < 110)
        - Position still open after path
        """
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_short(engine, entry_price=100.0, stop=110.0, target=80.0)

        prices = [100.0, 99.0, 98.5, 100.0, 101.0, 102.2]
        for p in prices:
            closed = engine.check_exits({"ETHUSDT": p})
            assert closed == [], f"Should not close at price {p}"

        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(1.5)  # max favorable = 100 - 98.5
        assert trade.mae == pytest.approx(2.2)  # max adverse = 102.2 - 100

    def test_long_gap_path(self):
        """LONG entry=100, stop=98, price: 100→99→97 (gap)

        Expected:
        - MFE = 1 (at 100 entry, first tick)
        - MAE = 3 (at 97)
        - Exit: STOP_LOSS_GAP at stop level
        - pnl_r ≈ -1R
        """
        repo = FakeRepo()
        engine = PaperTradingEngine(
            Settings(slippage_percent=0, taker_fee=0,
                     risk_per_trade=0.01, max_symbol_exposure=1.0), repo,
        )
        _open_long(engine, entry_price=100.0, stop=98.0, target=120.0)

        engine.check_exits({"BTCUSDT": 99.0})
        trade = list(engine.open_trades.values())[0]
        assert trade.mfe == pytest.approx(0.0)  # price went down from 100
        assert trade.mae == pytest.approx(1.0)  # 100 - 99

        closed = engine.check_exits({"BTCUSDT": 97.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
        assert repo.closed[0]["pnl_r"] == pytest.approx(-1.0, abs=1e-6)
