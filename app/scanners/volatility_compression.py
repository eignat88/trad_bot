"""Scanner 05: Volatility Compression -> Expansion.

Quality features emitted (all normalised to [0, 1]):
    squeeze_duration      – number of bars in squeeze, normalised to 20
    expansion_ratio       – recent range / previous range, normalised
    bb_width_percentile   – Bollinger Band width percentile (inverted: tighter = higher)
    volume_ratio          – current volume / average volume, normalised
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


class VolatilityCompressionScanner:
    name = "VOLATILITY_COMPRESSION"
    version = "2.0.0"

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

    def _bb_squeeze(self, candles: list) -> tuple[bool, float]:
        """Return (is_squeeze, bb_width) — bb_width is the raw normalised width."""
        if len(candles) < self.squeeze_lookback:
            return False, 1.0
        recent = candles[-self.squeeze_lookback:]
        closes = [c.close for c in recent]
        mean = sum(closes) / len(closes)
        variance = sum((c - mean) ** 2 for c in closes) / len(closes)
        std = variance ** 0.5
        bb_width = (2 * std) / mean if mean > 0 else 0
        is_squeeze = bb_width < self.bb_squeeze_threshold
        # Inverted percentile: tighter squeeze → higher quality
        bb_width_pct = max(0.0, 1.0 - min(bb_width / self.bb_squeeze_threshold, 1.0))
        return is_squeeze, bb_width_pct

    def _squeeze_duration(self, candles: list) -> float:
        """Count consecutive bars where ATR percentile is low, normalised to [0, 1]."""
        if len(candles) < self.atr_period + 1:
            return 0.0
        ranges = [candles[i].high - candles[i].low for i in range(self.atr_period, len(candles))]
        if not ranges:
            return 0.0
        current_atr = sum(ranges[-self.atr_period:]) / self.atr_period
        duration = 0
        for r in reversed(ranges):
            if r < current_atr:
                duration += 1
            else:
                break
        return min(duration / 20, 1.0)  # 20+ bars → quality 1.0

    def _expansion_ratio(self, candles: list) -> float:
        """Recent range / previous range, normalised to [0, 1]."""
        if len(candles) < 8:
            return 0.0
        recent_ranges = [c.high - c.low for c in candles[-3:]]
        prev_ranges = [c.high - c.low for c in candles[-8:-3]]
        if not prev_ranges:
            return 0.0
        avg_recent = sum(recent_ranges) / 3
        avg_prev = sum(prev_ranges) / 5
        if avg_prev <= 0:
            return 0.0
        ratio = avg_recent / avg_prev
        # 1.5× → quality 0.25, 3× → quality 1.0
        return max(0.0, min((ratio - 1) / 2, 1.0))

    def _build_features(
        self,
        candles_15m: list,
        candles_5m: list,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
    ) -> dict[str, object]:
        squeeze_duration = self._squeeze_duration(candles_15m)
        _, bb_width_pct = self._bb_squeeze(candles_15m)
        expansion_ratio = self._expansion_ratio(candles_5m)

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_ratio = min(candles_5m[-1].volume / avg_vol / 1.5, 1.0) if avg_vol > 0 else 0.0

        return {
            "squeeze_duration": squeeze_duration,
            "expansion_ratio": expansion_ratio,
            "bb_width_percentile": bb_width_pct,
            "volume_ratio": volume_ratio,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
        }

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 20:
            return results
        atr_pct = self._atr_percentile(candles_15m)
        bb_squeeze, _ = self._bb_squeeze(candles_15m)
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
            invalidation = recent_low * 0.998
            target_1 = current_price + atr * 2
            features = self._build_features(
                candles_15m, candles_5m,
                current_price, invalidation, target_1, atr,
            )
            results.append(SetupCandidate(
                scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
                direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
                detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
                reference_price=current_price, entry_zone_low=current_price * 0.999, entry_zone_high=current_price * 1.002,
                invalidation_price=invalidation, target_1=target_1, target_2=current_price + atr * 3,
                market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
                features=features,
            ))
        else:
            invalidation = recent_high * 1.002
            target_1 = current_price - atr * 2
            features = self._build_features(
                candles_15m, candles_5m,
                current_price, invalidation, target_1, atr,
            )
            results.append(SetupCandidate(
                scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
                direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
                detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
                reference_price=current_price, entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
                invalidation_price=invalidation, target_1=target_1, target_2=current_price - atr * 3,
                market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
                features=features,
            ))
        return results

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
