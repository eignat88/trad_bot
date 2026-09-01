"""Scanner 07: Momentum Exhaustion / Failed Breakout.

Quality features emitted (all normalised to [0, 1]):
    exhaustion_magnitude  – how far past the prior swing, normalised
    body_ratio            – body / range ratio of the exhaustion candle
    rsi_confirmation      – RSI proximity to overbought/oversold zone
    volume_ratio          – volume / average volume, normalised
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs, find_swing_lows


class MomentumExhaustionScanner:
    name = "MOMENTUM_EXHAUSTION"
    version = "2.0.0"

    def __init__(self, swing_lookback: int = 5, exhaustion_threshold: float = 0.003) -> None:
        self.swing_lookback = swing_lookback
        self.exhaustion_threshold = exhaustion_threshold

    @staticmethod
    def _body_ratio(candle) -> float:
        """Body / range ratio (inverted: smaller body = better exhaustion signal)."""
        candle_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        if candle_range <= 0:
            return 0.0
        return 1.0 - min(body / candle_range, 1.0)  # inverted: weak body → high quality

    @staticmethod
    def _exhaustion_magnitude(recent_extreme: float, prior_level: float) -> float:
        """How far past the prior swing level, normalised to [0, 1]."""
        if prior_level <= 0:
            return 0.0
        overshoot = abs(recent_extreme - prior_level) / prior_level
        # 0.5% overshoot → 0.5 quality, 2%+ → 1.0
        return min(overshoot / 0.02, 1.0)

    def _build_short_features(
        self,
        candles_5m: list,
        prev_high: float,
        recent_high: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        rsi: float,
    ) -> dict[str, object]:
        exhaustion_magnitude = self._exhaustion_magnitude(recent_high, prev_high)
        body_ratio = self._body_ratio(candles_5m[-1])

        # RSI confirmation: how deep into overbought (65-80 range → 0-1)
        rsi_confirmation = max(0.0, min((rsi - 65) / 15, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_ratio = min(candles_5m[-1].volume / avg_vol / 2, 1.0) if avg_vol > 0 else 0.0

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        return {
            "exhaustion_magnitude": exhaustion_magnitude,
            "body_ratio": body_ratio,
            "rsi_confirmation": rsi_confirmation,
            "volume_ratio": volume_ratio,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
        }

    def _build_long_features(
        self,
        candles_5m: list,
        prev_low: float,
        recent_low: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        rsi: float,
    ) -> dict[str, object]:
        exhaustion_magnitude = self._exhaustion_magnitude(recent_low, prev_low)
        body_ratio = self._body_ratio(candles_5m[-1])

        # RSI confirmation: how deep into oversold (20-35 range → 0-1)
        rsi_confirmation = max(0.0, min((35 - rsi) / 15, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_ratio = min(candles_5m[-1].volume / avg_vol / 2, 1.0) if avg_vol > 0 else 0.0

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        return {
            "exhaustion_magnitude": exhaustion_magnitude,
            "body_ratio": body_ratio,
            "rsi_confirmation": rsi_confirmation,
            "volume_ratio": volume_ratio,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
        }

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
        invalidation = recent_high * 1.002
        target_1 = current_price - atr * 2
        features = self._build_short_features(
            candles_5m, prev_high, recent_high,
            current_price, invalidation, target_1, atr, ctx.indicators.rsi,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_high, entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
            invalidation_price=invalidation, target_1=target_1, target_2=current_price - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
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
        invalidation = recent_low * 0.998
        target_1 = current_price + atr * 2
        features = self._build_long_features(
            candles_5m, prev_low, recent_low,
            current_price, invalidation, target_1, atr, ctx.indicators.rsi,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_low, entry_zone_low=current_price * 0.999, entry_zone_high=current_price * 1.002,
            invalidation_price=invalidation, target_1=target_1, target_2=current_price + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
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
