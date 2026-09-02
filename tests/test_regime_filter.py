"""Tests for the market regime direction filter and TTL multiplier."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.scanners.models import MarketContext, SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator
from app.paper.engine import PaperTradingEngine, PaperTradeRecord, _ENTRY_TIMEOUT_BASE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(regime: str = "RANGE") -> MarketContext:
    """Minimal MarketContext stub with a given regime."""
    return MarketContext(
        symbol="BTCUSDT",
        candles_5m=(),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=MagicMock(),
        market_regime=regime,
        levels=MagicMock(),
        evaluated_at=datetime.now(timezone.utc),
    )


def _candidate(direction: str = "LONG", symbol: str = "BTCUSDT",
               scanner: str = "TREND_PULLBACK_V2", **overrides) -> SetupCandidate:
    defaults = dict(
        scanner_name=scanner, symbol=symbol, direction=direction,
        entry_zone_low=99.0, entry_zone_high=101.0,
        invalidation_price=90.0, target_1=120.0, target_2=None, score=50.0,
        state=SetupState.SETUP_READY,
    )
    defaults.update(overrides)
    return SetupCandidate(**defaults)


class _FakeRepo:
    """Minimal repository stub for PaperTradingEngine (matches test_paper_engine.py)."""

    def __init__(self):
        self.setups: list = []
        self.trades: list = []
        self.closed: list = []

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return {"daily_loss_usdt": 0.0, "consecutive_losses": 0}

    def get_latest_paper_account_snapshot(self):
        return None

    def save_paper_trade(self, trade):
        self.trades.append(trade)
        return len(self.trades)

    def close_paper_trade(self, **kw):
        self.closed.append(kw)

    def update_paper_trade_funding(self, *args):
        pass


# ===========================================================================
# 1. Orchestrator regime filter
# ===========================================================================

class TestOrchestratorRegimeFilter:
    def test_long_allowed_in_trend_up(self):
        ctx = _make_ctx("TREND_UP")
        candidates = [_candidate("LONG")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 1

    def test_short_blocked_in_trend_up(self):
        ctx = _make_ctx("TREND_UP")
        candidates = [_candidate("SHORT")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 0

    def test_short_allowed_in_trend_down(self):
        ctx = _make_ctx("TREND_DOWN")
        candidates = [_candidate("SHORT")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 1

    def test_long_blocked_in_trend_down(self):
        ctx = _make_ctx("TREND_DOWN")
        candidates = [_candidate("LONG")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 0

    def test_both_directions_allowed_in_range(self):
        ctx = _make_ctx("RANGE")
        candidates = [_candidate("LONG"), _candidate("SHORT")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 2

    def test_both_directions_allowed_in_high_volatility(self):
        ctx = _make_ctx("HIGH_VOLATILITY")
        candidates = [_candidate("LONG"), _candidate("SHORT")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 2

    def test_no_regime_passes_all(self):
        ctx = _make_ctx(None)
        candidates = [_candidate("LONG"), _candidate("SHORT")]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 2

    def test_mixed_candidates_partially_filtered(self):
        ctx = _make_ctx("TREND_DOWN")
        candidates = [
            _candidate("LONG", "BTCUSDT"),
            _candidate("SHORT", "ETHUSDT"),
            _candidate("LONG", "SOLUSDT"),
        ]
        result = ScannerOrchestrator._apply_regime_filter(ctx, candidates)
        assert len(result) == 1
        assert result[0].symbol == "ETHUSDT"


# ===========================================================================
# 2. Paper engine regime filter
# ===========================================================================

class TestPaperEngineRegimeFilter:
    def test_long_rejected_in_trend_down(self):
        settings = Settings(regime_filter_enabled=True, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        c = _candidate("LONG", market_regime="TREND_DOWN")
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert opened == []

    def test_short_rejected_in_trend_up(self):
        settings = Settings(regime_filter_enabled=True, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        c = _candidate("SHORT", invalidation_price=110.0, market_regime="TREND_UP")
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert opened == []

    def test_long_allowed_in_trend_up(self):
        settings = Settings(regime_filter_enabled=True, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        c = _candidate("LONG", market_regime="TREND_UP")
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert len(opened) == 1

    def test_both_allowed_in_range(self):
        settings = Settings(regime_filter_enabled=True, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        long_c = _candidate("LONG", scanner="OTHER_SCANNER", market_regime="RANGE")
        opened = engine.check_entries([long_c], {"BTCUSDT": 100.0})
        assert len(opened) == 1

    def test_trend_pullback_rejected_in_range_by_scanner_allowlist(self):
        settings = Settings(regime_filter_enabled=True, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        candidate = _candidate("LONG", market_regime="RANGE")
        assert engine.check_entries([candidate], {"BTCUSDT": 100.0}) == []

    def test_filter_disabled_allows_all(self):
        settings = Settings(regime_filter_enabled=False, max_symbol_exposure=1.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        c = _candidate("LONG", market_regime="TREND_DOWN")
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert len(opened) == 1


# ===========================================================================
# 3. TTL multiplier
# ===========================================================================

class TestTTLMultiplier:
    def test_default_base_timeout_values(self):
        """Base timeout values should match the documented defaults."""
        assert _ENTRY_TIMEOUT_BASE["5m"] == 12
        assert _ENTRY_TIMEOUT_BASE["15m"] == 8
        assert _ENTRY_TIMEOUT_BASE["1h"] == 6
        assert _ENTRY_TIMEOUT_BASE["4h"] == 4

    def test_multiplier_doubles_ttl(self):
        settings = Settings(setup_ttl_multiplier=2.0)
        engine = PaperTradingEngine(settings, _FakeRepo())
        base = _ENTRY_TIMEOUT_BASE["5m"]  # 12
        effective = base * settings.setup_ttl_multiplier  # 24
        # A 5m trade entered 24*5=120 minutes ago should NOT be expired yet
        trade = PaperTradeRecord(
            trade_id=1, setup_id="test", symbol="BTCUSDT",
            scanner_name="TEST", direction="LONG", score=50.0,
            entry_price=100.0, entry_fee=0.0, stop_price=90.0,
            target_1=120.0, target_2=None, position_size=1.0,
            risk_usdt=10.0, balance_before=10000.0,
            market_regime="RANGE",
            entered_at=datetime.now(timezone.utc) - timedelta(minutes=effective * 5 - 1),
            entry_timeframe="5m",
        )
        assert not engine._is_expired(trade)

        # But at effective * 5 + 1 minutes it should be expired
        trade.entered_at = datetime.now(timezone.utc) - timedelta(minutes=effective * 5 + 1)
        assert engine._is_expired(trade)

    def test_half_multiplier_shortens_ttl(self):
        settings = Settings(setup_ttl_multiplier=0.5)
        engine = PaperTradingEngine(settings, _FakeRepo())
        base = _ENTRY_TIMEOUT_BASE["5m"]  # 12
        effective = base * settings.setup_ttl_multiplier  # 6
        trade = PaperTradeRecord(
            trade_id=1, setup_id="test", symbol="BTCUSDT",
            scanner_name="TEST", direction="LONG", score=50.0,
            entry_price=100.0, entry_fee=0.0, stop_price=90.0,
            target_1=120.0, target_2=None, position_size=1.0,
            risk_usdt=10.0, balance_before=10000.0,
            market_regime="RANGE",
            entered_at=datetime.now(timezone.utc) - timedelta(minutes=effective * 5 + 1),
            entry_timeframe="5m",
        )
        assert engine._is_expired(trade)


# ===========================================================================
# 4. EXPIRED_PROFITABLE exit reason
# ===========================================================================

class TestExpiredProfitable:
    def test_profitable_expiry_uses_special_reason(self):
        """When an expired trade has positive gross P&L, the reason should be EXPIRED_PROFITABLE."""
        settings = Settings(slippage_percent=0.0, max_symbol_exposure=1.0)
        repo = _FakeRepo()
        engine = PaperTradingEngine(settings, repo)
        # Open a LONG at 100
        c = _candidate("LONG", target_1=120.0, entry_zone_low=99.0, entry_zone_high=101.0)
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert len(opened) == 1
        trade = opened[0]
        # Set entered_at far in the past to trigger expiry
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=3)
        # Price is above entry → profitable for LONG
        closed = engine.check_exits({"BTCUSDT": 105.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "EXPIRED_PROFITABLE"

    def test_losing_expiry_uses_plain_expired(self):
        """When an expired trade has negative gross P&L, reason should be EXPIRED."""
        settings = Settings(slippage_percent=0.0, max_symbol_exposure=1.0)
        repo = _FakeRepo()
        engine = PaperTradingEngine(settings, repo)
        c = _candidate("LONG", target_1=120.0, entry_zone_low=99.0, entry_zone_high=101.0)
        opened = engine.check_entries([c], {"BTCUSDT": 100.0})
        assert len(opened) == 1
        trade = opened[0]
        trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=3)
        # Price is below entry → losing for LONG
        closed = engine.check_exits({"BTCUSDT": 95.0})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "EXPIRED"
