"""Regression tests for TREND_PULLBACK_V2 risk geometry fix.

Root cause: V2 anchored entry_zone to EMA spread (min/max of EMA20/EMA50 * 0.998/1.002)
instead of current_price, causing stop to frequently fall inside the entry zone.

V2 also used ATR-based target instead of risk-reward-based target, producing targets
that could land inside or below the entry zone high.

These tests verify:
  1. V2 produces candidates with valid risk geometry by default
  2. Stop inside entry zone is properly rejected
  3. Target below entry zone is properly rejected
  4. Detailed reason codes are returned by the validator
  5. Entry zone is anchored to current_price, not EMA
  6. Target is risk-reward based, not ATR-based
  7. Production-representative regression cases
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from app.models import Candle
from app.scanners.models import (
    IndicatorSnapshot, MarketContext, MarketLevels, SetupCandidate,
)
from app.scanners.risk_geometry import (
    INVALID_RISK_GEOMETRY,
    REASON_STOP_INSIDE_ENTRY_ZONE,
    REASON_STOP_ABOVE_ENTRY_ZONE,
    REASON_TARGET_INSIDE_ENTRY_ZONE,
    REASON_TARGET_BELOW_ENTRY_ZONE,
    validate_risk_geometry,
    has_valid_risk_geometry,
)
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candle(idx: int, o: float, h: float, l: float, c: float, vol: float = 10) -> Candle:
    return Candle(idx * 300_000, o, h, l, c, vol)


def _make_indicators(**kw) -> IndicatorSnapshot:
    base = IndicatorSnapshot(
        atr=kw.pop("atr", 2.0), rsi=kw.pop("rsi", 45.0),
        ema20=kw.pop("ema20", 100.0), ema50=kw.pop("ema50", 95.0),
        ema200=kw.pop("ema200", 90.0),
    )
    return IndicatorSnapshot(**{**base.__dict__, **kw})


def _make_context(
    *,
    current_price: float = 100.0,
    ema20: float | None = None,
    ema50: float = 95.0,
    ema200: float = 90.0,
    atr: float = 2.0,
    rsi: float = 45.0,
    recent_lows: list[float] | None = None,
    market_regime: str = "TREND_UP",
) -> MarketContext:
    """Build a MarketContext that TREND_PULLBACK_V2 will scan successfully.

    When ema20 is not provided, it is computed so that pullback_quality ≈ 0.4
    (well within the default max_pullback_quality of 0.75).

    pullback_quality = 1 - min(ema_distance / tolerance, 1)
    With ema_distance = tolerance * 0.6 → quality ≈ 0.4.
    """
    tolerance = 0.012  # V2 default
    if ema20 is None:
        # distance = |price - ema20| / ema20 ≈ tolerance * 0.6
        ema20 = current_price / (1 + tolerance * 0.6)

    ind = _make_indicators(ema20=ema20, ema50=ema50, ema200=ema200, atr=atr, rsi=rsi)

    # 1h candles: close > ema200 for LONG
    one_hour = [_candle(i, ema200 + 10, ema200 + 12, ema200 + 8, ema200 + 10) for i in range(50)]

    # 5m candles: last candle close = current_price, signal candle is bullish
    lows = recent_lows if recent_lows is not None else [current_price - 3.0] * 20
    five_minute = []
    for i in range(20):
        lo = lows[i] if i < len(lows) else lows[-1]
        o = current_price - 0.5
        h = current_price + 0.5
        c = current_price
        five_minute.append(_candle(i, o, h, lo, c))

    # 15m candles: enough for V2
    fifteen = [_candle(i, current_price - 1, current_price + 1, current_price - 2, current_price) for i in range(30)]

    return MarketContext(
        symbol="BTCUSDT",
        candles_5m=tuple(five_minute),
        candles_15m=tuple(fifteen),
        candles_1h=tuple(one_hour),
        candles_4h=(),
        indicators=ind,
        market_regime=market_regime,
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. Valid LONG candidate from V2 passes geometry
# ---------------------------------------------------------------------------

class TestV2ValidGeometry:
    """V2 scanner produces candidates with valid risk geometry."""

    def test_long_candidate_passes_validator(self):
        ctx = _make_context(current_price=100, ema50=95, recent_lows=[96, 97, 98])
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        assert len(results) >= 1
        for c in results:
            ok, reason = validate_risk_geometry(c)
            assert ok, f"Expected valid geometry, got {reason}"

    def test_long_entry_zone_anchored_to_price(self):
        """Entry zone must be ±0.2% around current_price, not around EMA."""
        ctx = _make_context(current_price=100, ema50=95, recent_lows=[96, 97, 98])
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        assert len(results) >= 1
        c = results[0]
        # Entry zone should be 99.8–100.2 (±0.2% around 100)
        assert c.entry_zone_low == pytest.approx(99.8, rel=1e-6)
        assert c.entry_zone_high == pytest.approx(100.2, rel=1e-6)

    def test_long_target_is_risk_reward_based(self):
        """target_1 = current_price + risk * target_r, not ATR-based."""
        ctx = _make_context(current_price=100, ema50=95, recent_lows=[96, 97, 98])
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        assert len(results) >= 1
        c = results[0]
        risk = c.reference_price - c.invalidation_price
        expected_target = c.reference_price + risk * scanner.target_r
        assert c.target_1 == pytest.approx(expected_target, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. Stop inside entry zone → rejected
# ---------------------------------------------------------------------------

class TestStopInsideEntryZone:
    """Stop inside entry zone must be rejected with detailed reason code."""

    def test_validator_rejects_stop_inside_zone_long(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=100.5,  # inside [100, 101]
            target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == REASON_STOP_INSIDE_ENTRY_ZONE

    def test_validator_rejects_stop_inside_zone_short(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="SHORT",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=100.5,  # inside [100, 101]
            target_1=98,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == REASON_STOP_INSIDE_ENTRY_ZONE


# ---------------------------------------------------------------------------
# 3. Stop above entry zone (LONG) → rejected
# ---------------------------------------------------------------------------

class TestStopAboveEntryZone:

    def test_validator_rejects_stop_above_zone_long(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=102,  # above entire zone
            target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == REASON_STOP_ABOVE_ENTRY_ZONE


# ---------------------------------------------------------------------------
# 4. Target inside entry zone → rejected
# ---------------------------------------------------------------------------

class TestTargetInsideEntryZone:

    def test_validator_rejects_target_inside_zone_long(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=99,
            target_1=100.5,  # inside [100, 101]
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == REASON_TARGET_INSIDE_ENTRY_ZONE

    def test_validator_rejects_target_below_zone_long(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=99,
            target_1=99.5,  # below entire zone
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == REASON_TARGET_BELOW_ENTRY_ZONE


# ---------------------------------------------------------------------------
# 5. Valid setups pass
# ---------------------------------------------------------------------------

class TestValidSetups:

    def test_valid_long(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=99,
            target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is True
        assert reason is None

    def test_valid_short(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="SHORT",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=102,
            target_1=98,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is True
        assert reason is None

    def test_has_valid_risk_geometry_true(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=99, target_1=103,
        )
        assert has_valid_risk_geometry(candidate) is True


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_entry_zone_inverted(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=101, entry_zone_high=100,  # inverted
            invalidation_price=99, target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == "ENTRY_ZONE_INVERTED"

    def test_entry_zone_missing(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=0, entry_zone_high=0,
            invalidation_price=99, target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == "ENTRY_ZONE_MISSING"

    def test_stop_missing(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=0, target_1=103,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == "STOP_MISSING"

    def test_target_missing(self):
        candidate = SetupCandidate(
            scanner_name="TREND_PULLBACK_V2", symbol="BTCUSDT", direction="LONG",
            entry_zone_low=100, entry_zone_high=101,
            invalidation_price=99, target_1=None,
        )
        ok, reason = validate_risk_geometry(candidate)
        assert ok is False
        assert reason == "TARGET_1_MISSING"


# ---------------------------------------------------------------------------
# 7. V2 scanner internal validation rejects bad geometry before emit
# ---------------------------------------------------------------------------

class TestV2InternalValidation:
    """V2 now validates geometry internally, like V1 and V3."""

    def test_v2_does_not_emit_stop_inside_entry_zone(self):
        """When candle lows produce stop inside entry zone, V2 returns None."""
        # Make recent candle lows very close to current_price so stop
        # falls inside the ±0.2% entry zone.
        # current_price=100, entry_zone=[99.8, 100.2]
        # Lows at 100.1 → stop = 100.1 * 0.998 = 99.90 → inside zone → invalid
        ctx = _make_context(current_price=100, ema50=95, recent_lows=[100.1, 100.1, 100.1])
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        # V2 should not produce a candidate with stop inside zone
        for c in results:
            ok, _ = validate_risk_geometry(c)
            assert ok, "V2 should reject candidates with bad geometry internally"


# ---------------------------------------------------------------------------
# 8. Production-representative regression cases
# ---------------------------------------------------------------------------

class TestProductionRegressions:
    """Regressions inspired by production reject patterns.

    DASHUSDT, UNIUSDT, NEARUSDT were among the top rejected symbols.
    The pattern: entry_zone from EMA spread was wide enough that stop
    (from candle lows) frequently fell inside.
    """

    def test_regression_dash_like_wide_ema_spread(self):
        """DASHUSDT-like: EMA20=68, EMA50=65, current_price=67.14.

        Old entry_zone = [64.87, 68.14], stop=67.56 → inside zone.
        New entry_zone = [67.0, 67.28], stop must be below 67.0.
        """
        ctx = _make_context(
            current_price=67.14,
            ema20=68, ema50=65, ema200=60,
            recent_lows=[64.0, 64.5, 65.0],
        )
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        for c in results:
            ok, reason = validate_risk_geometry(c)
            assert ok, f"DASH-like regression: {reason}"
            # Entry zone should be anchored to current_price
            assert c.entry_zone_low == pytest.approx(67.14 * 0.998, rel=1e-6)
            assert c.entry_zone_high == pytest.approx(67.14 * 1.002, rel=1e-6)

    def test_regression_uni_like_close_ema(self):
        """UNIUSDT-like: EMA20 and EMA50 very close, current_price near both."""
        ctx = _make_context(
            current_price=12.50,
            ema20=12.55, ema50=12.45, ema200=11.00,
            recent_lows=[11.8, 12.0, 12.2],
        )
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        for c in results:
            ok, reason = validate_risk_geometry(c)
            assert ok, f"UNI-like regression: {reason}"

    def test_regression_near_like_extreme_spread(self):
        """NEARUSDT-like: very wide EMA spread."""
        ctx = _make_context(
            current_price=5.20,
            ema20=5.5, ema50=4.8, ema200=4.0,
            recent_lows=[4.5, 4.6, 4.7],
        )
        scanner = TrendPullbackScannerV2()
        results = scanner.scan(ctx)
        for c in results:
            ok, reason = validate_risk_geometry(c)
            assert ok, f"NEAR-like regression: {reason}"


# ---------------------------------------------------------------------------
# 9. Backward compatibility: legacy INVALID_RISK_GEOMETRY string preserved
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:

    def test_invalid_risk_geometry_constant_still_exists(self):
        assert INVALID_RISK_GEOMETRY == "INVALID_RISK_GEOMETRY"

    def test_all_reason_codes_are_strings(self):
        from app.scanners import risk_geometry as rg
        codes = [
            rg.REASON_STOP_INSIDE_ENTRY_ZONE,
            rg.REASON_STOP_ABOVE_ENTRY_ZONE,
            rg.REASON_STOP_BELOW_ENTRY_ZONE,
            rg.REASON_TARGET_INSIDE_ENTRY_ZONE,
            rg.REASON_TARGET_ABOVE_ENTRY_ZONE,
            rg.REASON_TARGET_BELOW_ENTRY_ZONE,
        ]
        for code in codes:
            assert isinstance(code, str)
            assert len(code) > 0
