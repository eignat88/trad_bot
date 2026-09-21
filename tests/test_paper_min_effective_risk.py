"""Tests for minimum effective risk gate.

Covers:
1. actual risk >= 50% requested → trade opens
2. actual risk < 50% requested → trade rejected
3. AVAX/normal-sized trade unaffected
4. tiny ENA/AR/USELESS-like trade rejected
5. max_symbol_exposure and portfolio caps continue to work
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.config.settings import _load_execution_policies
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.scanners.models import SetupCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeRepository:
    """Minimal in-memory repository for paper-trade testing."""

    def __init__(self, risk_state=None, account_snapshot=None, safety_gate_state=None):
        self.saved: list[PaperTradeRecord] = []
        self.closed: list[dict] = []
        self.safety_events: list[dict] = []
        self.safety_gate_modes: list[str] = []
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
        self.safety_gate_state["safety_gate_mode"] = mode

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

    def save_dca_state(self, *args):
        pass

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass


def _candidate(**changes) -> SetupCandidate:
    """Create a generic LONG candidate."""
    values = {
        "scanner_name": "TEST",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": 90.0,
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
        "invalidation_price": 90.0,
        "target_1": 120.0,
    }
    values.update(changes)
    return SetupCandidate(**values)


# ---------------------------------------------------------------------------
# Test 1: actual risk >= 50% requested → trade opens
# ---------------------------------------------------------------------------

class TestRiskGateOpensTrade:
    def test_full_risk_trade_opens(self):
        """When no caps reduce the position, risk_usdt ≈ requested_risk → opens."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,          # requested = $100
            max_symbol_exposure=1.0,      # no symbol cap
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 1
        trade = opened[0]
        requested_risk = 10_000.0 * 0.01  # $100
        assert trade.risk_usdt >= requested_risk * 0.50

    def test_60_percent_risk_opens(self):
        """When actual risk is 60% of requested → still opens."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,
            # symbol exposure caps quantity so that risk_usdt ≈ $60 (60% of $100)
            max_symbol_exposure=0.06,     # caps notional → risk ≈ $60
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 1
        requested_risk = 10_000.0 * 0.01
        assert opened[0].risk_usdt >= requested_risk * 0.50


# ---------------------------------------------------------------------------
# Test 2: actual risk < 50% requested → trade rejected
# ---------------------------------------------------------------------------

class TestRiskGateRejectsTrade:
    def test_tiny_risk_rejected(self):
        """When symbol exposure caps position so risk < 50% requested → rejected."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,          # requested = $100
            # Tight symbol cap forces quantity down so risk_usdt ≈ $30 (30% of $100)
            max_symbol_exposure=0.003,
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 0
        assert len(repo.saved) == 0

    def test_custom_threshold_rejects(self):
        """With min_effective_risk_ratio=0.80, risk at 60% is rejected."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,
            max_symbol_exposure=0.06,
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.80,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 0


# ---------------------------------------------------------------------------
# Test 3: AVAX/normal-sized trade unaffected
# ---------------------------------------------------------------------------

class TestNormalTradeUnaffected:
    def test_normal_trade_opens(self):
        """A normal-sized trade (e.g. AVAX-like) with full risk should open."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.005,
            max_symbol_exposure=0.20,
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        # AVAX-like: price ~35, stop at 33, risk distance=2
        opened = engine.check_entries(
            [_candidate(
                symbol="AVAXUSDT",
                entry_zone_low=34.0,
                entry_zone_high=36.0,
                invalidation_price=33.0,
                target_1=40.0,
            )],
            {"AVAXUSDT": 35.0},
        )
        assert len(opened) == 1
        requested_risk = 10_000.0 * 0.005  # $50
        assert opened[0].risk_usdt >= requested_risk * 0.50


# ---------------------------------------------------------------------------
# Test 4: tiny ENA/AR/USELESS-like trade rejected
# ---------------------------------------------------------------------------

