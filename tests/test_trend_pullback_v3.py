"""Unit tests for TREND_PULLBACK_V3 scanner."""
from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2
from app.scanners.trend_pullback_v3 import TrendPullbackScannerV3


def _make_indicators(**overrides):
    base = IndicatorSnapshot(
        atr=100.0,
        rsi=65.0,           # > 60 threshold
        ema20=10000.0,
        ema50=9900.0,
        ema200=9500.0,
        bb_upper=10200.0,
        bb_lower=9800.0,
        bb_width=0.04,
        volume_sma=1000.0,
        adx=40.0,           # > 35 threshold
        ema50_slope=0.3,    # > 0 threshold
    )
    return IndicatorSnapshot(**{**base.__dict__, **overrides})


def _make_candles(n=60, base_price=10000, base_ts=1_000_000, direction="up"):
    """Create a sequence of candles with a consistent trend.

    The last candle close will be near base_price (within pullback_tolerance of EMA).
    """
    candles = []
    for i in range(n):
        ts = base_ts + i * 60_000
        if direction == "up":
            o = base_price + i * 0.5
        else:
            o = base_price - i * 0.5
        h = o + 10
        l = o - 10
        c = o + 5 if direction == "up" else o - 5
        candles.append(Candle(ts, o, h, l, c, 100))
    return tuple(candles)


def _make_context(
    symbol="BTCUSDT",
    regime="TREND_UP",
    pullback_close=None,
    hour=12,  # Default to hour 12 (allowed)
    **ind_overrides,
):
    ind = _make_indicators(**ind_overrides)
    one_hour = _make_candles(60, 10000, direction="up")

    # Create 5m and 15m candles
    # We need the last candle close to be within pullback_tolerance (1.2%) of EMA20
    # but with pullback_quality <= 0.75 (i.e., distance >= 0.25 * tolerance = 0.3%)
    # So we need price at ~0.5% from EMA20 (10000), i.e., ~10050 or ~9950

    # Default: create candles ending at a price ~0.5% above EMA20
    # This gives pullback_quality ≈ 0.58, which is within [0, 0.75]
    default_close = 10050.0  # 0.5% above EMA20=10000

    signal = _make_candles(30, 10000, direction="up")
    five_minute = _make_candles(20, 10000, direction="up")

    close = default_close if pullback_close is None else pullback_close

    # Override last candle close
    last_signal = signal[-1]
    last_five = five_minute[-1]
    signal = tuple(list(signal[:-1]) + [
        Candle(last_signal.timestamp, last_signal.open, last_signal.high, last_signal.low, close, 100)
    ])
    five_minute = tuple(list(five_minute[:-1]) + [
        Candle(last_five.timestamp, last_five.open, last_five.high, last_five.low, close, 100)
    ])

    evaluated_at = datetime(2025, 1, 15, hour, 0, 0, tzinfo=timezone.utc)

    return MarketContext(
        symbol=symbol,
        candles_5m=five_minute,
        candles_15m=signal,
        candles_1h=one_hour,
        candles_4h=(),
        indicators=ind,
        market_regime=regime,
        levels=MarketLevels(),
        evaluated_at=evaluated_at,
    )


# ─── V3 Name and defaults ───

def test_v3_name_is_distinct():
    """scanner.name == 'TREND_PULLBACK_V3'"""
    assert TrendPullbackScannerV3.name == "TREND_PULLBACK_V3"
    assert TrendPullbackScannerV3.name != TrendPullbackScannerV2.name


def test_v3_default_rsi_threshold():
    """rsi_threshold == 60.0"""
    scanner = TrendPullbackScannerV3()
    assert scanner.rsi_threshold == 60.0


def test_v3_default_adx_threshold():
    """adx_threshold == 35.0"""
    scanner = TrendPullbackScannerV3()
    assert scanner.adx_threshold == 35.0


def test_v3_default_ema50_slope_min():
    """ema50_slope_min == 0.0"""
    scanner = TrendPullbackScannerV3()
    assert scanner.ema50_slope_min == 0.0


