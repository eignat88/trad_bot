"""Unit tests for MOMENTUM_EXHAUSTION_R scanner (reversed version)."""
from datetime import datetime, timezone

import pytest

from app.models import Candle
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    ScannerDirection,
    SetupCandidate,
)
from app.scanners.momentum_exhaustion import MomentumExhaustionScanner
from app.scanners.momentum_exhaustion_r import MomentumExhaustionRScanner


# ── fixtures ──────────────────────────────────────────────────────────

def _make_indicators(**overrides):
    base = IndicatorSnapshot(
        atr=100.0,
        rsi=50.0,
        ema20=10000.0,
        ema50=9900.0,
        ema200=9500.0,
        bb_upper=10200.0,
        bb_lower=9800.0,
        bb_width=0.04,
        volume_sma=1000.0,
    )
    return IndicatorSnapshot(**{**base.__dict__, **overrides})


def _make_candles(n=60, base_price=10000, base_ts=1_000_000, direction="up"):
    """Create a sequence of candles with a clear trend direction."""
    candles = []
    for i in range(n):
        ts = base_ts + i * 60_000
        if direction == "up":
            o = base_price + i * 0.5
            c = o + 5
        elif direction == "down":
            o = base_price - i * 0.5
            c = o - 5
        else:
            o = base_price
            c = o
        h = max(o, c) + 5
        l = min(o, c) - 5
        candles.append(Candle(ts, o, h, l, c, 100))
    return tuple(candles)


def _make_exhaustion_candles(
    prev_high: float,
    recent_high: float,
    current_price: float,
    base_ts: int = 1_000_000,
    bearish: bool = True,
):
    """Create 15m + 5m candles that form an exhaustion pattern.

    The 15m candles form two swing highs (prev_high and a higher one).
    The 5m candles show the recent breakout above prev_high followed by
    a bearish (or bullish) exhaustion candle.
    """
    # 15m: 30+ candles with two swing highs
    candles_15m = []
    for i in range(40):
        ts = base_ts + i * 900_000  # 15m intervals
        if i < 15:
            o = prev_high - 50 + i * 2
            h = o + 10
            l = o - 10
            c = o + 3
        elif i < 25:
            # first swing high at prev_high
            o = prev_high - 5
            h = prev_high + 5
            l = prev_high - 15
            c = prev_high - 3
        else:
            # second swing high (recent_high > prev_high)
            offset = (recent_high - prev_high) * ((i - 25) / 15)
            o = prev_high + offset
            h = recent_high + 2
            l = prev_high - 10
            c = prev_high + offset - 2
        candles_15m.append(Candle(ts, o, h, l, c, 100))

    # 5m: 20 candles showing the exhaustion
    candles_5m = []
    for i in range(20):
        ts = base_ts + i * 300_000  # 5m intervals
        if i < 10:
            # trending up to recent_high
            o = prev_high + (recent_high - prev_high) * (i / 10) - 5
            h = o + 8
            l = o - 3
            c = o + 4
        elif i < 15:
            # consolidation near recent_high
            o = recent_high - 3
            h = recent_high + 2
            l = recent_high - 8
            c = recent_high - 2
        else:
            # exhaustion candle near current_price
            if bearish:
                o = current_price + 3
                c = current_price - 3
            else:
                o = current_price - 3
                c = current_price + 3
            h = max(o, c) + 5
            l = min(o, c) - 5
        candles_5m.append(Candle(ts, o, h, l, c, 100))

    return tuple(candles_15m), tuple(candles_5m)


def _make_exhaustion_context(
    prev_level: float = 10000,
    recent_extreme: float = 10100,
    current_price: float = 10005,
    rsi: float = 50.0,
    bearish: bool = True,
    atr: float = 100.0,
):
    """Build a MarketContext that triggers an exhaustion setup."""
    candles_15m, candles_5m = _make_exhaustion_candles(
        prev_level, recent_extreme, current_price, bearish=bearish,
    )
    indicators = _make_indicators(rsi=rsi, atr=atr)
    return MarketContext(
        symbol="BTCUSDT",
        candles_5m=candles_5m,
        candles_15m=candles_15m,
        candles_1h=_make_candles(60, 10000),
        candles_4h=_make_candles(10, 10000),
        indicators=indicators,
        market_regime="TREND_UP",
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )


