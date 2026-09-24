"""Scanner 07RLV2: Momentum Exhaustion Reverse Long V2.

V2 = V1 + ENTRY_CONFIRM_RSI_RISING

Adds a single entry-confirmation filter: rsi_delta_3 > 0.
This requires RSI(14) to be rising at the moment of setup detection,
meaning the bearish momentum is already decelerating.

All other logic (detection, SL, TP, hold, score, TTL) is identical to V1.
This is an isolated experiment — no other filters are added.

Hypothesis: RSI turning up confirms that bearish exhaustion is real,
not a continuation pause.  OOS validation on 115,759 backtest setups
showed consistent improvement across all 12 months, all regimes,
and all time splits (DEV/VALID/OOS).

Parameters (fixed, same as V1):
  - Direction: LONG
  - SL: -2.5% from entry
  - TP: +3.0% from entry
  - Max hold: 240 minutes
  - DCA: OFF
  - Trailing: OFF
  - Breakeven: OFF

Added filter:
  - rsi_delta_3 > 0  (RSI(14) current closed 5m bar minus RSI(14) 3 bars ago)
"""
from __future__ import annotations
from datetime import datetime, timezone
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs
from app.indicators.technical import rsi_wilder
from app.scanners.funnel_diagnostics import get_funnel_collector


class MomentumExhaustionReverseLongV2Scanner:
    """MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 scanner.

    Identical to V1 except for one entry-confirmation filter:
    rsi_delta_3 > 0 (RSI must be rising at detection time).
    """

    name = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
    version = "1.0.0"

    def __init__(
        self,
        swing_lookback: int = 5,
        exhaustion_threshold: float = 0.003,
        rsi_period: int = 14,
        rsi_delta_lookback: int = 3,
    ) -> None:
        self.swing_lookback = swing_lookback
        self.exhaustion_threshold = exhaustion_threshold
        self.rsi_period = rsi_period
        self.rsi_delta_lookback = rsi_delta_lookback

    def _compute_rsi_delta_3(self, candles_5m: list) -> float | None:
        """Compute rsi_delta_3 from closed 5m candles only.

        rsi_delta_3 = RSI(14) on current closed bar - RSI(14) 3 bars ago.

        Uses only fully closed candles known at detected_at.
        No look-ahead: candles_5m[-1] is the last CLOSED candle.
        """
        closes = [c.close for c in candles_5m]
        # Need enough closes for RSI(14) + 3 bars of history
        min_closes = self.rsi_period + 1 + self.rsi_delta_lookback
        if len(closes) < min_closes:
            return None

        rsi_now = rsi_wilder(closes, self.rsi_period)
        rsi_prev = rsi_wilder(closes[:-self.rsi_delta_lookback], self.rsi_period)
        return rsi_now - rsi_prev

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
        rsi_delta_3: float,
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
            # V2-specific: RSI rising confirmation
            "rsi_14": round(rsi, 4),
            "rsi_14_3bars_ago": round(rsi - rsi_delta_3, 4),
            "rsi_delta_3": round(rsi_delta_3, 4),
            "rsi_period": self.rsi_period,
            "rsi_timeframe": "5m",
            "entry_confirmation_rsi_rising": True,
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
        """Bearish exhaustion detected -> reversed direction: LONG.

        Identical to V1 _scan_long, with one addition:
        rsi_delta_3 > 0 check before creating the setup.

        -- SIGNAL_FUNNEL_DIAGNOSTICS_V1 (shadow-only) --
        Every gate incrementally counts PASS/REJECT for observability.
        No production behavior is changed.
        """
        funnel = get_funnel_collector(self.name)
        funnel.increment("TOTAL_SCANS")

        candles_15m, candles_5m = list(ctx.candles_15m), list(ctx.candles_5m)
        if len(candles_15m) < 30 or len(candles_5m) < 15:
            funnel.increment("NO_DATA")
            return None
        funnel.increment("PASS_DATA_LENGTH")

        swing_highs = find_swing_highs(candles_15m, self.swing_lookback)
        if len(swing_highs) < 2:
            funnel.increment("NO_SWINGS")
            return None
        funnel.increment("PASS_SWING_HIGHS")
        prev_high = swing_highs[-2].price

        recent_high = max(c.high for c in candles_5m[-5:])
        # Price broke above previous swing high
        if recent_high <= prev_high:
            funnel.increment("NO_BREAKOUT")
            return None
        funnel.increment("PASS_BREAK_PREV_HIGH")

        current_price = candles_5m[-1].close
        # Price came back near the previous high (not too far above)
        if current_price > prev_high * (1 + self.exhaustion_threshold):
            funnel.increment("TOO_FAR_ABOVE_PREV_HIGH")
            return None
        funnel.increment("PASS_RETURN_NEAR_HIGH")

        last = candles_5m[-1]
        # Bearish candle (exhaustion signal)
        if last.close > last.open:
            funnel.increment("NOT_BEARISH")
            return None
        funnel.increment("PASS_BEARISH_CANDLE")
        candle_range = last.high - last.low
        body = abs(last.close - last.open)
        if candle_range > 0 and body / candle_range > 0.7:
            funnel.increment("BODY_TOO_LARGE")
            return None
        funnel.increment("PASS_BODY_RATIO")

        # RSI overbought confirmation
        if ctx.indicators.rsi < 65:
            funnel.increment("RSI_BELOW_65")
            return None
        funnel.increment("PASS_RSI_65")

        # -- V2 ADDITION: RSI rising confirmation --
        rsi_delta_3 = self._compute_rsi_delta_3(candles_5m)
        if rsi_delta_3 is None:
            funnel.increment("RSI_DELTA_MISSING")
            return None
        funnel.increment("PASS_RSI_DELTA_AVAILABLE")
        if rsi_delta_3 <= 0:
            funnel.increment("RSI_DELTA_NOT_POSITIVE")
            return None
        funnel.increment("PASS_RSI_DELTA_POSITIVE")

        atr = ctx.indicators.atr if ctx.indicators.atr > 0 else (current_price * 0.015)

        # -- Reversed direction: LONG --
        sl_pct = 0.025  # 2.5%
        tp_pct = 0.03   # 3.0%

        invalidation = current_price * (1 - sl_pct)
        target_1 = current_price * (1 + tp_pct)

        source_scanner = "MOMENTUM_EXHAUSTION"
        source_direction = "SHORT"

        features = self._build_long_features(
            candles_5m, prev_high, recent_high,
            current_price, invalidation, target_1, atr, ctx.indicators.rsi,
            rsi_delta_3,
        )

        features["stop_loss_pct"] = sl_pct * 100  # 2.5%
        features["take_profit_pct"] = tp_pct * 100  # 3.0%
        features["hold_minutes"] = 240
        features["source_scanner"] = source_scanner
        features["source_direction"] = source_direction

        funnel.increment("FINAL_SETUP")

        return SetupCandidate(
            scanner_name=self.name, scanner_version=self.version, symbol=ctx.symbol,
            direction=ScannerDirection.LONG.value,
            htf_timeframe="1h", setup_timeframe="15m", entry_timeframe="5m",
            detected_at=ctx.evaluated_at, setup_started_at=datetime.now(timezone.utc),
            reference_price=recent_high,
            entry_zone_low=current_price * 0.998, entry_zone_high=current_price * 1.001,
            invalidation_price=invalidation,
            target_1=target_1, target_2=None,
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
