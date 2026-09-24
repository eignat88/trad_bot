"""ATR Wick Rejection Short Scanner (Shadow Experiment V1).

This scanner detects SHORT signals based on:
- Upper wick rejection (price spiked up but closed lower)
- ATR-based wick size measurement
- RSI/StochRSI confirmation
- Bollinger Band proximity
- EMA slope confirmation
- Volume ratio confirmation

IMPORTANT: This is a SHADOW scanner — it does NOT open paper/live positions.
It only collects signals for experimental analysis.

Architecture:
- detect_raw_candidate(): Core detection (wick only) — no filtering
- detect_signal(): Full detection with ALL filters (strict mode)
- All feature values stored regardless of filtering

Author: MiMo-v2.5
Date: 2026-09-23
Experiment: ATR_WICK_REJECTION_SHORT_V1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.indicators import (
    atr_wilder,
    bollinger_bands,
    ema,
    ema_slope,
    rsi_wilder,
    volume_ratio as calc_volume_ratio,
)
from app.models import Candle
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


# --- Configuration Constants ---
SCANNER_NAME = "ATR_WICK_REJECTION_SHORT_V1"
SCANNER_VERSION = "1.0.0"

# Wick rejection thresholds (CORE DETECTION — only wick size matters)
WICK_ATR_THRESHOLD = 1.5  # Upper wick must be ≥1.5x ATR

# Feature filter thresholds (OPTIONAL — for strict mode only)
CLOSE_LOCATION_THRESHOLD = 0.30  # Close must be in lower 30% of range
RSI_OVERBOUGHT = 60.0  # RSI ≥60 for SHORT signal
BB_UPPER_PROXIMITY = 0.02  # Price within 2% of upper BB
EMA_SLOPE_THRESHOLD = -0.0001  # Slope must be negative
VOLUME_RATIO_THRESHOLD = 1.2  # Volume must be ≥1.2x average

# ATR period
ATR_PERIOD = 14

# RSI period
RSI_PERIOD = 14

# EMA periods
EMA_FAST = 20
EMA_MEDIUM = 50
EMA_SLOW = 200


@dataclass(frozen=True)
class WickRejectionSignal:
    """Detected wick rejection signal with all feature values."""
    symbol: str
    signal_time: datetime
    signal_price: float
    # Candle data
    open: float
    high: float
    low: float
    close: float
    volume: float
    # ATR indicators
    atr: float
    atr_pct: float
    # Wick analysis
    wick_size: float
    wick_atr: float
    upper_wick_pct: float
    close_location: float
    # RSI/StochRSI
    rsi: float
    stoch_rsi: float | None
    # Bollinger Bands
    bb_upper: float
    bb_mid: float
    bb_lower: float
    bb_width: float
    distance_to_upper_bb: float
    # EMA
    ema_fast: float
    ema_medium: float
    ema_slow: float
    ema_slope: float
    # Volume
    volume_ratio: float
    # Filter results (for diagnostic)
    strict_pass: bool = False
    # OOS filter flags
    oos_a_stoch_08: bool = False
    oos_b_stoch_08_vol_10: bool = False
    oos_c_stoch_06_vol_10: bool = False
    # Metadata
    signal_version: str = SCANNER_VERSION


class AtrWickRejectionShortScanner:
    """Detects SHORT signals based on ATR wick rejection pattern.

    This scanner is designed for shadow/counterfactual analysis only.
    It does NOT integrate with paper trading or live trading systems.

    Architecture:
    - detect_raw_candidate(): Core detection (wick only) — no filtering
    - detect_signal(): Full detection with ALL filters (strict mode)
    - All feature values stored regardless of filtering
    """

    def __init__(
        self,
        wick_atr_threshold: float = WICK_ATR_THRESHOLD,
        close_location_threshold: float = CLOSE_LOCATION_THRESHOLD,
        rsi_overbought: float = RSI_OVERBOUGHT,
        bb_upper_proximity: float = BB_UPPER_PROXIMITY,
        ema_slope_threshold: float = EMA_SLOPE_THRESHOLD,
        volume_ratio_threshold: float = VOLUME_RATIO_THRESHOLD,
    ) -> None:
        self.wick_atr_threshold = wick_atr_threshold
        self.close_location_threshold = close_location_threshold
        self.rsi_overbought = rsi_overbought
        self.bb_upper_proximity = bb_upper_proximity
        self.ema_slope_threshold = ema_slope_threshold
        self.volume_ratio_threshold = volume_ratio_threshold

    def _calculate_upper_wick(self, candle: Candle) -> float:
        """Calculate upper wick size (high - max(open, close))."""
        body_top = max(candle.open, candle.close)
        return candle.high - body_top

    def _calculate_close_location(self, candle: Candle) -> float:
        """Calculate where close is within the candle range [0, 1].

        0.0 = close at low (bearish)
        1.0 = close at high (bullish)
        """
        candle_range = candle.high - candle.low
        if candle_range <= 0:
            return 0.5  # Default to middle for zero-range candles
        return (candle.close - candle.low) / candle_range

    def _calculate_stoch_rsi(
        self, closes: list[float], rsi_period: int = 14, stoch_period: int = 14
    ) -> float | None:
        """Calculate Stochastic RSI.

        StochRSI = (RSI - min(RSI, N)) / (max(RSI, N) - min(RSI, N))

        Returns None if insufficient data.
        """
        if len(closes) < rsi_period + stoch_period + 1:
            return None

        # Calculate RSI values
        rsi_values: list[float] = []
        for i in range(rsi_period + 1, len(closes) + 1):
            try:
                rsi_val = rsi_wilder(closes[:i], rsi_period)
                rsi_values.append(rsi_val)
            except (ValueError, IndexError):
                continue

        if len(rsi_values) < stoch_period:
            return None

        # Calculate Stochastic RSI
        recent_rsi = rsi_values[-stoch_period:]
        min_rsi = min(recent_rsi)
        max_rsi = max(recent_rsi)

        if max_rsi - min_rsi <= 0:
            return 0.5  # Default to middle when no range

        current_rsi = rsi_values[-1]
        return (current_rsi - min_rsi) / (max_rsi - min_rsi)

    def detect_raw_candidate(self, ctx: MarketContext) -> WickRejectionSignal | None:
        """Detect raw SHORT candidate — NO filters applied.

        This is the RAWEST detection — the ONLY condition is:
        - upper_wick > 0 (any candle with some upper wick)

        All feature values (wick_atr, close_location, RSI, BB, EMA, volume)
        are computed and stored, but NONE of them are used as filters.

        Returns WickRejectionSignal for any candle with upper_wick > 0,
        None only when:
        - insufficient data (< 50 candles)
        - ATR calculation fails
        - upper_wick <= 0 (pure bullish candle)
        """
        candles_5m = list(ctx.candles_5m)
        if len(candles_5m) < 50:  # Need enough data for indicators
            return None

        last_candle = candles_5m[-1]
        closes = [c.close for c in candles_5m]

        # --- Calculate indicators ---
        try:
            atr = atr_wilder(candles_5m, ATR_PERIOD)
        except ValueError:
            return None

        atr_pct = atr / last_candle.close if last_candle.close > 0 else 0

        rsi = rsi_wilder(closes, RSI_PERIOD)

        stoch_rsi = self._calculate_stoch_rsi(closes, RSI_PERIOD)

        # Bollinger Bands
        bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20)
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
        distance_to_upper_bb = (bb_upper - last_candle.close) / last_candle.close if last_candle.close > 0 else 0

        # EMA
        ema_fast = ema(closes, EMA_FAST)
        ema_medium = ema(closes, EMA_MEDIUM)
        ema_slow = ema(closes, EMA_SLOW) if len(closes) >= EMA_SLOW else ema_medium
        ema_slope_val = ema_slope(closes, EMA_MEDIUM, lookback=5) if len(closes) >= EMA_MEDIUM + 5 else 0

        # Volume ratio
        volumes = [c.volume for c in candles_5m]
        vol_ratio = calc_volume_ratio(volumes, 20) if len(volumes) > 20 else 1.0

        # --- Wick analysis ---
        upper_wick = self._calculate_upper_wick(last_candle)
        wick_atr_ratio = upper_wick / atr if atr > 0 else 0
        upper_wick_pct = upper_wick / last_candle.close if last_candle.close > 0 else 0
        close_location = self._calculate_close_location(last_candle)

        # --- RAW DETECTION: Only check upper_wick > 0 ---
        if upper_wick <= 0:
            return None

        # --- Return raw candidate with ALL features (no filtering) ---
        return WickRejectionSignal(
            symbol=ctx.symbol,
            signal_time=ctx.evaluated_at,
            signal_price=last_candle.close,
            open=last_candle.open,
            high=last_candle.high,
            low=last_candle.low,
            close=last_candle.close,
            volume=last_candle.volume,
            atr=atr,
            atr_pct=atr_pct,
            wick_size=upper_wick,
            wick_atr=wick_atr_ratio,
            upper_wick_pct=upper_wick_pct,
            close_location=close_location,
            rsi=rsi,
            stoch_rsi=stoch_rsi,
            bb_upper=bb_upper,
            bb_mid=bb_mid,
            bb_lower=bb_lower,
            bb_width=bb_width,
            distance_to_upper_bb=distance_to_upper_bb,
            ema_fast=ema_fast,
            ema_medium=ema_medium,
            ema_slow=ema_slow,
            ema_slope=ema_slope_val,
            volume_ratio=vol_ratio,
            strict_pass=False,
            oos_a_stoch_08=stoch_rsi is not None and stoch_rsi >= 0.8,
            oos_b_stoch_08_vol_10=(stoch_rsi is not None and stoch_rsi >= 0.8 and vol_ratio <= 1.0),
            oos_c_stoch_06_vol_10=(stoch_rsi is not None and stoch_rsi >= 0.6 and vol_ratio <= 1.0),
            signal_version=SCANNER_VERSION,
        )

    def passes_strict_filters(self, raw: WickRejectionSignal) -> bool:
        """Check whether a raw candidate passes all strict filters.

        Same conditions as detect_signal(), but takes an existing
        WickRejectionSignal and returns only the boolean result.
        Centralises the strict predicate so it's defined in one place.
        """
        return (
            raw.rsi >= self.rsi_overbought
            and raw.distance_to_upper_bb <= self.bb_upper_proximity
            and raw.ema_slope <= self.ema_slope_threshold
            and raw.volume_ratio >= self.volume_ratio_threshold
        )

    def detect_signal(self, ctx: MarketContext) -> WickRejectionSignal | None:
        """Detect ATR wick rejection SHORT signal with ALL filters.

        STRICT mode: delegates to passes_strict_filters().
        Returns WickRejectionSignal with strict_pass=True if all
        conditions are met, None otherwise.
        """
        raw = self.detect_raw_candidate(ctx)
        if raw is None:
            return None

        if not self.passes_strict_filters(raw):
            return None

        return WickRejectionSignal(
            symbol=raw.symbol,
            signal_time=raw.signal_time,
            signal_price=raw.signal_price,
            open=raw.open, high=raw.high, low=raw.low,
            close=raw.close, volume=raw.volume,
            atr=raw.atr, atr_pct=raw.atr_pct,
            wick_size=raw.wick_size, wick_atr=raw.wick_atr,
            upper_wick_pct=raw.upper_wick_pct,
            close_location=raw.close_location,
            rsi=raw.rsi, stoch_rsi=raw.stoch_rsi,
            bb_upper=raw.bb_upper, bb_mid=raw.bb_mid,
            bb_lower=raw.bb_lower, bb_width=raw.bb_width,
            distance_to_upper_bb=raw.distance_to_upper_bb,
            ema_fast=raw.ema_fast, ema_medium=raw.ema_medium,
            ema_slow=raw.ema_slow, ema_slope=raw.ema_slope,
            volume_ratio=raw.volume_ratio,
            strict_pass=True,
            signal_version=raw.signal_version,
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        """Compatibility method with MarketScanner protocol.

        Returns SetupCandidate for integration with existing scanner infrastructure.
        """
        signal = self.detect_signal(ctx)
        if signal is None:
            return []

        # Create SetupCandidate for compatibility
        return [SetupCandidate(
            scanner_name=SCANNER_NAME,
            scanner_version=SCANNER_VERSION,
            symbol=ctx.symbol,
            direction=ScannerDirection.SHORT.value,
            htf_timeframe="1h",
            setup_timeframe="5m",
            entry_timeframe="5m",
            detected_at=ctx.evaluated_at,
            setup_started_at=datetime.now(timezone.utc),
            signal_candle_open_time=ctx.candles_5m[-1].timestamp,
            reference_price=signal.signal_price,
            entry_zone_low=signal.signal_price * 0.999,
            entry_zone_high=signal.signal_price * 1.001,
            invalidation_price=signal.high * 1.002,
            target_1=signal.close - signal.atr * 2,
            target_2=signal.close - signal.atr * 3,
            score=self._calculate_score(signal),
            market_regime=ctx.market_regime,
            state=SetupState.SETUP_READY,
            features=self._signal_to_features(signal),
        )]

    def _calculate_score(self, signal: WickRejectionSignal) -> float:
        """Calculate signal score based on feature quality.

        Score range: 0-100
        """
        score = 0.0

        # Wick quality (0-30 points)
        wick_score = min(signal.wick_atr / 2.0, 1.0) * 30
        score += wick_score

        # Close location (0-20 points) — lower close = higher score
        close_score = (1.0 - signal.close_location) * 20
        score += close_score

        # RSI confirmation (0-15 points)
        rsi_score = min((signal.rsi - self.rsi_overbought) / 20, 1.0) * 15
        score += max(0, rsi_score)

        # BB proximity (0-15 points)
        bb_score = max(0, (self.bb_upper_proximity - signal.distance_to_upper_bb) / self.bb_upper_proximity) * 15
        score += bb_score

        # Volume confirmation (0-10 points)
        vol_score = min((signal.volume_ratio - 1.0) / 1.0, 1.0) * 10
        score += max(0, vol_score)

        # EMA slope (0-10 points)
        slope_score = min(abs(signal.ema_slope) / 0.001, 1.0) * 10
        score += slope_score

        return round(min(score, 100.0), 2)

    def _signal_to_features(self, signal: WickRejectionSignal) -> dict[str, Any]:
        """Convert signal to features dict for storage."""
        return {
            "atr": signal.atr,
            "atr_pct": signal.atr_pct,
            "wick_size": signal.wick_size,
            "wick_atr": signal.wick_atr,
            "upper_wick_pct": signal.upper_wick_pct,
            "close_location": signal.close_location,
            "rsi": signal.rsi,
            "stoch_rsi": signal.stoch_rsi,
            "bb_upper": signal.bb_upper,
            "bb_mid": signal.bb_mid,
            "bb_lower": signal.bb_lower,
            "bb_width": signal.bb_width,
            "distance_to_upper_bb": signal.distance_to_upper_bb,
            "ema_fast": signal.ema_fast,
            "ema_medium": signal.ema_medium,
            "ema_slow": signal.ema_slow,
            "ema_slope": signal.ema_slope,
            "volume_ratio": signal.volume_ratio,
            "strict_pass": signal.strict_pass,
            "signal_version": signal.signal_version,
        }