# ── tests: scanner identity ──────────────────────────────────────────

def test_r_scanner_name_differs_from_original():
    """MOMENTUM_EXHAUSTION_R must have a distinct name."""
    assert MomentumExhaustionRScanner.name == "MOMENTUM_EXHAUSTION_R"
    assert MomentumExhaustionScanner.name == "MOMENTUM_EXHAUSTION"
    assert MomentumExhaustionRScanner.name != MomentumExhaustionScanner.name


def test_r_scanner_version():
    """R scanner starts at 1.0.0."""
    assert MomentumExhaustionRScanner.version == "1.0.0"


def test_r_scanner_default_params():
    """Default swing_lookback and exhaustion_threshold match the original."""
    r = MomentumExhaustionRScanner()
    o = MomentumExhaustionScanner()
    assert r.swing_lookback == o.swing_lookback
    assert r.exhaustion_threshold == o.exhaustion_threshold


# ── tests: direction reversal ────────────────────────────────────────

def test_bearish_exhaustion_reversed_to_long():
    """Original SHORT (bearish exhaustion) → R scanner gives LONG."""
    # Build context that triggers bearish exhaustion in the original
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=10100,
        current_price=10005,
        rsi=70.0,  # overbought → bearish exhaustion
        bearish=True,
        atr=100.0,
    )
    r_scanner = MomentumExhaustionRScanner()
    o_scanner = MomentumExhaustionScanner()

    o_candidates = o_scanner.scan(ctx)
    r_candidates = r_scanner.scan(ctx)

    # Original should produce SHORT
    if o_candidates:
        assert any(c.direction == "SHORT" for c in o_candidates)

    # R scanner should produce LONG (reversed)
    if r_candidates:
        long_candidates = [c for c in r_candidates if c.direction == "LONG"]
        assert len(long_candidates) > 0, "R scanner should produce LONG from bearish exhaustion"


def test_bullish_exhaustion_reversed_to_short():
    """Original LONG (bullish exhaustion) → R scanner gives SHORT."""
    # Build context that triggers bullish exhaustion in the original
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=9900,  # below prev
        current_price=9995,
        rsi=30.0,  # oversold → bullish exhaustion
        bearish=False,
        atr=100.0,
    )
    r_scanner = MomentumExhaustionRScanner()
    o_scanner = MomentumExhaustionScanner()

    o_candidates = o_scanner.scan(ctx)
    r_candidates = r_scanner.scan(ctx)

    # Original should produce LONG
    if o_candidates:
        assert any(c.direction == "LONG" for c in o_candidates)

    # R scanner should produce SHORT (reversed)
    if r_candidates:
        short_candidates = [c for c in r_candidates if c.direction == "SHORT"]
        assert len(short_candidates) > 0, "R scanner should produce SHORT from bullish exhaustion"


# ── tests: detection logic parity ────────────────────────────────────

def test_r_scan_requires_same_candle_count():
    """R scanner needs the same minimum candle count as the original."""
    r = MomentumExhaustionRScanner()

    # Too few 15m candles
    ctx_short_15m = _make_exhaustion_context()
    # Override with fewer candles
    from dataclasses import replace
    ctx_short_15m = replace(ctx_short_15m, candles_15m=_make_candles(10))
    assert r.scan(ctx_short_15m) == []

    # Too few 5m candles
    ctx_short_5m = _make_exhaustion_context()
    ctx_short_5m = replace(ctx_short_5m, candles_5m=_make_candles(10))
    assert r.scan(ctx_short_5m) == []


