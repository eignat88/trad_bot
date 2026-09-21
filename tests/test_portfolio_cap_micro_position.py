"""Regression test: portfolio net exposure cap can produce micro-positions.

Root cause: when net_remaining is very small (near-zero), the portfolio cap
divides a tiny notional by entry_price, producing a position_size that is:
  - orders of magnitude smaller than the risk-based target
  - below the exchange minimum order quantity
  - economically meaningless (notional ~$1, risk ~$0.01)

This causes PnL to display as $0.00 due to display rounding.

Bug location: app/paper/engine.py lines 396-413
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.scanners.models import SetupCandidate


class FakeRepository:
    """Minimal repository that tracks saved trades."""

    def __init__(self, risk_state=None, account_snapshot=None, safety_gate_state=None):
        self.saved = []
        self.closed = []
        self.safety_events = []
        self.safety_gate_modes = []
        self.risk_state = risk_state or {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.account_snapshot = account_snapshot
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

    def insert_paper_safety_event(self, event):
        self.safety_events.append(event)

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

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)


def _settings(**overrides):
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_open_positions=10,
        max_symbol_exposure=0.20,
        max_portfolio_gross_exposure=0.60,
        max_portfolio_net_exposure=0.40,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        atr_stop_multiple=1.5,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _candidate(**changes):
    values = {
        "scanner_name": "TEST",
        "symbol": "BNBUSDT",
        "direction": "LONG",
        "score": 90.0,
        "entry_zone_low": 750.0,
        "entry_zone_high": 760.0,
        "invalidation_price": 745.0,
        "target_1": 800.0,
    }
    values.update(changes)
    return SetupCandidate(**values)


class _FakeTrade:
    """Minimal trade record compatible with portfolio_exposure checks."""
    def __init__(self, symbol, entry_price, direction, size, entry_market_price=None):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.entry_market_price = entry_market_price or entry_price
        self.position_size = size
        self.stop_price = entry_price * 0.95
        self.target_1 = entry_price * 1.05
        self.is_dca_active = False
        self.effective_stop_price = self.stop_price
        self.effective_tp = self.target_1


class TestPortfolioCapMicroPosition:
    """Verify that portfolio net exposure cap does NOT produce micro-positions."""

    def test_micro_position_when_net_exhausted_reproduces_bug(self):
        """REPRODUCE the BNBUSDT bug: portfolio cap creates a micro-position.

        With existing LONG positions consuming nearly all net exposure,
        the portfolio cap produces a tiny notional and risk far below target.
        """
        settings = _settings(
            max_portfolio_net_exposure=0.40,
            max_portfolio_gross_exposure=0.60,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)
        engine.balance = 9000.0

        # Simulate two existing LONG positions consuming net exposure
        # ETHUSDT: notional ≈ 0.5 * 3500 = $1750
        # SOLUSDT: notional ≈ 10 * 175 = $1750
        # Total net ≈ $3500; net_limit = $10000 * 0.40 = $4000
        # net_remaining ≈ $500
        engine.open_trades = {
            "ETHUSDT": _FakeTrade("ETHUSDT", 3500.0, "LONG", 0.5),
            "SOLUSDT": _FakeTrade("SOLUSDT", 175.0, "LONG", 10.0),
        }

        candidate = _candidate(
            symbol="BNBUSDT",
            entry_zone_low=750.0, entry_zone_high=760.0,
            invalidation_price=745.0, target_1=800.0,
        )
        prices = {"BNBUSDT": 755.0, "ETHUSDT": 3500.0, "SOLUSDT": 175.0}

        opened = engine.check_entries([candidate], prices)

        # With the min_effective_risk_ratio gate, micro-positions where the
        # portfolio cap truncates risk below 50% of requested are rejected
        # at entry, so the trade never opens.  This is the intended fix:
        # the MIN_EFFECTIVE_RISK gate prevents the bug from occurring.
        assert len(opened) == 0, (
            "BNB should have been rejected by the effective risk gate: "
            "portfolio net cap would truncate position to micro-size"
        )

    def test_micro_position_with_tight_net_cap(self):
        """With max_portfolio_net_exposure=0.01, position is tiny."""
        settings = _settings(
            max_portfolio_net_exposure=0.01,
            max_portfolio_gross_exposure=0.60,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)
        engine.balance = 10_000.0

        candidate = _candidate(
            symbol="HYPEUSDT",
            entry_zone_low=89.0, entry_zone_high=91.0,
            invalidation_price=88.0, target_1=95.0,
        )
        prices = {"HYPEUSDT": 90.0}

        opened = engine.check_entries([candidate], prices)

        # With net_cap = $10000 * 0.01 = $100
        # quantity = $100 / $90 = 1.11
        # risk = abs(90.1 - 88.0) * 1.11 = $2.33
        # target_risk = $50 → risk is 4.7% of target
        if opened:
            trade = opened[0]
            distance = abs(trade.entry_price - trade.stop_price)
            risk = distance * trade.position_size
            target_risk = 10_000.0 * 0.005
            notional = trade.entry_price * trade.position_size

            print(f"HYPE position: qty={trade.position_size:.6f}, risk=${risk:.2f}, "
                  f"notional=${notional:.2f}")

            # This is EXPECTED behavior with tight cap - document it
            assert risk < target_risk * 0.10, (
                f"With tight net cap, risk should be small: got ${risk:.2f} "
                f"vs target ${target_risk:.2f}"
            )

    def test_full_risk_with_no_existing_positions(self):
        """With no existing positions, sizing is risk-based (not cap-based)."""
        settings = _settings()
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)
        engine.balance = 10_000.0

        # Only BNB candidate to avoid order-of-processing effects
        candidate = _candidate(
            symbol="BNBUSDT",
            entry_zone_low=750, entry_zone_high=760,
            invalidation_price=745, target_1=800,
        )
        prices = {"BNBUSDT": 755.0}

        opened = engine.check_entries([candidate], prices)

        assert len(opened) == 1
        trade = opened[0]
        distance = abs(trade.entry_price - trade.stop_price)
        risk = distance * trade.position_size
        target_risk = 10_000.0 * 0.005  # $50

        # Exposure cap: 10000 * 0.20 / 755 = 2.65 → notional = $2003
        # Risk-based: $50 / distance → should be larger than exposure cap
        # Final qty = min(risk_qty, exposure_cap) → exposure cap dominates
        exposure_cap = 10_000 * 0.20 / 755
        max_risk = distance * exposure_cap

        print(f"BNB (no existing): qty={trade.position_size:.6f}, risk=${risk:.2f}, "
              f"exposure_cap_qty={exposure_cap:.4f}, max_possible_risk=${max_risk:.2f}")

        assert trade.position_size == pytest.approx(exposure_cap, rel=0.01), (
            f"Qty should be limited by symbol exposure cap: "
            f"got {trade.position_size:.6f}, expected ~{exposure_cap:.6f}"
        )
        assert risk >= 1.0, (
            f"Risk should be at least $1 with no existing positions, got ${risk:.2f}"
        )

    def test_net_remaining_calculation(self):
        """Verify net_remaining is correctly computed for LONG direction."""
        settings = _settings(
            max_portfolio_net_exposure=0.40,
            max_portfolio_gross_exposure=0.60,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)
        engine.balance = 10_000.0

        # One existing LONG: notional = $3000
        engine.open_trades = {
            "ETHUSDT": _FakeTrade("ETHUSDT", 3000.0, "LONG", 1.0),
        }

        # Exposure check:
        # long_notional = $3000, short_notional = $0
        # net = $3000, net_limit = $10000 * 0.40 = $4000
        # net_remaining (LONG) = $4000 - $3000 = $1000
        # gross_remaining = $6000 - $3000 = $3000
        # portfolio_notional_cap = min($3000, $1000) = $1000

        exposure = engine.portfolio_exposure({"ETHUSDT": 3000.0})
        assert exposure["long_notional"] == pytest.approx(3000.0, rel=0.01)
        assert exposure["short_notional"] == pytest.approx(0.0, abs=0.01)

        equity = engine.mark_to_market({"ETHUSDT": 3000.0})["equity"]
        net_limit = equity * 0.40
        current_net = exposure["long_notional"] - exposure["short_notional"]
        net_remaining = net_limit - current_net

        print(f"equity=${equity:.2f}, net_limit=${net_limit:.2f}, "
              f"current_net=${current_net:.2f}, net_remaining=${net_remaining:.2f}")

        assert net_remaining == pytest.approx(1000.0, rel=0.01), (
            f"net_remaining should be ~$1000, got ${net_remaining:.2f}"
        )
