"""Scanner 05: Volatility Compression -> Expansion."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


class VolatilityCompressionScanner:
    name = "VOLATILITY_COMPRESSION"
    version = "1.0.0"

    def __init__(self, atr_period: int = 14, squeeze_lookback: int = 20, bb_squeeze_threshold: float = 0.02) -> None:
        self.atr_period = atr_period
        self.squeeze_lookback = squeeze_lookback
        self.bb_squeeze_threshold = bb_squeeze_threshold

    def _atr_percentile(self, candles: list) -> float:
        if len(candles) < self.atr_period + 1:
            return 50.0
        ranges = [candles[i].high - candles[i].low for i in range(self.atr_period, len(candles))]
        if not ranges:
            return 50.0
        current_atr = sum(ranges[-self.atr_period:]) / self.atr_period
        count_below = sum(1 for r in ranges if r < current_atr)
        return count_below / len(ranges) * 100

    def _bb_squeeze(self, candles: list) -> bool:
        if len(candles) < self.squeeze_lookback:
            return False
        recent = candles[-self.squeeze_lookback:]
        closes = [c.close for c in recent]
        mean = sum(closes) / len(closes)
        variance = sum((c - mean) ** 2 for c in closes) / len(closes)
        std = variance ** 0.5
        bb_width = (2 * std) / mean if mean > 0 else 0
        return bb_width < self.bb_squeeze_threshold

    def _detect_expansion(self, candles: list) -> str | None:
        if len(candles) < 3:
            return None
        recent_ranges = [c.high - c.low for c in candles[-3:]]
        prev_ranges = [c.high - c.low for c in candles[-8:-3]]
        if not prev_ranges:
            return None
        avg_recent = sum(recent_ranges) / 3
        avg_prev = sum(prev_ranges) / 5
        if avg_prev <= 0 or avg_recent / avg_prev < 1.5:
            return None
        last = candles[-1]
        return "bullish_expansion" if last.close > last.open else "bearish_expansion"

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 20:
            return results
        atr_pct = self._atr_percentile(candles_15m)
        bb_squeeze = self._bb_squeeze(candles_15m)
        if atr_pct > 30 and not bb_squeeze:
            return results
        expansion = self._detect_expansion(candles_5m)
        if expansion is None:
            return results
        current_price = candles_5m[-1].close
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (current_price * 0.01)
        recent_high = max(c.high for c in candles_15m[-10:])
        recent_low = min(c.low for c in candles_15m[-10:])
        if expansion == "bullish_expansion":
            results.append(SetupCandidate(
                scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
                direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
                detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
                reference_price=current_price, entry_zone_low=current_price * 0.999, entry_zone_high=current_price * 1.002,
                invalidation_price=recent_low * 0.998, target_1=current_price + atr * 2, target_2=current_price + atr * 3,
                market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
                features={"htf_context": True, "squeeze_detected": True, "volatility_expansion": True, "volume_spike": candles_5m[-1].volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.3, "stop_distance_ok": True},
            ))
        else:
            results.append(SetupCandidate(
                scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
                direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
                detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
                reference_price=current_price, entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
                invalidation_price=recent_high * 1.002, target_1=current_price - atr * 2, target_2=current_price - atr * 3,
                market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
                features={"htf_context": True, "squeeze_detected": True, "volatility_expansion": True, "volume_spike": candles_5m[-1].volume > sum(c.volume for c in candles_5m[-10:]) / 10 * 1.3, "stop_distance_ok": True},
            ))
        return results
