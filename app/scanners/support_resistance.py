"""Scanner 06: Support/Resistance Reaction."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_displacement, find_swing_highs, find_swing_lows


class SupportResistanceScanner:
    name = "SUPPORT_RESISTANCE_REACTION"
    version = "1.0.0"

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
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=closest_support, entry_zone_low=closest_support * 0.998, entry_zone_high=closest_support * 1.002,
            invalidation_price=closest_support * 0.995, target_1=closest_support + atr * 2, target_2=closest_support + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "level_touches": True, "rejection": True, "structure_confirmation": True, "volume_spike": last.volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.3, "stop_distance_ok": True},
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
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=closest_resistance, entry_zone_low=closest_resistance * 0.998, entry_zone_high=closest_resistance * 1.002,
            invalidation_price=closest_resistance * 1.005, target_1=closest_resistance - atr * 2, target_2=closest_resistance - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "level_touches": True, "rejection": True, "structure_confirmation": True, "volume_spike": last.volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.3, "stop_distance_ok": True},
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
