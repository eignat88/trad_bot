"""Scanner 02: Breakout + Retest.

Quality features emitted (all normalised to [0, 1]):
    volume_ratio          – breakout volume / average volume (graduated)
    retest_distance       – how close price is to retest zone center
    rr_ratio              – reward-to-risk normalised
    stop_distance_atr     – stop distance in ATR (inverted: tighter = higher)
    regime_alignment      – regime direction matches trade direction
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs, find_swing_lows


class BreakoutRetestScanner:
    name = "BREAKOUT_RETEST"
    version = "2.0.0"

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

    def _build_features(
        self,
        candles_15m: list,
        candles_5m: list,
        level: float,
        current_price: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        breakout_vol: float,
        avg_vol: float,
        direction: str,
        market_regime: str | None,
    ) -> dict[str, object]:
        # Volume ratio: breakout volume relative to average, normalised
        volume_ratio = min(breakout_vol / avg_vol / 2, 1.0) if avg_vol > 0 else 0.0

        # Retest distance: how close price is to the breakout level
        retest_distance = max(0.0, 1.0 - abs(current_price - level) / (level * self.retest_margin))

        # R:R ratio
        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        # Stop distance in ATR
        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        # Regime alignment
        regime_alignment = 1.0 if (
            (direction == "LONG" and market_regime == "TREND_UP") or
            (direction == "SHORT" and market_regime == "TREND_DOWN")
        ) else 0.3

        return {
            "volume_ratio": volume_ratio,
            "retest_distance": retest_distance,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
            "regime_alignment": regime_alignment,
        }

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
        if avg_vol <= 0 or breakout_vol < avg_vol * 1.2:
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
        target_1 = resistance + atr * 2
        target_2 = resistance + atr * 3.5
        if invalidation >= retest_zone_low or target_1 <= retest_zone_high:
            return None
        features = self._build_features(
            candles_15m, candles_5m, resistance, current_price,
            resistance, invalidation, target_1, atr,
            breakout_vol, avg_vol, "LONG", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(candles_15m[breakout_candle_idx].timestamp / 1000, tz=timezone.utc),
            reference_price=resistance, entry_zone_low=retest_zone_low, entry_zone_high=retest_zone_high,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
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
        if avg_vol <= 0 or breakdown_vol < avg_vol * 1.2:
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
        target_1 = support - atr * 2
        target_2 = support - atr * 3.5
        if invalidation <= retest_zone_high or target_1 >= retest_zone_low:
            return None
        features = self._build_features(
            candles_15m, candles_5m, support, current_price,
            support, invalidation, target_1, atr,
            breakdown_vol, avg_vol, "SHORT", ctx.market_regime,
        )
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.fromtimestamp(candles_15m[breakdown_candle_idx].timestamp / 1000, tz=timezone.utc),
            reference_price=support, entry_zone_low=retest_zone_low, entry_zone_high=retest_zone_high,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
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
