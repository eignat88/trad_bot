"""Scanner: ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1.

Out-of-sample validation scanner for the close_location >= 0.70 adverse
filter on MOMENTUM_EXHAUSTION_REVERSE_LONG_V1.

Design principle: MAXIMALLY IDENTICAL to frozen MOMENTUM_EXHAUSTION_REVERSE_LONG_V1
with one additional post-setup filter.

Logic flow:
    1. Run MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 base detection (frozen)
    2. If base setup found → compute close_location from signal candle
    3. If close_location >= 0.70 → PASS → emit signal (TREATMENT)
    4. If close_location <  0.70 → REJECT → log as rejected candidate
    5. Every base setup (pass or reject) → log as CONTROL shadow

The old scanner MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 is BLOCKED for new
entries.  This scanner is the only active entry point for ME_R_LONG setups.

OOS_START is set at deployment time.  All data collected after that point
is OUT_OF_SAMPLE.  Threshold 0.70 is FROZEN — do not adapt.

Parameters (inherited frozen from V1):
  - Direction: LONG
  - SL: -2.5% from entry
  - TP: +3.0% from entry
  - Max hold: 240 minutes
  - DCA: OFF
  - Trailing: OFF
  - Breakeven: OFF

Additional filter:
  - close_location >= 0.70 (from signal candle: candles_5m[-1])

close_location formula:
    candle_range = high - low
    close_location = (close - low) / candle_range  if candle_range > 0
    close_location = None                           if candle_range == 0

Signal candle sourcing:
    close_location_source_timestamp = candles_5m[-1].timestamp
    decision_timestamp = ctx.evaluated_at
    GUARANTEE: close_location_source_timestamp <= decision_timestamp
    (candles_5m[-1] is fully closed before evaluation)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.scanners.close_location import (
    CLOSE_LOCATION_THRESHOLD,
    close_location_passes,
)
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.scanners.momentum_exhaustion_reverse_long_v1 import MomentumExhaustionReverseLongV1Scanner

logger = logging.getLogger(__name__)

REJECTION_REASON = "CLOSE_LOCATION_LT_070"
OOS_EXPERIMENT_ID = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class MERLongCloseLocationOOSValidationV1Scanner:
    """ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 scanner.

    Delegates to frozen MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 for base setup
    detection, then applies close_location >= 0.70 as the single additional
    adverse filter.

    Every base setup is logged with close_location value and filter result
    for both CONTROL and TREATMENT cohort tracking.
    """

    name = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
    version = "1.0.0"

    def __init__(
        self,
        base_scanner: MomentumExhaustionReverseLongV1Scanner | None = None,
        threshold: float = CLOSE_LOCATION_THRESHOLD,
    ) -> None:
        self._base_scanner = base_scanner or MomentumExhaustionReverseLongV1Scanner()
        self.threshold = threshold

    def _compute_close_location_from_signal_candle(
        self, candles_5m: list,
    ) -> tuple[float | None, datetime | None]:
        """Extract close_location from the signal candle.

        The signal candle is candles_5m[-1] — the last fully closed 5m candle
        at the time of evaluation.  This is the same candle that the base
        scanner uses for its detection logic (bearish candle check, body ratio, etc).

        Returns
        -------
        tuple[float | None, datetime | None]
            (close_location_value, signal_candle_timestamp)
            close_location is None for zero-range candles.
            signal_candle_timestamp is used for leakage audit.
        """
        if not candles_5m:
            return None, None

        signal_candle = candles_5m[-1]
        candle_high = signal_candle.high
        candle_low = signal_candle.low
        candle_close = signal_candle.close

        candle_range = candle_high - candle_low
        if candle_range <= 0:
            return None, signal_candle.timestamp

        close_location = (candle_close - candle_low) / candle_range
        return close_location, signal_candle.timestamp

    def _attach_oos_features(
        self,
        candidate: SetupCandidate,
        close_location: float | None,
        filter_passed: bool,
        signal_candle_timestamp: datetime | None,
    ) -> SetupCandidate:
        """Attach OOS experiment features to a SetupCandidate.

        Adds close_location metadata to the candidate's features dict
        for downstream tracking, analytics, and Grafana visibility.
        """
        features = dict(candidate.features)
        features["oos_experiment_id"] = OOS_EXPERIMENT_ID
        features["close_location"] = round(close_location, 6) if close_location is not None else None
        features["close_location_threshold"] = self.threshold
        features["close_location_passed"] = filter_passed
        features["close_location_source_timestamp"] = (
            signal_candle_timestamp.isoformat() if signal_candle_timestamp else None
        )
        features["close_location_decision_timestamp"] = candidate.detected_at.isoformat()
        # Research features (observation only, NOT used as gate)
        # body_ratio is already in features from the base scanner

        from dataclasses import replace
        return replace(candidate, features=features)

    def scan(self, ctx: MarketContext) -> list[SetupCandidate]:
        """Scan for ME_R_LONG base setup + close_location filter.

        Flow:
            1. Delegate to frozen V1 base scanner
            2. If no base setup → return empty
            3. Compute close_location from signal candle
            4. If close_location >= threshold → emit treatment signal
            5. If close_location < threshold → log rejection, return empty

        Control/shadow cohort logging is handled by the orchestrator
        or analytics layer — every base setup produces a control record
        regardless of filter outcome.
        """
        # Step 1: Run frozen base detection
        base_candidates = self._base_scanner.scan(ctx)
        if not base_candidates:
            return []

        results: list[SetupCandidate] = []

        for candidate in base_candidates:
            # Step 2: Compute close_location from signal candle
            # Signal candle is the same candle_5m[-1] used by the base scanner
            close_location, signal_ts = self._compute_close_location_from_signal_candle(
                list(ctx.candles_5m)
            )

            # Leakage audit guarantee:
            # signal_ts = candles_5m[-1].timestamp  (closed candle)
            # decision_ts = candidate.detected_at = ctx.evaluated_at
            # Since ctx.candles_5m[-1] is fully closed before ctx.evaluated_at:
            #   signal_ts <= decision_ts  ✓

            # Step 3: Evaluate close_location filter
            if close_location is not None:
                filter_passed = close_location >= self.threshold
            else:
                # Zero-range candle: cannot evaluate → reject
                filter_passed = False

            # Step 4: Attach OOS features for tracking
            enriched = self._attach_oos_features(
                candidate, close_location, filter_passed, signal_ts,
            )

            if filter_passed:
                # TREATMENT: emit signal for paper/live entry
                logger.info(
                    "ME_R_LONG OOS candidate %s close_location=%.4f threshold=%.2f PASS",
                    ctx.symbol,
                    close_location if close_location is not None else 0.0,
                    self.threshold,
                )
                results.append(enriched)
            else:
                # REJECTED: log but do not emit
                logger.info(
                    "ME_R_LONG OOS candidate %s close_location=%s threshold=%.2f REJECT reason=%s",
                    ctx.symbol,
                    f"{close_location:.4f}" if close_location is not None else "None",
                    self.threshold,
                    REJECTION_REASON,
                )

        return results