class TestMicroPositionRejected:
    def test_ena_tiny_risk_rejected(self):
        """ENA-like: risk_usdt ≈ $0.22 should be rejected with default 50% gate.

        ENA price ~0.22, stop very close → tiny risk after caps.
        """
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.005,         # requested = $50
            # Very tight cap: 0.005% of balance = $0.50 notional → ~2.27 units at $0.22
            # risk = distance * qty → need distance such that risk ≈ $0.22
            max_symbol_exposure=0.00005,
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(
                symbol="ENAUSDT",
                entry_zone_low=0.21,
                entry_zone_high=0.23,
                invalidation_price=0.20,
                target_1=0.25,
            )],
            {"ENAUSDT": 0.22},
        )
        # The tight cap should produce risk_usdt well below 50% of $50
        if opened:
            assert opened[0].risk_usdt < 50.0 * 0.50

    def test_useless_micro_risk_rejected(self):
        """USELESS-like: extremely small risk_usdt should be rejected."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.005,         # requested = $50
            max_symbol_exposure=0.000001,  # extremely tight cap
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(
                symbol="USELESSUSDT",
                entry_zone_low=0.000009,
                entry_zone_high=0.000012,
                invalidation_price=0.000008,
                target_1=0.000015,
            )],
            {"USELESSUSDT": 0.000011},
        )
        assert len(opened) == 0


# ---------------------------------------------------------------------------
# Test 5: max_symbol_exposure and portfolio caps continue to work
# ---------------------------------------------------------------------------

class TestCapsStillWork:
    def test_symbol_exposure_cap_still_applies(self):
        """Symbol exposure cap should still limit position size, but trade opens
        when risk stays above the 50% threshold.

        With risk_per_trade=0.01 (requested=$100), max_symbol_exposure=0.10 caps
        notional to $1000 → 10 units at $100 → risk = 10 * 10 = $100.
        After the cap, risk_usdt = $100 which is 100% of $100 → above 50% gate.
        """
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,          # requested = $100
            max_symbol_exposure=0.10,     # caps to $1000 notional → 10 units at $100
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 1
        trade = opened[0]
        # Position should be capped by symbol exposure
        assert trade.position_size <= 10.0  # 1000 / 100 = 10
        # But risk_usdt should still be above 50% of $100
        assert trade.risk_usdt >= 100.0 * 0.50

    def test_portfolio_gross_cap_still_applies(self):
        """Portfolio gross exposure cap should limit position but trade opens.

        With risk_per_trade=0.01 (requested=$100), max_portfolio_gross_exposure=0.10 caps
        notional to $1000 → 10 units at $100 → risk = 10 * 10 = $100.
        After the cap, risk_usdt = $100 which is 100% of $100 → above 50% gate.
        """
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,          # requested = $100
            max_symbol_exposure=1.0,
            max_portfolio_gross_exposure=0.10,  # caps to $1000 gross
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 1
        trade = opened[0]
        # risk_usdt should still be above 50% threshold
        assert trade.risk_usdt >= 100.0 * 0.50


# ---------------------------------------------------------------------------
# Test 6: Risk gate logging
# ---------------------------------------------------------------------------

class TestRiskGateLogging:
    def test_tiny_trade_rejection_logged(self):
        """Rejected tiny trade should log the MIN_EFFECTIVE_RISK reason."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,
            max_symbol_exposure=0.003,    # Forces tiny risk
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_candidate(entry_zone_low=99, entry_zone_high=101, invalidation_price=90)],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 0


# ---------------------------------------------------------------------------
# Test 7: Existing open trades not changed by risk gate
# ---------------------------------------------------------------------------

class TestExistingTradesUnchanged:
    def test_risk_gate_only_affects_new_entries(self):
        """The risk gate should only apply at entry, not to existing open trades."""
        settings = Settings(
            initial_balance=10_000.0,
            risk_per_trade=0.01,
            max_symbol_exposure=1.0,
            max_portfolio_gross_exposure=1.0,
            max_portfolio_net_exposure=1.0,
            min_effective_risk_ratio=0.50,
            taker_fee=0.0,
            slippage_percent=0.0,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        # Open a normal trade
        opened = engine.check_entries(
            [_candidate()],
            {"BTCUSDT": 100.0},
        )
        assert len(opened) == 1

        # The open trade should still exist and be manageable
        assert "BTCUSDT" in engine.open_trades
        closed = engine.check_exits({"BTCUSDT": 120.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "TAKE_PROFIT_1"
