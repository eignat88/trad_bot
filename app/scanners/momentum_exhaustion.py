"""Scanner 07: Momentum Exhaustion / Failed Breakout."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs, find_swing_lows


class MomentumExhaustionScanner:
    name = "MOMENTUM_EXHAUSTION"
    version = "1.0.0"

    def __init__(self, swing_lookback: int = 5, exhaustion_threshold: float = 0.003) -> None:
        self.swing_lookback = swing_lookback
        self.exhaustion_threshold = exhaustion_threshold

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        """Bearish exhaustion = SHORT."""
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            return None
        swing_highs = find_swing_highs(candles_15m, self.swing_lookback)
        if len(swing_highs) < 2:
            return None
        prev_high = swing_highs[-2].price
        recent_high = max(c.high for c in candles_5m[-5:])
        if recent_high <= prev_high:
            return None
        current_price = candles_5m[-1].close
        if current_price > prev_high * (1 + self.exhaustion_threshold):
            return None
        last = candles_5m[-1]
        if last.close > last.open:
            return None
        candle_range = last.high - last.low
        body = abs(last.close - last.open)
        if candle_range > 0 and body / candle_range > 0.7:
            return None
        if ctx.indicators.rsi < 65:
            return None
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (current_price * 0.015)
        avg_volume = sum(c.volume for c in candles_5m[-10:]) / 10
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_high, entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
            invalidation_price=recent_high * 1.002, target_1=current_price - atr * 2, target_2=current_price - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "new_high_failed": True,
                      "weak_continuation": True, "structure_break": True,
                      "rsi_confirmation": min((ctx.indicators.rsi - 65) / 15, 1),
                      "volume_confirmation": min(last.volume / avg_volume / 2, 1)
                      if avg_volume > 0 else 0, "stop_distance_ok": True},
        )

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        """Bullish exhaustion = LONG."""
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            return None
        swing_lows = find_swing_lows(candles_15m, self.swing_lookback)
        if len(swing_lows) < 2:
            return None
        prev_low = swing_lows[-2].price
        recent_low = min(c.low for c in candles_5m[-5:])
        if recent_low >= prev_low:
            return None
        current_price = candles_5m[-1].close
        if current_price < prev_low * (1 - self.exhaustion_threshold):
            return None
        last = candles_5m[-1]
        if last.close < last.open:
            return None
        candle_range = last.high - last.low
        body = abs(last.close - last.open)
        if candle_range > 0 and body / candle_range > 0.7:
            return None
        if ctx.indicators.rsi > 35:
            return None
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (current_price * 0.015)
        avg_volume = sum(c.volume for c in candles_5m[-10:]) / 10
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_low, entry_zone_low=current_price * 0.999, entry_zone_high=current_price * 1.002,
            invalidation_price=recent_low * 0.998, target_1=current_price + atr * 2, target_2=current_price + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "new_low_failed": True,
                      "weak_continuation": True, "structure_break": True,
                      "rsi_confirmation": min((35 - ctx.indicators.rsi) / 15, 1),
                      "volume_confirmation": min(last.volume / avg_volume / 2, 1)
                      if avg_volume > 0 else 0, "stop_distance_ok": True},
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        short_setup = self._scan_short(ctx)
        if short_setup:
            results.append(short_setup)
        long_setup = self._scan_long(ctx)
        if long_setup:
            results.append(long_setup)
        return results
