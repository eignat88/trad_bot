"""Scanner 03: Liquidity Sweep Reversal."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_displacement, find_swing_highs, find_swing_lows

logger = logging.getLogger(__name__)


class LiquidityReversalScanner:
    name = "LIQUIDITY_REVERSAL"
    version = "1.0.0"

    def __init__(self, swing_lookback: int = 5, sweep_margin: float = 0.001) -> None:
        self.swing_lookback = swing_lookback
        self.sweep_margin = sweep_margin

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
        for level in significant_levels:
            if any(c.low < level * (1 - self.sweep_margin) for c in candles_5m[-8:]):
                swept_level = level
                break
        if swept_level is None:
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
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=swept_level, entry_zone_low=swept_level, entry_zone_high=swept_level * 1.003,
            invalidation_price=recent_low * 0.998, target_1=swept_level + atr * 1.5, target_2=swept_level + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"liquidity_sweep": True, "rejection": True, "structure_confirmation": True, "volume_spike": last.volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.5, "stop_distance_ok": True},
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
        for level in significant_levels:
            if any(c.high > level * (1 + self.sweep_margin) for c in candles_5m[-8:]):
                swept_level = level
                break
        if swept_level is None:
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
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=swept_level, entry_zone_low=swept_level * 0.997, entry_zone_high=swept_level,
            invalidation_price=recent_high * 1.002, target_1=swept_level - atr * 1.5, target_2=swept_level - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"liquidity_sweep": True, "rejection": True, "structure_confirmation": True, "volume_spike": last.volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.5, "stop_distance_ok": True},
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
