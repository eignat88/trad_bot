"""Scanner 06: Support/Resistance Reaction.

Quality features emitted (all normalised to [0, 1]):
    level_touch_count     – number of times level was touched, normalised to 5
    rejection_strength    – wick / body ratio (graduated)
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)
    volume_spike          – volume > 1.3× average
    regime_alignment      – regime direction matches trade direction
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_displacement, find_swing_highs, find_swing_lows


class SupportResistanceScanner:
    name = "SUPPORT_RESISTANCE_REACTION"
    version = "2.0.0"

    def __init__(self, level_touches_min: int = 2, level_distance_pct: float = 0.005, swing_lookback: int = 5) -> None:
        self.level_touches_min = level_touches_min
        self.level_distance_pct = level_distance_pct
        self.swing_lookback = swing_lookback

    def _find_support_levels(self, candles: list) -> list[float]:
        lows = find_swing_lows(candles, self.swing_lookback)
        return sorted({sw.price for sw in lows if sum(1 for c in candles if abs(c.low - sw.price) / sw.price < 0.003) >= self.level_touches_min})

    def _find_resistance_levels(self, candles: list) -> list[float]:
        highs = find_swing_highs(candles, self.swing_lookback)
        return sorted({sw.price for sw in highs if sum(1 for c in candles if abs(c.high - sw.price) / sw.price < 0.003) >= self.level_touches_min})

    @staticmethod
    def _level_touch_count(candles: list, level: float, tolerance: float = 0.003) -> float:
        """Count touches at a level, normalised to [0, 1] (5+ touches → 1.0)."""
        touches = sum(1 for c in candles if abs(c.low - level) / level < tolerance or abs(c.high - level) / level < tolerance)
        return min(touches / 5, 1.0)

    @staticmethod
    def _rejection_strength(candle) -> float:
        """Wick / body ratio for the rejection candle (inverted: larger wick = higher)."""
        candle_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        if candle_range <= 0 or body <= 0:
            return 0.0
        wick = candle_range - body
        return min(wick / body / 3, 1.0)  # 3:1 wick:body → quality 1.0

    def _build_features(
        self,
        candles_1h: list,
        candles_15m: list,
        candles_5m: list,
        level: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        direction: str,
        market_regime: str | None,
    ) -> dict[str, object]:
        level_touch_count = self._level_touch_count(candles_1h, level)
        rejection_strength = self._rejection_strength(candles_5m[-1])

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_spike = avg_vol > 0 and candles_5m[-1].volume > avg_vol * 1.3

        regime_alignment = 1.0 if (
            (direction == "LONG" and market_regime == "TREND_UP") or
            (direction == "SHORT" and market_regime == "TREND_DOWN")
        ) else 0.3

        return {
            "level_touch_count": level_touch_count,
            "rejection_strength": rejection_strength,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
            "volume_spike": volume_spike,
            "regime_alignment": regime_alignment,
        }

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 30 or len(candles_15m) < 20 or len(candles_5m) < 15:
            return None
        support_levels = self._find_support_levels(candles_1h)
        if not support_levels:
            return None
        current_price = candles_5m[-1].close
        closest_support = None
        for level in sorted(support_levels, reverse=True):
            if level < current_price and (current_price - level) / current_price < self.level_distance_pct * 5:
                closest_support = level
                break
        if closest_support is None or abs(current_price - closest_support) / closest_support >= self.level_distance_pct:
            return None
        last = candles_5m[-1]
        if not (last.close > last.open and (min(last.open, last.close) - last.low) > abs(last.close - last.open)):
            return None
        disp = detect_displacement(candles_5m[-5:], n=2, threshold=1.0)
        if disp != "bullish_displacement" and last.close < candles_15m[-2].high:
            return None
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (closest_support * 0.02)
        invalidation = closest_support * 0.995
        target_1 = closest_support + atr * 2
        features = self._build_features(
            candles_1h, candles_15m, candles_5m,
            closest_support, closest_support, invalidation, target_1, atr,
            "LONG", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=closest_support, entry_zone_low=closest_support * 0.998, entry_zone_high=closest_support * 1.002,
            invalidation_price=invalidation, target_1=target_1, target_2=closest_support + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 30 or len(candles_15m) < 20 or len(candles_5m) < 15:
            return None
        resistance_levels = self._find_resistance_levels(candles_1h)
        if not resistance_levels:
            return None
        current_price = candles_5m[-1].close
        closest_resistance = None
        for level in sorted(resistance_levels):
            if level > current_price and (level - current_price) / current_price < self.level_distance_pct * 5:
                closest_resistance = level
                break
        if closest_resistance is None or abs(current_price - closest_resistance) / closest_resistance >= self.level_distance_pct:
            return None
        last = candles_5m[-1]
        if not (last.close < last.open and (last.high - max(last.open, last.close)) > abs(last.close - last.open)):
            return None
        disp = detect_displacement(candles_5m[-5:], n=2, threshold=1.0)
        if disp != "bearish_displacement" and last.close > candles_15m[-2].low:
            return None
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (closest_resistance * 0.02)
        invalidation = closest_resistance * 1.005
        target_1 = closest_resistance - atr * 2
        features = self._build_features(
            candles_1h, candles_15m, candles_5m,
            closest_resistance, closest_resistance, invalidation, target_1, atr,
            "SHORT", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=closest_resistance, entry_zone_low=closest_resistance * 0.998, entry_zone_high=closest_resistance * 1.002,
            invalidation_price=invalidation, target_1=target_1, target_2=closest_resistance - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        long_setup = self._scan_long(ctx)
        if long_setup:
            results.append(long_setup)
        short_setup = self._scan_short(ctx)
        if short_setup:
            results.append(short_setup)
        return results
