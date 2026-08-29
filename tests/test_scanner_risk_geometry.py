from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.breakout_retest import BreakoutRetestScanner
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.scanners.risk_geometry import validate_risk_geometry
from app.scanners.trend_pullback import TrendPullbackScanner


def candle(index, open_, high, low, close, volume=10):
    return Candle(index * 300_000, open_, high, low, close, volume)


def context(*, candles_5m, candles_15m=(), candles_1h=(), indicators):
    return MarketContext(
        symbol="BTCUSDT", candles_5m=tuple(candles_5m), candles_15m=tuple(candles_15m),
        candles_1h=tuple(candles_1h), candles_4h=(), indicators=indicators,
        market_regime="TREND", levels=MarketLevels(), evaluated_at=datetime.now(timezone.utc),
    )


def trend_context(direction, *, atr=2, recent_extreme=None):
    if direction == "LONG":
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=95, ema200=90)
        one_hour = [candle(i, 101, 102, 100, 101) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 100) for i in range(20)]
        low = 98 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 99, 101, low, 100) for i in range(17, 20)]
        five_minute[-1] = candle(19, 99, 101, low, 100)
    else:
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=105, ema200=110)
        one_hour = [candle(i, 99, 100, 98, 99) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 100) for i in range(20)]
        high = 102 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 101, high, 99, 100) for i in range(17, 20)]
        five_minute[-1] = candle(19, 101, high, 99, 100)
    return context(candles_5m=five_minute, candles_15m=[candle(i, 100, 101, 99, 100) for i in range(30)], candles_1h=one_hour, indicators=indicators)


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trend_pullback_returned_candidate_has_valid_geometry(direction):
    candidate = getattr(TrendPullbackScanner(), f"_scan_{direction.lower()}")(trend_context(direction))

    assert candidate is not None
    assert validate_risk_geometry(candidate)[0] is True


@pytest.mark.parametrize(("direction", "recent_extreme"), [("LONG", 100), ("SHORT", 100)])
def test_trend_pullback_rejects_stop_inside_entry_zone(direction, recent_extreme):
    candidate = getattr(TrendPullbackScanner(), f"_scan_{direction.lower()}")(trend_context(direction, recent_extreme=recent_extreme))

    assert candidate is None


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trend_pullback_rejects_target_inside_entry_zone(direction):
    candidate = getattr(TrendPullbackScanner(), f"_scan_{direction.lower()}")(trend_context(direction, atr=0.05))

    assert candidate is None


def test_trend_pullback_selects_ema20_anchor():
    assert TrendPullbackScanner()._pullback_anchor(100, 100, 105) == ("EMA20", 100)


def test_trend_pullback_selects_ema50_anchor():
    assert TrendPullbackScanner()._pullback_anchor(105, 100, 105) == ("EMA50", 105)


def test_trend_pullback_selects_nearest_anchor_when_near_both():
    assert TrendPullbackScanner(pullback_tolerance=0.1)._pullback_anchor(101, 100, 105) == ("EMA20", 100)


def breakout_context(direction, *, atr=2, recent_extreme=None):
    candles_15m = [candle(i, 100, 101, 99, 100) for i in range(30)]
    if direction == "LONG":
        candles_15m[20] = candle(20, 100, 102, 99, 101, 50)
        five_minute = [candle(i, 100, 101, 99, 100) for i in range(20)]
        low = 98 if recent_extreme is None else recent_extreme
        five_minute[-5:] = [candle(i, 99, 101, low, 100) for i in range(15, 20)]
    else:
        candles_15m[20] = candle(20, 100, 101, 98, 99, 50)
        five_minute = [candle(i, 100, 101, 99, 100) for i in range(20)]
        high = 102 if recent_extreme is None else recent_extreme
        five_minute[-5:] = [candle(i, 101, high, 99, 100) for i in range(15, 20)]
    return context(candles_5m=five_minute, candles_15m=candles_15m, indicators=IndicatorSnapshot(atr=atr))


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_breakout_retest_returned_candidate_has_valid_geometry(monkeypatch, direction):
    scanner = BreakoutRetestScanner()
    monkeypatch.setattr(scanner, "_find_breakout_level", lambda *_: 100)
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(breakout_context(direction))

    assert candidate is not None
    assert validate_risk_geometry(candidate)[0] is True


@pytest.mark.parametrize(("direction", "recent_extreme"), [("LONG", 100), ("SHORT", 100)])
def test_breakout_retest_rejects_stop_inside_entry_zone(monkeypatch, direction, recent_extreme):
    scanner = BreakoutRetestScanner()
    monkeypatch.setattr(scanner, "_find_breakout_level", lambda *_: 100)

    assert getattr(scanner, f"_scan_{direction.lower()}")(breakout_context(direction, recent_extreme=recent_extreme)) is None


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_breakout_retest_rejects_target_inside_entry_zone(monkeypatch, direction):
    scanner = BreakoutRetestScanner()
    monkeypatch.setattr(scanner, "_find_breakout_level", lambda *_: 100)

    assert getattr(scanner, f"_scan_{direction.lower()}")(breakout_context(direction, atr=0.05)) is None
