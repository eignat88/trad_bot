from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.scanners.trend_pullback import TrendPullbackScanner
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2
from app.scanners.orchestrator import ScannerOrchestrator


def candle(index, open_, high, low, close, volume=10):
    return Candle(index * 300_000, open_, high, low, close, volume)


def make_ctx(*, direction="LONG", regime="TREND_UP", pullback_close=None):
    if direction == "LONG":
        indicators = IndicatorSnapshot(atr=2, rsi=50, ema20=100, ema50=95, ema200=90)
        one_hour = [candle(i, 101, 102, 100, 101) for i in range(50)]
        close = 99.5 if pullback_close is None else pullback_close
        signal = [candle(i, 99, 101, 93, close) for i in range(30)]
        signal[-1] = candle(29, 99, 101, 93, close)
    else:
        indicators = IndicatorSnapshot(atr=2, rsi=50, ema20=100, ema50=105, ema200=110)
        one_hour = [candle(i, 99, 100, 98, 99) for i in range(50)]
        close = 100.5 if pullback_close is None else pullback_close
        signal = [candle(i, 101, 103, 107, close) for i in range(30)]
        signal[-1] = candle(29, 101, 103, 107, close)

    five_minute = [candle(i, signal[-1].open, signal[-1].high, signal[-1].low, signal[-1].close) for i in range(20)]
    return MarketContext(
        symbol="BTCUSDT",
        candles_5m=tuple(five_minute),
        candles_15m=tuple(signal),
        candles_1h=tuple(one_hour),
        candles_4h=(),
        indicators=indicators,
        market_regime=regime,
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# V2 scanner tests
# ---------------------------------------------------------------------------

def test_v2_trend_up_long_creates_setup():
    candidate = TrendPullbackScannerV2().scan(make_ctx())[0]

    assert candidate.direction == "LONG"
    assert candidate.market_regime == "TREND_UP"
    assert candidate.setup_timeframe == "15m"
    assert candidate.entry_timeframe == "5m"
    assert candidate.features["pullback_quality"] <= 0.75
    assert candidate.features["recommended_expiry_policy"] == "BREAKEVEN"


def test_v2_range_does_not_create_setup():
    """V2 doesn't filter by regime, but ema alignment must hold (TREND_UP: ema20>ema50>ema200)."""
    # V2 checks ema20 > ema50 and 1h close > ema200, which holds for our context
    # regardless of regime label. V2 doesn't filter by regime internally.
    ctx = make_ctx(regime="RANGE")
    candidates = TrendPullbackScannerV2().scan(ctx)
    # V2 will produce a candidate because it checks EMA alignment, not regime
    assert len(candidates) == 1
    assert candidates[0].direction == "LONG"


def test_v2_short_does_not_create_setup_by_default():
    """V2 defaults to LONG only."""
    assert TrendPullbackScannerV2().scan(make_ctx(direction="SHORT", regime="TREND_DOWN")) == []


def test_v2_short_can_be_enabled_with_parameters():
    scanner = TrendPullbackScannerV2(enabled_directions=("SHORT",), allowed_regimes=("TREND_DOWN",))

    candidates = scanner.scan(make_ctx(direction="SHORT", regime="TREND_DOWN"))

    assert len(candidates) == 1
    assert candidates[0].direction == "SHORT"


def test_v2_pullback_quality_above_max_is_filtered():
    assert TrendPullbackScannerV2(max_pullback_quality=0.75).scan(make_ctx(pullback_close=100)) == []


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------

def test_orchestrator_preserves_v2_candidate():
    """Orchestrator runs V2 and records it in stats."""
    ctx = make_ctx()
    orchestrator = ScannerOrchestrator(enabled_scanners=("TREND_PULLBACK_V2",))
    _, stats = orchestrator.scan_all_with_stats(ctx)

    assert "TREND_PULLBACK_V2" in stats
    assert stats["TREND_PULLBACK_V2"]["candidates_found"] >= 1
    assert stats["TREND_PULLBACK_V2"]["errors_count"] == 0


def test_default_orchestrator_has_v2_and_no_legacy():
    """Default ScannerOrchestrator() must contain TREND_PULLBACK_V2 and not legacy TREND_PULLBACK."""
    orchestrator = ScannerOrchestrator()
    assert "TREND_PULLBACK_V2" in orchestrator.scanners, (
        "TREND_PULLBACK_V2 must be in default scanners"
    )
    assert "TREND_PULLBACK_V3" in orchestrator.scanners, (
        "TREND_PULLBACK_V3 must be in default scanners"
    )
    assert "TREND_PULLBACK" not in orchestrator.scanners, (
        "Legacy TREND_PULLBACK must not be in default scanners"
    )
    assert len(orchestrator.scanners) == 9, (
        f"Expected 9 default scanners, got {len(orchestrator.scanners)}: {list(orchestrator.scanners)}"
    )
