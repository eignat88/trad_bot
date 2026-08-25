"""Scanner 01: Liquidity Sweep + CHOCH + OB Retest."""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_choch, detect_displacement, find_nearest_swing_high, find_nearest_swing_low, find_order_blocks, find_swing_highs, find_swing_lows


class LiquiditySweepCHOCHScanner:
    name = "LIQUIDITY_SWEEP_CHOCH_OB"
    version = "1.0.0"

    def __init__(self, ob_lookback: int = 5, swing_lookback: int = 5, sweep_margin: float = 0.001) -> None:
        self.ob_lookback = ob_lookback
        self.swing_lookback = swing_lookback
        self.sweep_margin = sweep_margin

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 30 or len(candles_15m) < 30 or len(candles_5m) < 20:
            return None
        obs = find_order_blocks(candles_1h, self.ob_lookback)
        bullish_obs = [ob for ob in obs if ob["type"] == "bullish"]
        if not bullish_obs:
            return None
        current_price = candles_5m[-1].close
        current_ob = None
        for ob in reversed(bullish_obs):
            if ob["low"] <= current_price <= ob["high"]:
                current_ob = ob
                break
        if current_ob is None:
            return None
        swing_lows = find_swing_lows(candles_15m, self.swing_lookback)
        if not swing_lows:
            return None
        recent_low = min(c.low for c in candles_5m[-10:])
        liquidity_level = swing_lows[-1].price
        swept = any(c.low < liquidity_level * (1 - self.sweep_margin) for c in candles_5m[-5:])
        if not swept:
            return None
        if candles_5m[-1].close <= liquidity_level:
            return None
        if detect_choch(candles_15m, self.swing_lookback) != "bullish_choch":
            return None
        if detect_displacement(candles_5m) != "bullish_displacement":
            return None
        entry_low = max(liquidity_level, current_ob["low"])
        entry_high = current_ob["high"]
        invalidation = recent_low * 0.998
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (entry_high - entry_low) * 2
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(current_ob["timestamp"] / 1000, tz=timezone.utc),
            reference_price=liquidity_level, entry_zone_low=entry_low, entry_zone_high=entry_high,
            invalidation_price=invalidation, target_1=entry_high + atr * 2, target_2=entry_high + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "liquidity_sweep": True, "choch": True, "ob_confluence": True, "displacement": True, "retest_quality": True},
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 30 or len(candles_15m) < 30 or len(candles_5m) < 20:
            return None
        obs = find_order_blocks(candles_1h, self.ob_lookback)
        bearish_obs = [ob for ob in obs if ob["type"] == "bearish"]
        if not bearish_obs:
            return None
        current_price = candles_5m[-1].close
        current_ob = None
        for ob in reversed(bearish_obs):
            if ob["low"] <= current_price <= ob["high"]:
                current_ob = ob
                break
        if current_ob is None:
            return None
        swing_highs = find_swing_highs(candles_15m, self.swing_lookback)
        if not swing_highs:
            return None
        recent_high = max(c.high for c in candles_5m[-10:])
        liquidity_level = swing_highs[-1].price
        swept = any(c.high > liquidity_level * (1 + self.sweep_margin) for c in candles_5m[-5:])
        if not swept:
            return None
        if candles_5m[-1].close >= liquidity_level:
            return None
        if detect_choch(candles_15m, self.swing_lookback) != "bearish_choch":
            return None
        if detect_displacement(candles_5m) != "bearish_displacement":
            return None
        entry_high = min(liquidity_level, current_ob["high"])
        entry_low = current_ob["low"]
        invalidation = recent_high * 1.002
        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (entry_high - entry_low) * 2
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(current_ob["timestamp"] / 1000, tz=timezone.utc),
            reference_price=liquidity_level, entry_zone_low=entry_low, entry_zone_high=entry_high,
            invalidation_price=invalidation, target_1=entry_low - atr * 2, target_2=entry_low - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features={"htf_context": True, "liquidity_sweep": True, "choch": True, "ob_confluence": True, "displacement": True, "retest_quality": True},
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
