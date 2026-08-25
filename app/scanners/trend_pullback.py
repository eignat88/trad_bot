"""Scanner 04: Trend Pullback."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


class TrendPullbackScanner:
    name = "TREND_PULLBACK"
    version = "1.0.0"

    def __init__(self, pullback_tolerance: float = 0.005, rsi_cool_threshold: float = 55) -> None:
        self.pullback_tolerance = pullback_tolerance
        self.rsi_cool_threshold = rsi_cool_threshold

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 50 or len(candles_15m) < 30 or len(candles_5m) < 20:
            return None
        ind = ctx.indicators
        if ind.ema20 <= 0 or ind.ema50 <= 0 or ind.ema200 <= 0:
            return None
        if not (ind.ema20 > ind.ema50 and candles_1h[-1].close > ind.ema200):
            return None
        current_price = candles_5m[-1].close
        near_ema20 = abs(current_price - ind.ema20) / ind.ema20 < self.pullback_tolerance
        near_ema50 = abs(current_price - ind.ema50) / ind.ema50 < self.pullback_tolerance
        if not (near_ema20 or near_ema50):
            return None
        if ind.rsi > self.rsi_cool_threshold:
            return None
        if candles_5m[-1].close < candles_5m[-1].open:
            return None
        invalidation = min(c.low for c in candles_5m[-3:]) * 0.998
        atr = ind.atr if ind.atr > 0 else (current_price * 0.015)
        ema_distance = min(abs(current_price - ind.ema20) / ind.ema20,
                           abs(current_price - ind.ema50) / ind.ema50)
        pullback_quality = 1 - min(ema_distance / self.pullback_tolerance, 1)
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=current_price, entry_zone_low=min(ind.ema20, ind.ema50) * 0.998,
            entry_zone_high=max(ind.ema20, ind.ema50) * 1.002, invalidation_price=invalidation,
            target_1=current_price + atr * 2, target_2=current_price + atr * 3.5,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "trend_alignment": True,
                      "pullback_to_ema": True, "pullback_quality": pullback_quality,
                      "rsi_cool": True, "rsi_confirmation": rsi_confirmation,
                      "stop_distance_ok": True},
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_5m = list(ctx.candles_1h), list(ctx.candles_5m)
        if len(candles_1h) < 50 or len(candles_5m) < 20:
            return None
        ind = ctx.indicators
        if ind.ema20 <= 0 or ind.ema50 <= 0 or ind.ema200 <= 0:
            return None
        if not (ind.ema20 < ind.ema50 and candles_1h[-1].close < ind.ema200):
            return None
        current_price = candles_5m[-1].close
        near_ema20 = abs(current_price - ind.ema20) / ind.ema20 < self.pullback_tolerance
        near_ema50 = abs(current_price - ind.ema50) / ind.ema50 < self.pullback_tolerance
        if not (near_ema20 or near_ema50):
            return None
        if ind.rsi < (100 - self.rsi_cool_threshold):
            return None
        if candles_5m[-1].close > candles_5m[-1].open:
            return None
        invalidation = max(c.high for c in candles_5m[-3:]) * 1.002
        atr = ind.atr if ind.atr > 0 else (current_price * 0.015)
        ema_distance = min(abs(current_price - ind.ema20) / ind.ema20,
                           abs(current_price - ind.ema50) / ind.ema50)
        pullback_quality = 1 - min(ema_distance / self.pullback_tolerance, 1)
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=current_price, entry_zone_low=min(ind.ema20, ind.ema50) * 0.998,
            entry_zone_high=max(ind.ema20, ind.ema50) * 1.002, invalidation_price=invalidation,
            target_1=current_price - atr * 2, target_2=current_price - atr * 3.5,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "trend_alignment": True,
                      "pullback_to_ema": True, "pullback_quality": pullback_quality,
                      "rsi_cool": True, "rsi_confirmation": rsi_confirmation,
                      "stop_distance_ok": True},
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
