"""Scanner 04: Trend Pullback."""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Candle
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


class TrendPullbackScanner:
    name = "TREND_PULLBACK"
    version = "1.1.0"

    def __init__(
        self,
        pullback_tolerance: float = 0.01,
        rsi_cool_threshold: float = 55,
        enabled_directions: tuple[str, ...] = ("LONG", "SHORT"),
        allowed_regimes: tuple[str, ...] = ("TREND_UP", "TREND_DOWN", "RANGE"),
        signal_timeframe: str = "5m",
        max_pullback_quality: float | None = 0.75,
        target_r: float | None = None,
        stop_buffer: float = 0.002,
    ) -> None:
        self.pullback_tolerance = pullback_tolerance
        self.rsi_cool_threshold = rsi_cool_threshold
        self.enabled_directions = enabled_directions
        self.allowed_regimes = allowed_regimes
        self.signal_timeframe = signal_timeframe
        self.max_pullback_quality = max_pullback_quality
        self.target_r = target_r
        self.stop_buffer = stop_buffer

    def _signal_candles(self, ctx: MarketContext) -> list[Candle]:
        if self.signal_timeframe == "15m":
            return list(ctx.candles_15m)
        if self.signal_timeframe == "5m":
            return list(ctx.candles_5m)
        if self.signal_timeframe == "1h":
            return list(ctx.candles_1h)
        raise ValueError(f"Unsupported signal_timeframe: {self.signal_timeframe}")

    def _regime_allowed(self, ctx: MarketContext) -> bool:
        return not self.allowed_regimes or ctx.market_regime in self.allowed_regimes

    def _pullback_anchor(self, current_price: float, ema20: float, ema50: float) -> tuple[str, float] | None:
        near_ema20 = abs(current_price - ema20) / ema20 < self.pullback_tolerance
        near_ema50 = abs(current_price - ema50) / ema50 < self.pullback_tolerance
        if near_ema20 and near_ema50:
            return ("EMA20", ema20) if abs(current_price - ema20) <= abs(current_price - ema50) else ("EMA50", ema50)
        if near_ema20:
            return "EMA20", ema20
        if near_ema50:
            return "EMA50", ema50
        return None

    @staticmethod
    def _entry_zone(anchor_ema: float) -> tuple[float, float]:
        return anchor_ema * 0.998, anchor_ema * 1.002

    def _pullback_quality(self, current_price: float, anchor_ema: float) -> float:
        ema_distance = abs(current_price - anchor_ema) / anchor_ema
        return 1 - min(ema_distance / self.pullback_tolerance, 1)

    def _quality_allowed(self, pullback_quality: float) -> bool:
        return self.max_pullback_quality is None or pullback_quality <= self.max_pullback_quality

    def _score_long(self, ctx: MarketContext, pullback_quality: float, signal_candle: Candle) -> tuple[int, tuple[str, ...]]:
        ind = ctx.indicators
        score = 0
        reasons: list[str] = []

        if ctx.market_regime == "TREND_UP":
            score += 25
            reasons.append("TREND_UP regime")
        if ind.ema20 > ind.ema50 > ind.ema200:
            score += 20
            reasons.append("EMA20>EMA50>EMA200")
        if 0.25 <= pullback_quality <= 0.75:
            score += 20
            reasons.append("pullback_quality preferred range")
        elif pullback_quality <= 0.85:
            score += 10
            reasons.append("pullback_quality acceptable")
        if 45 <= ind.rsi <= self.rsi_cool_threshold:
            score += 20
            reasons.append("RSI cooled")
        elif ind.rsi <= self.rsi_cool_threshold:
            score += 10
            reasons.append("RSI below cool threshold")
        if signal_candle.close >= signal_candle.open:
            score += 15
            reasons.append("bullish signal candle")

        return min(score, 100), tuple(reasons)

    def _score_short(self, ctx: MarketContext, pullback_quality: float, signal_candle: Candle) -> tuple[int, tuple[str, ...]]:
        ind = ctx.indicators
        score = 0
        reasons: list[str] = []

        if ctx.market_regime == "TREND_DOWN":
            score += 25
            reasons.append("TREND_DOWN regime")
        if ind.ema20 < ind.ema50 < ind.ema200:
            score += 20
            reasons.append("EMA20<EMA50<EMA200")
        if 0.25 <= pullback_quality <= 0.75:
            score += 20
            reasons.append("pullback_quality preferred range")
        elif pullback_quality <= 0.85:
            score += 10
            reasons.append("pullback_quality acceptable")
        if (100 - self.rsi_cool_threshold) <= ind.rsi <= 55:
            score += 20
            reasons.append("RSI cooled")
        elif ind.rsi >= (100 - self.rsi_cool_threshold):
            score += 10
            reasons.append("RSI above cool threshold")
        if signal_candle.close <= signal_candle.open:
            score += 15
            reasons.append("bearish signal candle")

        return min(score, 100), tuple(reasons)

    def _base_features(self, *, anchor_name: str, anchor_ema: float, pullback_quality: float,
                       rsi_confirmation: float, risk: float) -> dict[str, object]:
        return {
            "htf_context": True,
            "trend_alignment": True,
            "pullback_to_ema": True,
            "pullback_anchor": anchor_name,
            "pullback_anchor_price": anchor_ema,
            "pullback_quality": pullback_quality,
            "rsi_cool": True,
            "rsi_confirmation": rsi_confirmation,
            "stop_distance_ok": True,
            "target_r": self.target_r,
            "risk_r": risk,
            "signal_timeframe": self.signal_timeframe,
            "recommended_expiry_bars": 144,
            "recommended_expiry_policy": "BREAKEVEN",
        }

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        if not self._regime_allowed(ctx):
            return None
        candles_1h = list(ctx.candles_1h)
        signal_candles = self._signal_candles(ctx)
        if len(candles_1h) < 50 or len(signal_candles) < 20:
            return None
        if self.signal_timeframe == "15m" and len(signal_candles) < 30:
            return None
        ind = ctx.indicators
        if ind.ema20 <= 0 or ind.ema50 <= 0 or ind.ema200 <= 0:
            return None
        if not (ind.ema20 > ind.ema50 and candles_1h[-1].close > ind.ema200):
            return None
        signal_candle = signal_candles[-1]
        current_price = signal_candle.close
        anchor = self._pullback_anchor(current_price, ind.ema20, ind.ema50)
        if anchor is None:
            return None
        anchor_name, anchor_ema = anchor
        pullback_quality = self._pullback_quality(current_price, anchor_ema)
        if not self._quality_allowed(pullback_quality):
            return None
        if ind.rsi > self.rsi_cool_threshold or signal_candle.close < signal_candle.open:
            return None
        entry_zone_low, entry_zone_high = self._entry_zone(anchor_ema)
        invalidation = min(c.low for c in signal_candles[-3:]) * (1 - self.stop_buffer)
        entry = entry_zone_high
        risk = abs(entry - invalidation)
        target_1 = entry + risk * self.target_r
        target_2 = None
        if risk <= 0 or invalidation >= entry_zone_low or target_1 <= entry_zone_high:
            return None
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)
        score, reasons = self._score_long(ctx, pullback_quality, signal_candle)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h",
            setup_timeframe=self.signal_timeframe, entry_timeframe=self.signal_timeframe,
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=signal_candle.timestamp,
            reference_price=current_price, entry_zone_low=entry_zone_low, entry_zone_high=entry_zone_high,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            score=score, market_regime=ctx.market_regime, reasons=reasons, state=SetupState.SETUP_READY,
            features=self._base_features(
                anchor_name=anchor_name,
                anchor_ema=anchor_ema,
                pullback_quality=pullback_quality,
                rsi_confirmation=rsi_confirmation,
                risk=risk,
            ),
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        if not self._regime_allowed(ctx):
            return None
        candles_1h = list(ctx.candles_1h)
        signal_candles = self._signal_candles(ctx)
        if len(candles_1h) < 50 or len(signal_candles) < 20:
            return None
        if self.signal_timeframe == "15m" and len(signal_candles) < 30:
            return None
        ind = ctx.indicators
        if ind.ema20 <= 0 or ind.ema50 <= 0 or ind.ema200 <= 0:
            return None
        if not (ind.ema20 < ind.ema50 and candles_1h[-1].close < ind.ema200):
            return None
        signal_candle = signal_candles[-1]
        current_price = signal_candle.close
        anchor = self._pullback_anchor(current_price, ind.ema20, ind.ema50)
        if anchor is None:
            return None
        anchor_name, anchor_ema = anchor
        pullback_quality = self._pullback_quality(current_price, anchor_ema)
        if not self._quality_allowed(pullback_quality):
            return None
        if ind.rsi < (100 - self.rsi_cool_threshold) or signal_candle.close > signal_candle.open:
            return None
        entry_zone_low, entry_zone_high = self._entry_zone(anchor_ema)
        invalidation = max(c.high for c in signal_candles[-3:]) * (1 + self.stop_buffer)
        entry = entry_zone_low
        risk = abs(entry - invalidation)
        target_1 = entry - risk * self.target_r
        target_2 = None
        if risk <= 0 or invalidation <= entry_zone_high or target_1 >= entry_zone_low:
            return None
        rsi_confirmation = 1 - min(abs(ind.rsi - 50) / 15, 1)
        score, reasons = self._score_short(ctx, pullback_quality, signal_candle)
        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value, htf_timeframe="1h",
            setup_timeframe=self.signal_timeframe, entry_timeframe=self.signal_timeframe,
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=signal_candle.timestamp,
            reference_price=current_price, entry_zone_low=entry_zone_low, entry_zone_high=entry_zone_high,
            invalidation_price=invalidation, target_1=target_1, target_2=target_2,
            score=score, market_regime=ctx.market_regime, reasons=reasons, state=SetupState.SETUP_READY,
            features=self._base_features(
                anchor_name=anchor_name,
                anchor_ema=anchor_ema,
                pullback_quality=pullback_quality,
                rsi_confirmation=rsi_confirmation,
                risk=risk,
            ),
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        results: list[SetupCandidate] = []
        if ScannerDirection.LONG.value in self.enabled_directions:
            long_setup = self._scan_long(ctx)
            if long_setup:
                results.append(long_setup)
        if ScannerDirection.SHORT.value in self.enabled_directions:
            short_setup = self._scan_short(ctx)
            if short_setup:
                results.append(short_setup)
        return results
