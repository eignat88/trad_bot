from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.scanners.trend_pullback import TrendPullbackScanner
from app.scanners.orchestrator import ScannerOrchestrator


def candle(index, open_, high, low, close, volume=10):
    return Candle(index * 300_000, open_, high, low, close, volume)


def make_ctx(*, direction="LONG", regime="TREND_UP", pullback_close=None):
    if direction == "LONG":
        indicators = IndicatorSnapshot(atr=2, rsi=50, ema20=100, ema50=95, ema200=90)
        one_hour = [candle(i, 101, 102, 100, 101) for i in range(50)]
        close = 99.5 if pullback_close is None else pullback_close
        signal = [candle(i, 99, 101, 98, close) for i in range(30)]
        signal[-1] = candle(29, 99, 101, 98, close)
    else:
        indicators = IndicatorSnapshot(atr=2, rsi=50, ema20=100, ema50=105, ema200=110)
        one_hour = [candle(i, 99, 100, 98, 99) for i in range(50)]
        close = 100.5 if pullback_close is None else pullback_close
        signal = [candle(i, 101, 103, 99, close) for i in range(30)]
        signal[-1] = candle(29, 101, 103, 99, close)

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


def test_trend_up_long_creates_15m_setup_with_score_and_features():
    candidate = TrendPullbackScanner().scan(make_ctx())[0]

    assert candidate.direction == "LONG"
    assert candidate.market_regime == "TREND_UP"
    assert candidate.setup_timeframe == "15m"
    assert candidate.entry_timeframe == "15m"
    assert candidate.features["signal_timeframe"] == "15m"
    assert candidate.features["pullback_quality"] <= 0.75
    assert candidate.score > 0
    assert candidate.reasons
    assert candidate.features["target_r"] == pytest.approx(0.75)
    assert candidate.features["risk_r"] == pytest.approx(candidate.entry_zone_high - candidate.invalidation_price)
    assert candidate.features["recommended_expiry_bars"] == 144
    assert candidate.features["recommended_expiry_policy"] == "BREAKEVEN"


def test_range_does_not_create_setup_by_default():
    assert TrendPullbackScanner().scan(make_ctx(regime="RANGE")) == []


def test_short_does_not_create_setup_by_default():
    assert TrendPullbackScanner().scan(make_ctx(direction="SHORT", regime="TREND_DOWN")) == []


def test_short_can_be_enabled_with_parameters():
    scanner = TrendPullbackScanner(enabled_directions=("SHORT",), allowed_regimes=("TREND_DOWN",))

    candidates = scanner.scan(make_ctx(direction="SHORT", regime="TREND_DOWN"))

    assert len(candidates) == 1
    assert candidates[0].direction == "SHORT"


def test_long_target_uses_entry_plus_risk_times_target_r():
    scanner = TrendPullbackScanner(target_r=0.75)
    candidate = scanner.scan(make_ctx())[0]
    risk = candidate.entry_zone_high - candidate.invalidation_price

    assert candidate.target_1 == pytest.approx(candidate.entry_zone_high + risk * 0.75)
    assert candidate.target_2 is None


def test_pullback_quality_above_max_is_filtered():
    assert TrendPullbackScanner(max_pullback_quality=0.75).scan(make_ctx(pullback_close=100)) == []


def test_orchestrator_preserves_scanner_specific_score_and_reasons():
    candidate = TrendPullbackScanner().scan(make_ctx())[0]
    expected_score, expected_reasons = candidate.score, candidate.reasons
    orchestrator = ScannerOrchestrator(enabled_scanners=("TREND_PULLBACK",))
    result, _ = orchestrator.scan_all_with_stats(make_ctx())

    assert result[0].score == expected_score
    assert result[0].reasons == expected_reasons
