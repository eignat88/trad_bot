"""Scanner 02: Breakout + Retest."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs, find_swing_lows


class BreakoutRetestScanner:
    name = "BREAKOUT_RETEST"
    version = "1.0.0"

    def __init__(self, swing_lookback: int = 5, breakout_margin: float = 0.001, retest_margin: float = 0.003) -> None:
        self.swing_lookback = swing_lookback
        self.breakout_margin = breakout_margin
        self.retest_margin = retest_margin

    def _find_breakout_level(self, candles: list, direction: str, lookback: int = 50) -> float | None:
        subset = candles[-lookback:] if len(candles) > lookback else candles
        if direction == "LONG":
            highs = find_swing_highs(subset, self.swing_lookback)
            return highs[-1].price if highs else None
        lows = find_swing_lows(subset, self.swing_lookback)
        return lows[-1].price if lows else None

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 20:
            return None
        resistance = self._find_breakout_level(candles_15m, "LONG")
        if resistance is None:
            return None
        breakout_candle_idx = None
        for i in range(len(candles_15m) - 10, len(candles_15m)):
            if candles_15m[i].close > resistance * (1 + self.breakout_margin):
                breakout_candle_idx = i
                break
        if breakout_candle_idx is None:
            return None
        breakout_vol = candles_15m[breakout_candle_idx].volume
        avg_vol = sum(c.volume for c in candles_15m[-20:]) / 20
        if breakout_vol < avg_vol * 1.2:
            return None
        current_price = candles_5m[-1].close
        retest_zone_high = resistance * (1 + self.retest_margin)
        retest_zone_low = resistance * (1 - self.retest_margin)
        if not (retest_zone_low <= current_price <= retest_zone_high):
            return None
        if candles_5m[-1].close < candles_5m[-1].open:
            return None
        recent_low = min(c.low for c in candles_5m[-5:])
        invalidation = recent_low * 0.998
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (resistance * 0.02)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(candles_15m[breakout_candle_idx].timestamp / 1000, tz=timezone.utc),
            reference_price=resistance, entry_zone_low=retest_zone_low, entry_zone_high=retest_zone_high,
            invalidation_price=invalidation, target_1=resistance + atr * 2, target_2=resistance + atr * 3.5,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "breakout_level": resistance, "volume_spike": breakout_vol > avg_vol * 1.5, "retest_quality": True, "stop_distance_ok": True},
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 20:
            return None
        support = self._find_breakout_level(candles_15m, "SHORT")
        if support is None:
            return None
        breakdown_candle_idx = None
        for i in range(len(candles_15m) - 10, len(candles_15m)):
            if candles_15m[i].close < support * (1 - self.breakout_margin):
                breakdown_candle_idx = i
                break
        if breakdown_candle_idx is None:
            return None
        breakdown_vol = candles_15m[breakdown_candle_idx].volume
        avg_vol = sum(c.volume for c in candles_15m[-20:]) / 20
        if breakdown_vol < avg_vol * 1.2:
            return None
        current_price = candles_5m[-1].close
        retest_zone_high = support * (1 + self.retest_margin)
        retest_zone_low = support * (1 - self.retest_margin)
        if not (retest_zone_low <= current_price <= retest_zone_high):
            return None
        if candles_5m[-1].close > candles_5m[-1].open:
            return None
        recent_high = max(c.high for c in candles_5m[-5:])
        invalidation = recent_high * 1.002
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (support * 0.02)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(candles_15m[breakdown_candle_idx].timestamp / 1000, tz=timezone.utc),
            reference_price=support, entry_zone_low=retest_zone_low, entry_zone_high=retest_zone_high,
            invalidation_price=invalidation, target_1=support - atr * 2, target_2=support - atr * 3.5,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "breakout_level": support, "volume_spike": breakdown_vol > avg_vol * 1.5, "retest_quality": True, "stop_distance_ok": True},
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
