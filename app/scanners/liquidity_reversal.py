"""Scanner 03: Liquidity Sweep Reversal.

Quality features emitted (all normalised to [0, 1]):
    sweep_depth           – how far past the level the sweep went
    rejection_strength    – wick / body ratio of the rejection candle
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)
    volume_spike          – volume > 1.5× average
    regime_alignment      – regime direction matches trade direction
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_displacement, find_swing_highs, find_swing_lows

logger = logging.getLogger(__name__)


class LiquidityReversalScanner:
    name = "LIQUIDITY_REVERSAL"
    version = "2.0.0"

    def __init__(self, swing_lookback: int = 5, sweep_margin: float = 0.001) -> None:
        self.swing_lookback = swing_lookback
        self.sweep_margin = sweep_margin

    @staticmethod
    def _sweep_depth(swept_price: float, level: float) -> float:
        """How far past the level the sweep went, normalised to [0, 1]."""
        if level <= 0:
            return 0.0
        pct = abs(level - swept_price) / level
        return min(pct / 0.01, 1.0)  # 1% → quality 1.0

    @staticmethod
    def _rejection_strength(candle) -> float:
        """Wick / body ratio (inverted: larger wick = stronger rejection)."""
        candle_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        if candle_range <= 0 or body <= 0:
            return 0.0
        wick = candle_range - body
        return min(wick / body / 3, 1.0)

    def _build_features(
        self,
        candles_15m: list,
        candles_5m: list,
        swept_level: float,
        swept_price: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        direction: str,
        market_regime: str | None,
    ) -> dict[str, object]:
        sweep_depth = self._sweep_depth(swept_price, swept_level)
        rejection_strength = self._rejection_strength(candles_5m[-1])

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_spike = avg_vol > 0 and candles_5m[-1].volume > avg_vol * 1.5

        regime_alignment = 1.0 if (
            (direction == "LONG" and market_regime == "TREND_UP") or
            (direction == "SHORT" and market_regime == "TREND_DOWN")
        ) else 0.3

        return {
            "sweep_depth": sweep_depth,
            "rejection_strength": rejection_strength,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
            "volume_spike": volume_spike,
            "regime_alignment": regime_alignment,
        }

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            return None
        swing_lows = find_swing_lows(candles_15m, self.swing_lookback)
        if len(swing_lows) < 2:
            return None
        significant_levels = [swing_lows[-1].price]
        if ctx.levels.previous_day_low > 0:
            significant_levels.append(ctx.levels.previous_day_low)
        if ctx.levels.previous_week_low > 0:
            significant_levels.append(ctx.levels.previous_week_low)
        swept_level = None
        swept_price = None
        for level in significant_levels:
            for c in candles_5m[-8:]:
                if c.low < level * (1 - self.sweep_margin):
                    swept_level = level
                    swept_price = c.low
                    break
            if swept_level is not None:
                break
        if swept_level is None or swept_price is None:
            return None
        if candles_5m[-1].close <= swept_level:
            return None
        last = candles_5m[-1]
        if not (last.close > last.open and (min(last.open, last.close) - last.low) > 2 * abs(last.close - last.open)):
            return None
        disp = detect_displacement(candles_5m[-5:], n=2, threshold=1.0)
        if not (disp == "bullish_displacement" or last.close > candles_15m[-2].high):
            return None
        recent_low = min(c.low for c in candles_5m[-5:])
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (swept_level * 0.02)
        invalidation = recent_low * 0.998
        target_1 = swept_level + atr * 1.5
        features = self._build_features(
            candles_15m, candles_5m, swept_level, swept_price,
            swept_level, invalidation, target_1, atr,
            "LONG", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=swept_level, entry_zone_low=swept_level, entry_zone_high=swept_level * 1.003,
            invalidation_price=invalidation, target_1=target_1, target_2=swept_level + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            return None
        swing_highs = find_swing_highs(candles_15m, self.swing_lookback)
        if len(swing_highs) < 2:
            return None
        significant_levels = [swing_highs[-1].price]
        if ctx.levels.previous_day_high > 0:
            significant_levels.append(ctx.levels.previous_day_high)
        if ctx.levels.previous_week_high > 0:
            significant_levels.append(ctx.levels.previous_week_high)
        swept_level = None
        swept_price = None
        for level in significant_levels:
            for c in candles_5m[-8:]:
                if c.high > level * (1 + self.sweep_margin):
                    swept_level = level
                    swept_price = c.high
                    break
            if swept_level is not None:
                break
        if swept_level is None or swept_price is None:
            return None
        if candles_5m[-1].close >= swept_level:
            return None
        last = candles_5m[-1]
        if not (last.close < last.open and (last.high - max(last.open, last.close)) > 2 * abs(last.close - last.open)):
            return None
        disp = detect_displacement(candles_5m[-5:], n=2, threshold=1.0)
        if not (disp == "bearish_displacement" or last.close < candles_15m[-2].low):
            return None
        recent_high = max(c.high for c in candles_5m[-5:])
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (swept_level * 0.02)
        invalidation = recent_high * 1.002
        target_1 = swept_level - atr * 1.5
        features = self._build_features(
            candles_15m, candles_5m, swept_level, swept_price,
            swept_level, invalidation, target_1, atr,
            "SHORT", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=swept_level, entry_zone_low=swept_level * 0.997, entry_zone_high=swept_level,
            invalidation_price=invalidation, target_1=target_1, target_2=swept_level - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        levels = ctx.levels
        if any(value <= 0 for value in (
            levels.previous_day_high, levels.previous_day_low,
            levels.previous_week_high, levels.previous_week_low,
        )):
            logger.info("LIQUIDITY_REVERSAL skipped: HTF levels unavailable")
            return []
        results: list[SetupCandidate] = []
        long_setup = self._scan_long(ctx)
        if long_setup:
            results.append(long_setup)
        short_setup = self._scan_short(ctx)
        if short_setup:
            results.append(short_setup)
        return results
