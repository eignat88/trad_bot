"""Scanner 04c: Trend Pullback V3 — edge-optimized variant with regime filters.

Key differences from v2:
- RSI filter: RSI > 60 (momentum confirmation, not cooled)
- ADX filter: ADX > 35 (strong trend strength)
- EMA50 slope filter: slope > 0 (trend direction confirmation)
- Hour filter: hour 6-23, excluding hours 10, 15, 19
- Symbol exclusions: ONDOUSDT, BNBUSDT, SOLUSDT
- pullback_tolerance: 0.012 (same as v2)
- target_r: 0.50 (same as v2)
- expire_at_breakeven: True (same as v2)

Edge analysis results (from VPS data enrichment):
  Win Rate:    85.3% (1169/1371)
  Avg R:       +0.378R per trade
  Kelly:       1.12 (half-Kelly = 0.56)
  Hit 0.5R:    94.2%
  Hit 1.0R:    83.9%
  Hit 2.0R:    66.2%

Reference: TREND_PULLBACK_V3_SPEC.md
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState

# Symbols excluded from V3 scanning based on edge analysis
EXCLUDED_SYMBOLS: frozenset[str] = frozenset({
    "ONDOUSDT", "BNBUSDT", "SOLUSDT",
})

# Hours to exclude from scanning (UTC)
EXCLUDED_HOURS: frozenset[int] = frozenset({10, 15, 19})


class TrendPullbackScannerV3:
    name = "TREND_PULLBACK_V3"
    version = "1.0.0"

    def __init__(
        self,
        pullback_tolerance: float = 0.012,      # same as v2
        rsi_threshold: float = 60.0,             # RSI > 60 (momentum confirmation)
        adx_threshold: float = 35.0,             # ADX > 35 (strong trend)
        ema50_slope_min: float = 0.0,            # EMA50 slope > 0 (rising)
        enabled_directions: tuple[str, ...] = ("LONG",),
        allowed_regimes: tuple[str, ...] = ("TREND_UP",),
        signal_timeframe: str = "15m",           # same as v2
        max_pullback_quality: float | None = 0.75,
        target_r: float = 0.50,                  # same as v2
        stop_buffer: float = 0.002,              # same as v2
        hour_start: int = 6,                     # hour filter start (inclusive)
        hour_end: int = 23,                      # hour filter end (inclusive)
        excluded_hours: frozenset[int] | None = None,
        excluded_symbols: frozenset[str] | None = None,
    ) -> None:
        self.pullback_tolerance = pullback_tolerance
        self.rsi_threshold = rsi_threshold
        self.adx_threshold = adx_threshold
        self.ema50_slope_min = ema50_slope_min
        self.enabled_directions = enabled_directions
        self.allowed_regimes = allowed_regimes
        self.signal_timeframe = signal_timeframe
        self.max_pullback_quality = max_pullback_quality
        self.target_r = target_r
        self.stop_buffer = stop_buffer
        self.hour_start = hour_start
        self.hour_end = hour_end
        self.excluded_hours = excluded_hours if excluded_hours is not None else EXCLUDED_HOURS
        self.excluded_symbols = excluded_symbols if excluded_symbols is not None else EXCLUDED_SYMBOLS

    def _is_symbol_excluded(self, symbol: str) -> bool:
        """Check if symbol is in the exclusion list."""
        return symbol in self.excluded_symbols

    def _is_hour_allowed(self, evaluated_at: datetime) -> bool:
        """Check if the current hour is within the allowed trading window."""
        hour = evaluated_at.hour
        if hour < self.hour_start or hour > self.hour_end:
            return False
        if hour in self.excluded_hours:
            return False
        return True

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        # Symbol exclusion check
        if self._is_symbol_excluded(ctx.symbol):
            return None

        # Hour filter check
        if not self._is_hour_allowed(ctx.evaluated_at):
            return None

        candles_1h, candles_15m, candles_5m = list(ctx.candles_1h), list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_1h) < 50 or len(candles_15m) < 30 or len(candles_5m) < 20:
            return None

        ind = ctx.indicators
        if ind.ema20 <= 0 or ind.ema50 <= 0 or ind.ema200 <= 0:
            return None

        # Trend alignment: EMA20 > EMA50 and price above EMA200
        if not (ind.ema20 > ind.ema50 and candles_1h[-1].close > ind.ema200):
            return None

        # ADX filter: ADX > threshold (strong trend)
        if ind.adx < self.adx_threshold:
            return None

        # EMA50 slope filter: slope > 0 (rising trend)
        if ind.ema50_slope <= self.ema50_slope_min:
            return None

        current_price = candles_5m[-1].close

        # Pullback detection: price near EMA20 or EMA50
        near_ema20 = abs(current_price - ind.ema20) / ind.ema20 < self.pullback_tolerance
        near_ema50 = abs(current_price - ind.ema50) / ind.ema50 < self.pullback_tolerance
        if not (near_ema20 or near_ema50):
            return None

        # RSI filter: RSI > threshold (momentum confirmation)
        if ind.rsi < self.rsi_threshold:
            return None

        # Signal candle must be bullish
        if candles_5m[-1].close < candles_5m[-1].open:
            return None

        # Invalidation: low of last 3 candles with buffer
        invalidation = min(c.low for c in candles_5m[-3:]) * (1 - self.stop_buffer)
        atr = ind.atr if ind.atr > 0 else (current_price * 0.015)

        # Compute risk and target
        risk = abs(current_price - invalidation)
        if self.target_r is not None:
            target_1 = current_price + risk * self.target_r
            target_2 = None
        else:
            target_1 = current_price + atr * 2
            target_2 = current_price + atr * 3.5

        # Validate risk geometry
        if risk <= 0 or invalidation >= current_price or target_1 <= current_price:
            return None

        # Pullback quality calculation
        ema_distance = min(abs(current_price - ind.ema20) / ind.ema20,
                           abs(current_price - ind.ema50) / ind.ema50)
        pullback_quality = 1 - min(ema_distance / self.pullback_tolerance, 1)

        # Filter by max_pullback_quality if set
        if self.max_pullback_quality is not None and pullback_quality > self.max_pullback_quality:
            return None

        # RSI confirmation strength
        rsi_confirmation = 1 - min(abs(ind.rsi - 70) / 30, 1)

        # Score based on signal quality
        score = self._score_long(ctx, pullback_quality, ind.rsi, ind.adx, ind.ema50_slope)

        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value, htf_timeframe="1h",
            setup_timeframe=self.signal_timeframe, entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=current_price,
            entry_zone_low=min(ind.ema20, ind.ema50) * 0.998,
            entry_zone_high=max(ind.ema20, ind.ema50) * 1.002,
            invalidation_price=invalidation,
            target_1=target_1, target_2=target_2,
            score=score,
            market_regime=ctx.market_regime,
            reasons=self._build_reasons(pullback_quality, ind.rsi, ind.adx, ind.ema50_slope),
            state=SetupState.SETUP_READY,
            features={
                "htf_context": True,
                "trend_alignment": True,
                "pullback_to_ema": True,
                "pullback_quality": pullback_quality,
                "rsi_momentum": True,
                "rsi_confirmation": rsi_confirmation,
                "adx_trend_strength": True,
                "adx_value": ind.adx,
                "ema50_slope_positive": True,
                "ema50_slope_value": ind.ema50_slope,
                "hour_filter": True,
                "stop_distance_ok": True,
                "target_r": self.target_r,
                "risk_r": risk,
                "signal_timeframe": self.signal_timeframe,
                "recommended_expiry_bars": 144,
                "recommended_expiry_policy": "BREAKEVEN",
            },
        )

    def _scan_short(self, ctx: MarketContext) -> SetupCandidate | None:
        # V3 is LONG-only by default; SHORT support for future use
        return None

    def _score_long(
        self,
        ctx: MarketContext,
        pullback_quality: float,
        rsi: float,
        adx_val: float,
        ema50_slope: float,
    ) -> float:
        """Compute a quality score for the LONG setup (0-100)."""
        score = 0.0

        # Trend alignment (20 pts)
        if ctx.market_regime == "TREND_UP":
            score += 20

        # Pullback quality (20 pts)
        if 0.25 <= pullback_quality <= 0.75:
            score += 20
        elif pullback_quality <= 0.85:
            score += 10

        # RSI momentum (20 pts)
        if rsi >= 70:
            score += 20  # Strong momentum
        elif rsi >= 60:
            score += 15  # Good momentum
        else:
            score += 5   # Below threshold but passed filter

        # ADX strength (20 pts)
        if adx_val >= 50:
            score += 20  # Very strong trend
        elif adx_val >= 40:
            score += 15  # Strong trend
        elif adx_val >= 35:
            score += 10  # Moderate-strong trend

        # EMA50 slope (20 pts)
        if ema50_slope >= 0.5:
            score += 20  # Strong upward slope
        elif ema50_slope >= 0.2:
            score += 15  # Moderate upward slope
        elif ema50_slope > 0:
            score += 10  # Slight upward slope

        return min(score, 100.0)

    def _build_reasons(
        self,
        pullback_quality: float,
        rsi: float,
        adx_val: float,
        ema50_slope: float,
    ) -> tuple[str, ...]:
        """Build explainable reasons for the setup."""
        reasons: list[str] = []

        reasons.append("TREND_UP alignment")
        reasons.append("pullback_to_ema")

        if pullback_quality >= 0.5:
            reasons.append("pullback_quality high")
        else:
            reasons.append("pullback_quality acceptable")

        if rsi >= 70:
            reasons.append("RSI strong momentum")
        elif rsi >= 60:
            reasons.append("RSI momentum confirmed")

        if adx_val >= 50:
            reasons.append("ADX very strong trend")
        elif adx_val >= 40:
            reasons.append("ADX strong trend")
        else:
            reasons.append("ADX trend confirmed")

        if ema50_slope >= 0.5:
            reasons.append("EMA50 slope steep rising")
        elif ema50_slope >= 0.2:
            reasons.append("EMA50 slope rising")
        else:
            reasons.append("EMA50 slope positive")

        reasons.append("hour_filter_passed")
        reasons.append("symbol_not_excluded")

        return tuple(reasons)

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
