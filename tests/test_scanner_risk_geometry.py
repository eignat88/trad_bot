from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.breakout_retest import BreakoutRetestScanner
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels, SetupCandidate, SetupState
from app.scanners.risk_geometry import validate_risk_geometry
from app.scanners.trend_pullback import TrendPullbackScanner
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2


def candle(index, open_, high, low, close, volume=10):
    return Candle(index * 300_000, open_, high, low, close, volume)


def context(*, candles_5m, candles_15m=(), candles_1h=(), indicators, market_regime="TREND_UP"):
    return MarketContext(
        symbol="BTCUSDT", candles_5m=tuple(candles_5m), candles_15m=tuple(candles_15m),
        candles_1h=tuple(candles_1h), candles_4h=(), indicators=indicators,
        market_regime=market_regime, levels=MarketLevels(), evaluated_at=datetime.now(timezone.utc),
    )


def trend_context_v2(direction, *, atr=2, recent_extreme=None):
    """Build a context that V2 can scan and produce valid risk geometry."""
    if direction == "LONG":
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=95, ema200=90)
        one_hour = [candle(i, 101, 102, 100, 101) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 99.5) for i in range(20)]
        low = 93 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 99, 101, low, 99.5) for i in range(17, 20)]
        five_minute[-1] = candle(19, 99, 101, low, 99.5)
        candles_15m = [candle(i, 99, 101, low, 99.5) for i in range(30)]
    else:
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=105, ema200=110)
        one_hour = [candle(i, 99, 100, 98, 99) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 100.5) for i in range(20)]
        high = 107 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 101, high, 99, 100.5) for i in range(17, 20)]
        five_minute[-1] = candle(19, 101, high, 99, 100.5)
        candles_15m = [candle(i, 101, high, 99, 100.5) for i in range(30)]
    market_regime = "TREND_UP" if direction == "LONG" else "TREND_DOWN"
    return context(
        candles_5m=five_minute,
        candles_15m=candles_15m,
        candles_1h=one_hour,
        indicators=indicators,
        market_regime=market_regime,
    )


def trend_pullback_scanner_v2(direction):
    """Create a V2 scanner configured for the given direction."""
    regime = "TREND_UP" if direction == "LONG" else "TREND_DOWN"
    return TrendPullbackScannerV2(allowed_regimes=(regime,))


# ---------------------------------------------------------------------------
# V2 scanner: validates geometry via orchestrator, not internally
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trend_pullback_v2_returned_candidate_has_valid_geometry(direction):
    scanner = trend_pullback_scanner_v2(direction)
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(trend_context_v2(direction))

    assert candidate is not None
    assert validate_risk_geometry(candidate)[0] is True


@pytest.mark.parametrize(("direction", "recent_extreme"), [("LONG", 100), ("SHORT", 100)])
def test_trend_pullback_v2_stop_inside_entry_zone_not_rejected_internally(direction, recent_extreme):
    """V2 does not reject bad geometry internally; it relies on the orchestrator."""
    scanner = trend_pullback_scanner_v2(direction)
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(
        trend_context_v2(direction, recent_extreme=recent_extreme),
    )
    # V2 returns a candidate; the orchestrator's validate_risk_geometry rejects it.
    assert candidate is not None
    assert validate_risk_geometry(candidate)[0] is False


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trend_pullback_v2_target_zero_produces_valid_candidate(direction):
    """With target_r=0, V2 produces a candidate (target validation is external)."""
    scanner = TrendPullbackScannerV2(
        allowed_regimes=("TREND_UP",) if direction == "LONG" else ("TREND_DOWN",),
        target_r=0,
    )
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(trend_context_v2(direction))
    # V2 doesn't reject target inside entry zone internally
    assert candidate is not None


# ---------------------------------------------------------------------------
# V1 legacy scanner: internal geometry checks (deprecated, target_r=None bug)
# ---------------------------------------------------------------------------

def _v1_context(direction, *, atr=2, recent_extreme=None):
    """Build a context for V1 scanner (uses signal candles for invalidation)."""
    if direction == "LONG":
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=95, ema200=90)
        one_hour = [candle(i, 101, 102, 100, 101) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 99.5) for i in range(20)]
        low = 98 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 99, 101, low, 99.5) for i in range(17, 20)]
        five_minute[-1] = candle(19, 99, 101, low, 99.5)
        candles_15m = [candle(i, 99, 101, low, 99.5) for i in range(30)]
    else:
        indicators = IndicatorSnapshot(atr=atr, rsi=50, ema20=100, ema50=105, ema200=110)
        one_hour = [candle(i, 99, 100, 98, 99) for i in range(50)]
        five_minute = [candle(i, 100, 101, 99, 100.5) for i in range(20)]
        high = 102 if recent_extreme is None else recent_extreme
        five_minute[-3:] = [candle(i, 101, high, 99, 100.5) for i in range(17, 20)]
        five_minute[-1] = candle(19, 101, high, 99, 100.5)
        candles_15m = [candle(i, 101, high, 99, 100.5) for i in range(30)]
    market_regime = "TREND_UP" if direction == "LONG" else "TREND_DOWN"
    return context(
        candles_5m=five_minute,
        candles_15m=candles_15m,
        candles_1h=one_hour,
        indicators=indicators,
        market_regime=market_regime,
    )


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_v1_internal_geometry_valid(direction):
    """V1 with explicit target_r produces valid geometry."""
    scanner = TrendPullbackScanner(
        allowed_regimes=("TREND_UP",) if direction == "LONG" else ("TREND_DOWN",),
        target_r=0.5,
    )
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(_v1_context(direction))
    assert candidate is not None
    assert validate_risk_geometry(candidate)[0] is True


@pytest.mark.parametrize(("direction", "recent_extreme"), [("LONG", 100), ("SHORT", 100)])
def test_v1_rejects_stop_inside_entry_zone(direction, recent_extreme):
    """V1 rejects internally when stop falls inside entry zone."""
    scanner = TrendPullbackScanner(
        allowed_regimes=("TREND_UP",) if direction == "LONG" else ("TREND_DOWN",),
        target_r=0.5,
    )
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(
        _v1_context(direction, recent_extreme=recent_extreme),
    )
    assert candidate is None


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_v1_rejects_target_inside_entry_zone(direction):
    """V1 rejects internally when target falls inside/below entry zone."""
    scanner = TrendPullbackScanner(
        allowed_regimes=("TREND_UP",) if direction == "LONG" else ("TREND_DOWN",),
        target_r=0,
    )
    candidate = getattr(scanner, f"_scan_{direction.lower()}")(_v1_context(direction))
    assert candidate is None


def test_trend_pullback_selects_ema20_anchor():
    assert TrendPullbackScanner()._pullback_anchor(100, 100, 105) == ("EMA20", 100)


def test_trend_pullback_selects_ema50_anchor():
    assert TrendPullbackScanner()._pullback_anchor(105, 100, 105) == ("EMA50", 105)


def test_trend_pullback_selects_nearest_anchor_when_near_both():
    assert TrendPullbackScanner(pullback_tolerance=0.1)._pullback_anchor(101, 100, 105) == ("EMA20", 100)


# ---------------------------------------------------------------------------
# Breakout retest scanner (unchanged)
# ---------------------------------------------------------------------------

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