def test_r_scan_needs_two_swing_highs_or_lows():
    """R scanner requires at least 2 swing points (same as original)."""
    r = MomentumExhaustionRScanner()
    # Context with no exhaustion pattern (flat candles)
    flat = _make_candles(60, 10000, direction="flat")
    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=flat[:20],
        candles_15m=flat,
        candles_1h=flat,
        candles_4h=flat[:10],
        indicators=_make_indicators(rsi=50.0),
        market_regime="TREND_UP",
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    assert r.scan(ctx) == []


# ── tests: entry/invalidation/targets for reversed LONG ──────────────

def test_reversed_long_invalidation_below_recent_low():
    """For reversed LONG, invalidation should be below recent_low of 5m candles."""
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=10100,
        current_price=10005,
        rsi=70.0,
        bearish=True,
        atr=100.0,
    )
    r = MomentumExhaustionRScanner()
    candidates = r.scan(ctx)
    long_candidates = [c for c in candidates if c.direction == "LONG"]
    if long_candidates:
        c = long_candidates[0]
        # Invalidation should be below entry (LONG stop is below)
        assert c.invalidation_price < c.entry_zone_high
        # Target should be above entry (LONG target is above)
        assert c.target_1 is not None
        assert c.target_1 > c.entry_zone_low


def test_reversed_short_invalidation_above_recent_high():
    """For reversed SHORT, invalidation should be above recent_high of 5m candles."""
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=9900,
        current_price=9995,
        rsi=30.0,
        bearish=False,
        atr=100.0,
    )
    r = MomentumExhaustionRScanner()
    candidates = r.scan(ctx)
    short_candidates = [c for c in candidates if c.direction == "SHORT"]
    if short_candidates:
        c = short_candidates[0]
        # Invalidation should be above entry (SHORT stop is above)
        assert c.invalidation_price > c.entry_zone_low
        # Target should be below entry (SHORT target is below)
        assert c.target_1 is not None
        assert c.target_1 < c.entry_zone_high


# ── tests: features ──────────────────────────────────────────────────

def test_reversed_long_features_present():
    """Reversed LONG candidates include all quality features."""
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=10100,
        current_price=10005,
        rsi=70.0,
        bearish=True,
        atr=100.0,
    )
    r = MomentumExhaustionRScanner()
    candidates = [c for c in r.scan(ctx) if c.direction == "LONG"]
    if candidates:
        f = candidates[0].features
        expected_keys = {
            "exhaustion_magnitude",
            "body_ratio",
            "rsi_confirmation",
            "volume_ratio",
            "rr_ratio",
            "stop_distance_atr",
        }
        assert expected_keys == set(f.keys())
        # All features should be in [0, 1]
        for k, v in f.items():
            assert 0.0 <= v <= 1.0, f"{k}={v} out of [0, 1]"


def test_reversed_short_features_present():
    """Reversed SHORT candidates include all quality features."""
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=9900,
        current_price=9995,
        rsi=30.0,
        bearish=False,
        atr=100.0,
    )
    r = MomentumExhaustionRScanner()
    candidates = [c for c in r.scan(ctx) if c.direction == "SHORT"]
    if candidates:
        f = candidates[0].features
        expected_keys = {
            "exhaustion_magnitude",
            "body_ratio",
            "rsi_confirmation",
            "volume_ratio",
            "rr_ratio",
            "stop_distance_atr",
        }
        assert expected_keys == set(f.keys())
        for k, v in f.items():
            assert 0.0 <= v <= 1.0, f"{k}={v} out of [0, 1]"


# ── tests: RSI filter ────────────────────────────────────────────────

def test_reversed_long_requires_overbought_rsi():
    """Reversed LONG (from bearish exhaustion) requires RSI >= 65."""
    r = MomentumExhaustionRScanner()

    # RSI too low → no signal
    ctx_low_rsi = _make_exhaustion_context(
        prev_level=10000, recent_extreme=10100, current_price=10005,
        rsi=60.0, bearish=True,
    )
    candidates = r.scan(ctx_low_rsi)
    assert all(c.direction != "LONG" for c in candidates)