def test_v3_default_pullback_tolerance():
    """pullback_tolerance == 0.012 (same as v2)"""
    scanner = TrendPullbackScannerV3()
    assert scanner.pullback_tolerance == 0.012


def test_v3_default_target_r():
    """target_r == 0.50 (same as v2)"""
    scanner = TrendPullbackScannerV3()
    assert scanner.target_r == 0.50


def test_v3_default_enabled_directions():
    """v3 defaults to LONG only"""
    scanner = TrendPullbackScannerV3()
    assert scanner.enabled_directions == ("LONG",)


def test_v3_default_allowed_regimes():
    """v3 defaults to TREND_UP only"""
    scanner = TrendPullbackScannerV3()
    assert scanner.allowed_regimes == ("TREND_UP",)


def test_v3_default_excluded_symbols():
    """v3 defaults to ONDOUSDT, BNBUSDT, SOLUSDT"""
    scanner = TrendPullbackScannerV3()
    assert "ONDOUSDT" in scanner.excluded_symbols
    assert "BNBUSDT" in scanner.excluded_symbols
    assert "SOLUSDT" in scanner.excluded_symbols


def test_v3_default_excluded_hours():
    """v3 defaults to hours 10, 15, 19 excluded"""
    scanner = TrendPullbackScannerV3()
    assert 10 in scanner.excluded_hours
    assert 15 in scanner.excluded_hours
    assert 19 in scanner.excluded_hours


# ─── V3 Signal generation ───

def test_v3_generates_long_in_trend_up():
    """V3 scanner generates LONG setup when all conditions are met."""
    ctx = _make_context()
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    long_setups = [s for s in results if s.direction == "LONG"]
    assert len(long_setups) >= 1
    assert long_setups[0].scanner_name == "TREND_PULLBACK_V3"
    assert long_setups[0].features.get("recommended_expiry_policy") == "BREAKEVEN"


def test_v3_does_not_generate_short_by_default():
    """V3 defaults to LONG only — no SHORT setups."""
    ctx = _make_context(regime="TREND_DOWN")
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    short_setups = [s for s in results if s.direction == "SHORT"]
    assert len(short_setups) == 0


# ─── RSI filter ───

