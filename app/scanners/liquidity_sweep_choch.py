"""Scanner 01: Liquidity Sweep + CHOCH + OB Retest.

Quality features emitted (all normalised to [0, 1]):
    sweep_depth          – how far below/above the liquidity level the sweep went
    displacement_strength – body / range ratio of the displacement candle
    ob_distance          – proximity of current price to OB center (1 = at center)
    rr_ratio             – reward-to-risk normalised to [0, 1]
    stop_distance_atr    – stop distance in ATR units (inverted: tighter = higher)
    volume_spike         – volume > 1.3× average
    rsi_confirmation     – RSI proximity to oversold/overbought zone
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import detect_choch, detect_displacement, find_nearest_swing_high, find_nearest_swing_low, find_order_blocks, find_swing_highs, find_swing_lows


class LiquiditySweepCHOCHScanner:
    name = "LIQUIDITY_SWEEP_CHOCH_OB"
    version = "2.0.0"

    def __init__(self, ob_lookback: int = 5, swing_lookback: int = 5, sweep_margin: float = 0.001) -> None:
        self.ob_lookback = ob_lookback
        self.swing_lookback = swing_lookback
        self.sweep_margin = sweep_margin

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _displacement_strength(candles: list) -> float:
        """Body / range ratio of the last candle (0-1)."""
        last = candles[-1]
        candle_range = last.high - last.low
        if candle_range <= 0:
            return 0.0
        return min(abs(last.close - last.open) / candle_range, 1.0)

    @staticmethod
    def _ob_distance(price: float, ob: dict) -> float:
        """How close price is to OB center (1 = dead center, 0 = at edge)."""
        ob_center = (ob["high"] + ob["low"]) / 2
        ob_half = (ob["high"] - ob["low"]) / 2
        if ob_half <= 0:
            return 0.0
        return max(0.0, 1.0 - abs(price - ob_center) / ob_half)

    @staticmethod
    def _volume_spike(candles: list) -> bool:
        avg_vol = sum(c.volume for c in candles[-20:]) / min(len(candles), 20)
        return avg_vol > 0 and candles[-1].volume > avg_vol * 1.3

    @staticmethod
    def _sweep_depth_pct(swept_low: float, level: float) -> float:
        """How far below the level the sweep went (percentage, positive)."""
        if level <= 0:
            return 0.0
        return abs(level - swept_low) / level

    def _build_long_features(
        self,
        candles_5m: list,
        liquidity_level: float,
        current_ob: dict,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
    ) -> dict[str, object]:
        # sweep depth: lowest low in last 5 candles vs liquidity level
        swept_low = min(c.low for c in candles_5m[-5:])
        sweep_pct = self._sweep_depth_pct(swept_low, liquidity_level)
        # normalise: 2% sweep depth → quality 1.0
        sweep_depth = min(sweep_pct / 0.02, 1.0)

        displacement_strength = self._displacement_strength(candles_5m)
        ob_distance = self._ob_distance(candles_5m[-1].close, current_ob)

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-20:]) / min(len(candles_5m), 20)
        volume_spike = avg_vol > 0 and candles_5m[-1].volume > avg_vol * 1.3

        return {
            "sweep_depth": sweep_depth,
            "displacement_strength": displacement_strength,
            "ob_distance": ob_distance,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
            "volume_spike": volume_spike,
            "rsi_confirmation": 0.5,  # neutral — scanner doesn't use RSI directly
        }

    def _build_short_features(
        self,
        candles_5m: list,
        liquidity_level: float,
        current_ob: dict,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
    ) -> dict[str, object]:
        swept_high = max(c.high for c in candles_5m[-5:])
        sweep_pct = self._sweep_depth_pct(swept_high, liquidity_level)
        sweep_depth = min(sweep_pct / 0.02, 1.0)

        displacement_strength = self._displacement_strength(candles_5m)
        ob_distance = self._ob_distance(candles_5m[-1].close, current_ob)

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-20:]) / min(len(candles_5m), 20)
        volume_spike = avg_vol > 0 and candles_5m[-1].volume > avg_vol * 1.3

        return {
            "sweep_depth": sweep_depth,
            "displacement_strength": displacement_strength,
            "ob_distance": ob_distance,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
            "volume_spike": volume_spike,
            "rsi_confirmation": 0.5,
        }

    # -- scan directions --------------------------------------------------

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
        target_1 = entry_high + atr * 2
        features = self._build_long_features(
            candles_5m, liquidity_level, current_ob,
            entry_high, invalidation, target_1, atr,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(current_ob["timestamp"] / 1000, tz=timezone.utc),
            reference_price=liquidity_level, entry_zone_low=entry_low, entry_zone_high=entry_high,
            invalidation_price=invalidation, target_1=target_1, target_2=entry_high + atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
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
        target_1 = entry_low - atr * 2
        features = self._build_short_features(
            candles_5m, liquidity_level, current_ob,
            entry_low, invalidation, target_1, atr,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(current_ob["timestamp"] / 1000, tz=timezone.utc),
            reference_price=liquidity_level, entry_zone_low=entry_low, entry_zone_high=entry_high,
            invalidation_price=invalidation, target_1=target_1, target_2=entry_low - atr * 3,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
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
