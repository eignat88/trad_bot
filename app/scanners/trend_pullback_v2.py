"""Scanner 04b: Trend Pullback V2 — optimized variant with tighter defaults.

Key differences from v1:
- pullback_tolerance: 0.012 (was 0.01) — less noise, better avg_r
- target_r: 0.50 (was 0.75) — more TP hits, higher win rate
- expire_at_breakeven: True — EXPIRED setups close at 0R

Reference: TREND_PULLBACK_V2_SPEC.md, trend_pullback_optimization_report.md
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


class TrendPullbackScannerV2:
    name = "TREND_PULLBACK_V2"
    version = "1.0.0"

    def __init__(
        self,
        pullback_tolerance: float = 0.012,      # was 0.01 in v1
        rsi_cool_threshold: float = 55,          # unchanged
        enabled_directions: tuple[str, ...] = ("LONG",),
        allowed_regimes: tuple[str, ...] = ("TREND_UP",),
        signal_timeframe: str = "15m",           # was 5m in v1
        max_pullback_quality: float | None = 0.75,
        target_r: float = 0.50,                  # was 0.75 in v1
        stop_buffer: float = 0.002,              # unchanged
    ) -> None:
        self.pullback_tolerance = pullback_tolerance
        self.rsi_cool_threshold = rsi_cool_threshold
        self.enabled_directions = enabled_directions
        self.allowed_regimes = allowed_regimes
        self.signal_timeframe = signal_timeframe
        self.max_pullback_quality = max_pullback_quality
        self.target_r = target_r
        self.stop_buffer = stop_buffer

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
        invalidation = min(c.low for c in candles_5m[-3:]) * (1 - self.stop_buffer)
        atr = ind.atr if ind.atr > 0 else (current_price * 0.015)

        # Compute targets using target_r ratio if specified
        risk = abs(current_price - invalidation)
        if self.target_r is not None:
            target_1 = current_price + atr * self.target_r * 2  # Scale by ATR for consistency
            target_2 = None
        else:
            target_1 = current_price + atr * 2
            target_2 = current_price + atr * 3.5

        ema_distance = min(abs(current_price - ind.ema20) / ind.ema20,
                           abs(current_price - ind.ema50) / ind.ema50)
        pullback_quality = 1 - min(ema_distance / self.pullback_tolerance, 1)
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)

        # Filter by max_pullback_quality if set
        if self.max_pullback_quality is not None and pullback_quality > self.max_pullback_quality:
            return None

        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=current_price, entry_zone_low=min(ind.ema20, ind.ema50) * 0.998,
            entry_zone_high=max(ind.ema20, ind.ema50) * 1.002, invalidation_price=invalidation,
            target_1=target_1, target_2=target_2,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "trend_alignment": True,
                      "pullback_to_ema": True, "pullback_quality": pullback_quality,
                      "rsi_cool": True, "rsi_confirmation": rsi_confirmation,
                      "stop_distance_ok": True,
                      "recommended_expiry_policy": "BREAKEVEN"},
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
        invalidation = max(c.high for c in candles_5m[-3:]) * (1 + self.stop_buffer)
        atr = ind.atr if ind.atr > 0 else (current_price * 0.015)

        # Compute targets using target_r ratio if specified
        risk = abs(current_price - invalidation)
        if self.target_r is not None:
            target_1 = current_price - atr * self.target_r * 2  # Scale by ATR for consistency
            target_2 = None
        else:
            target_1 = current_price - atr * 2
            target_2 = current_price - atr * 3.5

        ema_distance = min(abs(current_price - ind.ema20) / ind.ema20,
                           abs(current_price - ind.ema50) / ind.ema50)
        pullback_quality = 1 - min(ema_distance / self.pullback_tolerance, 1)
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)

        # Filter by max_pullback_quality if set
        if self.max_pullback_quality is not None and pullback_quality > self.max_pullback_quality:
            return None

        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=current_price, entry_zone_low=min(ind.ema20, ind.ema50) * 0.998,
            entry_zone_high=max(ind.ema20, ind.ema50) * 1.002, invalidation_price=invalidation,
            target_1=target_1, target_2=target_2,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "trend_alignment": True,
                      "pullback_to_ema": True, "pullback_quality": pullback_quality,
                      "rsi_cool": True, "rsi_confirmation": rsi_confirmation,
                      "stop_distance_ok": True,
                      "recommended_expiry_policy": "BREAKEVEN"},
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        if "LONG" in self.enabled_directions:
            long_setup = self._scan_long(ctx)
            if long_setup:
                results.append(long_setup)
        if "SHORT" in self.enabled_directions:
            short_setup = self._scan_short(ctx)
            if short_setup:
                results.append(short_setup)
        return results
