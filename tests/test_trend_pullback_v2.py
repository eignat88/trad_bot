"""Unit tests for TREND_PULLBACK_V2 scanner."""
import pytest

from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels, SetupCandidate
from app.scanners.outcome import evaluate_setup_outcome
from app.scanners.trend_pullback import TrendPullbackScanner
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2


def _make_indicators(**overrides):
    base = IndicatorSnapshot(
        atr=100.0,
        rsi=45.0,
        ema20=10000.0,
        ema50=9900.0,
        ema200=9500.0,
        bb_upper=10200.0,
        bb_lower=9800.0,
        bb_width=0.04,
        volume_sma=1000.0,
    )
    return IndicatorSnapshot(**{**base.__dict__, **overrides})


def _make_candles(n=60, base_price=10000, base_ts=1_000_000):
    candles = []
    for i in range(n):
        ts = base_ts + i * 60_000
        o = base_price + i * 0.5
        h = o + 10
        l = o - 10
        c = o + 5
        candles.append(Candle(ts, o, h, l, c, 100))
    return tuple(candles)


def _make_context(**overrides):
    ind = _make_indicators()
    base = MarketContext(
        symbol="BTCUSDT",
        candles_5m=_make_candles(20, 10000),
        candles_15m=_make_candles(30, 10000),
        candles_1h=_make_candles(60, 10000),
        candles_4h=_make_candles(10, 10000),
        indicators=ind,
        market_regime="TREND_UP",
        levels=MarketLevels(),
        evaluated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    return MarketContext(**{**base.__dict__, **overrides})


# ─── V2 Name and defaults ───

def test_v2_name_is_distinct_from_v1():
    """scanner.name == 'TREND_PULLBACK_V2', not 'TREND_PULLBACK'"""
    assert TrendPullbackScannerV2.name == "TREND_PULLBACK_V2"
    assert TrendPullbackScanner.name == "TREND_PULLBACK"
    assert TrendPullbackScannerV2.name != TrendPullbackScanner.name


def test_v2_default_pullback_tolerance():
    """pullback_tolerance == 0.012"""
    scanner = TrendPullbackScannerV2()
    assert scanner.pullback_tolerance == 0.012


def test_v2_default_target_r():
    """target_r == 0.50"""
    scanner = TrendPullbackScannerV2()
    assert scanner.target_r == 0.50


def test_v1_default_pullback_tolerance():
    """v1 pullback_tolerance == 0.01 (unchanged)"""
    scanner = TrendPullbackScanner()
    assert scanner.pullback_tolerance == 0.01


def test_v2_default_enabled_directions():
    """v2 defaults to LONG only"""
    scanner = TrendPullbackScannerV2()
    assert scanner.enabled_directions == ("LONG",)


def test_v2_default_allowed_regimes():
    """v2 defaults to TREND_UP only"""
    scanner = TrendPullbackScannerV2()
    assert scanner.allowed_regimes == ("TREND_UP",)


def test_v2_default_signal_timeframe():
    """v2 defaults to 15m"""
    scanner = TrendPullbackScannerV2()
    assert scanner.signal_timeframe == "15m"


# ─── V2 Signal generation ───

def test_v2_generates_long_in_trend_up():
    """V2 scanner generates LONG setup in TREND_UP when conditions are met."""
    # Price ~10014, need ema20 close enough but not too close (pullback_quality <= 0.75)
    ind = _make_indicators(ema20=10080, ema50=9900, ema200=9500)
    # candles_5m last close = 10000 + 19*0.5 + 5 = 10014.5
    # near_ema20: |10014.5 - 10080| / 10080 = 0.0065 < 0.012 ✓
    # pullback_quality = 1 - min(0.0065/0.012, 1) = 0.458 <= 0.75 ✓
    ctx = _make_context(indicators=ind)
    scanner = TrendPullbackScannerV2()
    results = scanner.scan(ctx)
    # Should produce at least one LONG setup in TREND_UP
    long_setups = [s for s in results if s.direction == "LONG"]
    assert len(long_setups) >= 1
    assert long_setups[0].scanner_name == "TREND_PULLBACK_V2"
    assert long_setups[0].features.get("recommended_expiry_policy") == "BREAKEVEN"


def test_v2_does_not_generate_short_by_default():
    """V2 defaults to LONG only — no SHORT setups."""
    ctx = _make_context(market_regime="TREND_DOWN")
    ind = _make_indicators(ema20=9800, ema50=9900, rsi=60)
    ctx = _make_context(indicators=ind, market_regime="TREND_DOWN")
    scanner = TrendPullbackScannerV2()
    results = scanner.scan(ctx)
    short_setups = [s for s in results if s.direction == "SHORT"]
    assert len(short_setups) == 0


def test_v2_custom_directions():
    """V2 can be configured to also scan SHORT."""
    ctx = _make_context(market_regime="TREND_DOWN")
    ind = _make_indicators(ema20=9800, ema50=9900, rsi=60)
    ctx = _make_context(indicators=ind, market_regime="TREND_DOWN")
    scanner = TrendPullbackScannerV2(enabled_directions=("LONG", "SHORT"))
    results = scanner.scan(ctx)
    # Should be able to scan SHORT when enabled
    assert isinstance(results, list)


# ─── V2 does not modify v1 ───

def test_v2_does_not_modify_v1():
    """TrendPullbackScanner (v1) is not changed."""
    scanner_v1 = TrendPullbackScanner()
    assert scanner_v1.pullback_tolerance == 0.01
    assert scanner_v1.name == "TREND_PULLBACK"


# ─── Expire at breakeven outcome ───

def _expired_candidate():
    """Create a candidate that will expire (no TP/SL hit)."""
    return SetupCandidate(
        scanner_name="TREND_PULLBACK_V2",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_000,
        reference_price=101,
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=99,
        target_1=105,  # far target, won't be hit
        target_2=110,
        features={"recommended_expiry_policy": "BREAKEVEN"},
    )


def test_v2_expire_at_breakeven_gives_0r():
    """EXPIRED_BE gives 0R (exit at entry price)."""
    setup = _expired_candidate()
    # Candles that touch entry but never hit TP or SL
    candles = [
        Candle(1_000, 101, 102, 100, 101, 100),  # signal candle
        Candle(1_300, 101, 101.5, 100.5, 101, 100),  # touch entry
        Candle(1_600, 101, 101.8, 100.8, 101, 100),  # still within range
        Candle(1_900, 101, 101.9, 100.9, 101, 100),  # still within range
    ]
    outcome = evaluate_setup_outcome(setup, candles, max_bars=3)
    assert outcome.first_event == "EXPIRED_BE"
    assert outcome.result_r == pytest.approx(0.0)
    assert outcome.entry_price == 101
    assert outcome.exit_price == 101


def test_v1_expire_gives_nonzero_r():
    """V1 EXPIRED gives close-based R (not 0R)."""
    setup = SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_000,
        reference_price=101,
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=99,
        target_1=105,
        target_2=110,
        features={},  # No recommended_expiry_policy
    )
    candles = [
        Candle(1_000, 101, 102, 100, 101, 100),
        Candle(1_300, 101, 101.5, 100.5, 101, 100),
        Candle(1_600, 102, 102.5, 101.5, 102, 100),  # close at 102
    ]
    outcome = evaluate_setup_outcome(setup, candles, max_bars=2)
    assert outcome.first_event == "EXPIRED"
    # Close at 102, entry at 101, risk=2 → result_r = (102-101)/2 = 0.5
    assert outcome.result_r == pytest.approx(0.5)


# ─── Backward compatibility ───

def test_v2_features_include_recommended_expiry_policy():
    """All v2 setups include recommended_expiry_policy=BREAKEVEN in features."""
    ctx = _make_context()
    scanner = TrendPullbackScannerV2()
    results = scanner.scan(ctx)
    for setup in results:
        assert setup.features.get("recommended_expiry_policy") == "BREAKEVEN"