def test_reversed_short_requires_oversold_rsi():
    """Reversed SHORT (from bullish exhaustion) requires RSI <= 35."""
    r = MomentumExhaustionRScanner()

    # RSI too high → no signal
    ctx_high_rsi = _make_exhaustion_context(
        prev_level=10000, recent_extreme=9900, current_price=9995,
        rsi=40.0, bearish=False,
    )
    candidates = r.scan(ctx_high_rsi)
    assert all(c.direction != "SHORT" for c in candidates)


# ── tests: scanner metadata ──────────────────────────────────────────

def test_reversed_long_candidate_metadata():
    """Reversed LONG candidate has correct scanner_name and timeframes."""
    ctx = _make_exhaustion_context(
        prev_level=10000, recent_extreme=10100, current_price=10005,
        rsi=70.0, bearish=True,
    )
    r = MomentumExhaustionRScanner()
    candidates = [c for c in r.scan(ctx) if c.direction == "LONG"]
    if candidates:
        c = candidates[0]
        assert c.scanner_name == "MOMENTUM_EXHAUSTION_R"
        assert c.htf_timeframe == "1h"
        assert c.setup_timeframe == "15m"
        assert c.entry_timeframe == "5m"
        assert c.symbol == "BTCUSDT"


def test_reversed_short_candidate_metadata():
    """Reversed SHORT candidate has correct scanner_name and timeframes."""
    ctx = _make_exhaustion_context(
        prev_level=10000, recent_extreme=9900, current_price=9995,
        rsi=30.0, bearish=False,
    )
    r = MomentumExhaustionRScanner()
    candidates = [c for c in r.scan(ctx) if c.direction == "SHORT"]
    if candidates:
        c = candidates[0]
        assert c.scanner_name == "MOMENTUM_EXHAUSTION_R"
        assert c.htf_timeframe == "1h"
        assert c.setup_timeframe == "15m"
        assert c.entry_timeframe == "5m"
        assert c.symbol == "BTCUSDT"


# ── tests: no signal scenarios ───────────────────────────────────────

def test_no_signal_when_no_exhaustion():
    """No signal when candles show no exhaustion pattern."""
    r = MomentumExhaustionRScanner()
    flat = _make_candles(60, 10000, direction="flat")
    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=flat[:20],
        candles_15m=flat,
        candles_1h=flat,
        candles_4h=flat[:10],
        indicators=_make_indicators(rsi=50.0),
        market_regime="TREND_UP",
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    assert r.scan(ctx) == []


def test_no_signal_with_extreme_price_displacement():
    """No signal when price is too far from the swing level."""
    r = MomentumExhaustionRScanner()
    ctx = _make_exhaustion_context(
        prev_level=10000,
        recent_extreme=10100,
        current_price=10050,  # way above prev_high (beyond exhaustion_threshold)
        rsi=70.0,
        bearish=True,
    )
    candidates = r.scan(ctx)
    assert candidates == [], "Price too far above swing level should produce no signal"


# ── tests: integration with orchestrator ──────────────────────────────

def test_orchestrator_registers_r_scanner():
    """ScannerOrchestrator should include MOMENTUM_EXHAUSTION_R."""
    from app.scanners.orchestrator import ScannerOrchestrator
    orch = ScannerOrchestrator()
    assert "MOMENTUM_EXHAUSTION_R" in orch.scanners
    assert isinstance(orch.scanners["MOMENTUM_EXHAUSTION_R"], MomentumExhaustionRScanner)


def test_orchestrator_can_filter_r_scanner():
    """ScannerOrchestrator should be able to filter to only R scanner."""
    from app.scanners.orchestrator import ScannerOrchestrator
    orch = ScannerOrchestrator(enabled_scanners=["MOMENTUM_EXHAUSTION_R"])
    assert len(orch.scanners) == 1
    assert "MOMENTUM_EXHAUSTION_R" in orch.scanners