def test_v3_rejects_low_rsi():
    """V3 rejects setup when RSI < 60."""
    ctx = _make_context(rsi=55.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_accepts_rsi_above_threshold():
    """V3 accepts setup when RSI >= 60."""
    ctx = _make_context(rsi=65.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


def test_v3_accepts_rsi_at_threshold():
    """V3 accepts setup when RSI == 60."""
    ctx = _make_context(rsi=60.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


# ─── ADX filter ───

def test_v3_rejects_low_adx():
    """V3 rejects setup when ADX < 35."""
    ctx = _make_context(adx=30.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_accepts_adx_above_threshold():
    """V3 accepts setup when ADX >= 35."""
    ctx = _make_context(adx=40.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


def test_v3_accepts_adx_at_threshold():
    """V3 accepts setup when ADX == 35."""
    ctx = _make_context(adx=35.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


# ─── EMA50 slope filter ───

def test_v3_rejects_negative_ema50_slope():
    """V3 rejects setup when EMA50 slope <= 0."""
    ctx = _make_context(ema50_slope=-0.1)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_zero_ema50_slope():
    """V3 rejects setup when EMA50 slope == 0."""
    ctx = _make_context(ema50_slope=0.0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_accepts_positive_ema50_slope():
    """V3 accepts setup when EMA50 slope > 0."""
    ctx = _make_context(ema50_slope=0.1)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


# ─── Hour filter ───

def test_v3_rejects_hour_before_start():
    """V3 rejects setup when hour < 6."""
    ctx = _make_context(hour=5)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_hour_after_end():
    """V3 rejects setup when hour > 23."""
    ctx = _make_context(hour=0)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_excluded_hour_10():
    """V3 rejects setup when hour == 10."""
    ctx = _make_context(hour=10)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_excluded_hour_15():
    """V3 rejects setup when hour == 15."""
    ctx = _make_context(hour=15)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_excluded_hour_19():
    """V3 rejects setup when hour == 19."""
    ctx = _make_context(hour=19)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_accepts_hour_in_range():
    """V3 accepts setup when hour is in allowed range."""
    ctx = _make_context(hour=12)
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


# ─── Symbol exclusion ───

def test_v3_rejects_ondo():
    """V3 rejects ONDOUSDT."""
    ctx = _make_context(symbol="ONDOUSDT")
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_bnb():
    """V3 rejects BNBUSDT."""
    ctx = _make_context(symbol="BNBUSDT")
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_rejects_sol():
    """V3 rejects SOLUSDT."""
    ctx = _make_context(symbol="SOLUSDT")
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) == 0


def test_v3_accepts_btc():
    """V3 accepts BTCUSDT."""
    ctx = _make_context(symbol="BTCUSDT")
    scanner = TrendPullbackScannerV3()
    results = scanner.scan(ctx)
    assert len(results) >= 1


# ─── Pullback quality ───

def test_v3_pullback_quality_above_max_is_filtered():
    """V3 filters setups with pullback_quality > max_pullback_quality."""
    # Use a price very close to EMA20 (10000) to get high pullback_quality
    # distance = |10005 - 10000| / 10000 = 0.0005
    # pullback_quality = 1 - min(0.0005 / 0.012, 1) = 1 - 0.042 = 0.958 > 0.75
    ctx = _make_context(pullback_close=10005.0)
    scanner = TrendPullbackScannerV3(max_pullback_quality=0.75)
    results = scanner.scan(ctx)
    assert len(results) == 0


# ─── Custom parameters ───

def test_v3_custom_thresholds():
    """V3 can be configured with custom thresholds."""
    scanner = TrendPullbackScannerV3(
        rsi_threshold=65.0,
        adx_threshold=40.0,
        ema50_slope_min=0.2,
    )
    assert scanner.rsi_threshold == 65.0
    assert scanner.adx_threshold == 40.0
    assert scanner.ema50_slope_min == 0.2


def test_v3_custom_excluded_symbols():
    """V3 can be configured with custom exclusions."""
    scanner = TrendPullbackScannerV3(
        excluded_symbols=frozenset({"BTCUSDT"}),
    )
    assert "BTCUSDT" in scanner.excluded_symbols
    assert "ONDOUSDT" not in scanner.excluded_symbols


def test_v3_custom_excluded_hours():
    """V3 can be configured with custom excluded hours."""
    scanner = TrendPullbackScannerV3(
        excluded_hours=frozenset({12}),
    )
    assert 12 in scanner.excluded_hours
    assert 10 not in scanner.excluded_hours


# ─── Orchestrator integration ───

def test_orchestrator_has_v3():
    """Default ScannerOrchestrator must contain TREND_PULLBACK_V3."""
    from app.scanners.orchestrator import ScannerOrchestrator
    orchestrator = ScannerOrchestrator()
    assert "TREND_PULLBACK_V3" in orchestrator.scanners
    assert len(orchestrator.scanners) == 9  # 8 existing + V3


def test_orchestrator_can_enable_only_v3():
    """ScannerOrchestrator can be configured to run only V3."""
    from app.scanners.orchestrator import ScannerOrchestrator
    orchestrator = ScannerOrchestrator(enabled_scanners=["TREND_PULLBACK_V3"])
    assert list(orchestrator.scanners.keys()) == ["TREND_PULLBACK_V3"]


def test_v3_does_not_modify_v2():
    """TrendPullbackScannerV2 is not changed."""
    scanner_v2 = TrendPullbackScannerV2()
    assert scanner_v2.name == "TREND_PULLBACK_V2"
    assert scanner_v2.rsi_cool_threshold == 55
    assert "ADX" not in dir(scanner_v2)
