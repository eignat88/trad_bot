"""Scanner 07RLV1: Momentum Exhaustion Reverse Long V1.

This scanner reuses the MOMENTUM_EXHAUSTION detection logic but trades in the
opposite direction. When MOMENTUM_EXHAUSTION detects a SHORT signal (bearish
exhaustion), this scanner creates a LONG setup.

Hypothesis: MOMENTUM_EXHAUSTION correctly identifies exhaustion points, but
the expected price direction is systematically inverted:
  - When MOMENTUM_EXHAUSTION forms SHORT (bearish exhaustion at swing high),
    price tends to continue UP → reversed LONG.

Detection logic is identical to MOMENTUM_EXHAUSTION SHORT; only the trade
direction and entry/invalidation/targets are adapted for LONG positions.

Parameters (fixed for V1):
  - Direction: LONG
  - SL: -2.5% from entry
  - TP: +3.0% from entry
  - Max hold: 240 minutes
  - DCA: OFF
  - Trailing: OFF
  - Breakeven: OFF
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs


class MomentumExhaustionReverseLongV1Scanner:
    """MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 scanner.

    Reuses MOMENTUM_EXHAUSTION SHORT detection logic but creates LONG setups.
    The scanner does not duplicate detection logic - it delegates to the original
    scanner's detection and adapts the output for LONG direction.
    """

    name = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
    version = "1.0.0"

    def __init__(self, swing_lookback: int = 5, exhaustion_threshold: float = 0.003) -> None:
        self.swing_lookback = swing_lookback
        self.exhaustion_threshold = exhaustion_threshold

    def _build_long_features(
        self,
        candles_5m: list,
        prev_high: float,
        recent_high: float,
        entry: float,
        invalidation: float,
        target_1: float,
        atr: float,
        rsi: float,
    ) -> dict[str, object]:
        """Features for the reversed LONG (original bearish exhaustion → LONG)."""
        exhaustion_magnitude = self._exhaustion_magnitude(recent_high, prev_high)
        body_ratio = self._body_ratio(candles_5m[-1])

        # RSI confirmation: how deep into overbought (65-80 → 0-1)
        rsi_confirmation = max(0.0, min((rsi - 65) / 15, 1.0))

        avg_vol = sum(c.volume for c in candles_5m[-10:]) / min(len(candles_5m), 10)
        volume_ratio = min(candles_5m[-1].volume / avg_vol / 2, 1.0) if avg_vol > 0 else 0.0

        risk = abs(entry - invalidation)
        rr_raw = abs(target_1 - entry) / risk if risk > 0 else 0.0
        rr_ratio = min(rr_raw / 3.0, 1.0)

        stop_atr = risk / atr if atr > 0 else 2.0
        stop_distance_atr = max(0.0, 1.0 - min(stop_atr / 2.0, 1.0))

        return {
            "exhaustion_magnitude": exhaustion_magnitude,
            "body_ratio": body_ratio,
            "rsi_confirmation": rsi_confirmation,
            "volume_ratio": volume_ratio,
            "rr_ratio": rr_ratio,
            "stop_distance_atr": stop_distance_atr,
        }

    @staticmethod
    def _body_ratio(candle) -> float:
        """Body / range ratio (inverted: smaller body = better exhaustion signal)."""
        candle_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        if candle_range <= 0:
            return 0.0
        return 1.0 - min(body / candle_range, 1.0)

    @staticmethod
    def _exhaustion_magnitude(recent_extreme: float, prior_level: float) -> float:
        """How far past the prior swing level, normalised to [0, 1]."""
        if prior_level <= 0:
            return 0.0
        overshoot = abs(recent_extreme - prior_level) / prior_level
        return min(overshoot / 0.02, 1.0)

    def _scan_long(self, ctx: MarketContext) -> SetupCandidate | None:
        """Bearish exhaustion detected → reversed direction: LONG.

        Detection is identical to MOMENTUM_EXHAUSTION _scan_short, but we go LONG
        because the hypothesis says price continues UP after this setup.
        """
        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            return None

        swing_highs = find_swing_highs(candles_15m, self.swing_lookback)
        if len(swing_highs) < 2:
            return None
        prev_high = swing_highs[-2].price

        recent_high = max(c.high for c in candles_5m[-5:])
        # Price broke above previous swing high
        if recent_high <= prev_high:
            return None

        current_price = candles_5m[-1].close
        # Price came back near the previous high (not too far above)
        if current_price > prev_high * (1 + self.exhaustion_threshold):
            return None

        last = candles_5m[-1]
        # Bearish candle (exhaustion signal)
        if last.close > last.open:
            return None
        candle_range = last.high - last.low
        body = abs(last.close - last.open)
        if candle_range > 0 and body / candle_range > 0.7:
            return None

        # RSI overbought confirmation
        if ctx.indicators.rsi < 65:
            return None

        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (current_price * 0.015)

        # ── Reversed direction: LONG ──
        # Fixed SL: -2.5% from entry
        # Fixed TP: +3.0% from entry
        # Max hold: 240 minutes

        # Calculate fixed SL and TP based on V1 parameters.
        # invalidation_price IS the effective stop — paper engine reads it
        # as stop_price.  Earlier revisions computed a swing-based
        # invalidation that overwrote the designed 2.5 % stop; this was
        # the root cause of premature STOP_LOSS exits (e.g. trade 303).
        sl_pct = 0.025  # 2.5%
        tp_pct = 0.03   # 3.0%

        invalidation = current_price * (1 - sl_pct)
        target_1 = current_price * (1 + tp_pct)

        # For lineage tracking: this setup came from MOMENTUM_EXHAUSTION SHORT detection
        source_scanner = "MOMENTUM_EXHAUSTION"
        source_direction = "SHORT"

        features = self._build_long_features(
            candles_5m, prev_high, recent_high,
            current_price, invalidation, target_1, atr, ctx.indicators.rsi,
        )

        # Add V1-specific parameters to features
        features["stop_loss_pct"] = sl_pct * 100  # 2.5%
        features["take_profit_pct"] = tp_pct * 100  # 3.0%
        features["hold_minutes"] = 240
        features["source_scanner"] = source_scanner
        features["source_direction"] = source_direction

        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value,
            htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_high,
            entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
            invalidation_price=invalidation,
            target_1=target_1, target_2=None,  # No TP2 for V1
            market_regime=ctx.market_regime, state=SetupState.SETUP_READY,
            features=features,
        )

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        """Scan for MOMENTUM_EXHAUSTION SHORT condition and create LONG setup."""
        results: list[SetupCandidate] = []
        long_setup = self._scan_long(ctx)
        if long_setup:
            results.append(long_setup)
        return results