"""Outcome evaluator for ME_R_LONG_CLOSE_LOCATION_OOS experiment.

Evaluates MFE/MAE at multiple horizons (15m, 30m, 60m, 120m, 240m)
for both PASS and REJECT signals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.exchange.bybit_client import BybitClient
from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

# Evaluation horizons in minutes
HORIZONS = [15, 30, 60, 120, 240]


class MERLongCLoOosEvaluator:
    """Evaluator for ME_R_LONG_CLOSE_LOCATION_OOS outcomes."""

    def __init__(
        self,
        repo: MERLongCLoOosRepository,
        client: BybitClient,
    ) -> None:
        self.repo = repo
        self.client = client

    def evaluate_pending(self, limit: int = 100) -> dict[str, int]:
        """Evaluate pending signals and return stats.

        Returns dict with:
        - checked: number of eligible signals checked
        - updated: number of signals with at least one horizon updated
        - errors: number of signals that failed evaluation
        """
        eligible = self.repo.get_eligible_signals(limit=limit)
        stats = {"checked": 0, "updated": 0, "errors": 0}

        for signal in eligible:
            stats["checked"] += 1
            try:
                self._evaluate_signal(signal)
                stats["updated"] += 1
            except Exception:
                stats["errors"] += 1
                logger.exception(
                    "Failed to evaluate signal %d for %s",
                    signal["signal_id"], signal["symbol"],
                )

        return stats

    def _evaluate_signal(self, signal: dict) -> None:
        """Evaluate a single signal across all horizons."""
        signal_id = signal["signal_id"]
        symbol = signal["symbol"]
        signal_time = signal["signal_time"]
        signal_price = signal["signal_price"]
        outcome_id = signal["outcome_id"]

        # Fetch candles for evaluation
        candles = self.client.get_klines(symbol, "5", 500)
        if not candles:
            logger.warning("No candles available for %s", symbol)
            return

        # Convert signal_time to milliseconds for comparison
        if isinstance(signal_time, datetime):
            signal_time_ms = int(signal_time.timestamp() * 1000)
        else:
            signal_time_ms = signal_time

        # Calculate risk (distance to invalidation)
        # For LONG: risk = entry - invalidation
        # We'll use a default 2.5% risk for now
        risk = signal_price * 0.025

        # Evaluate each horizon
        outcome_data = {}
        for horizon in HORIZONS:
            horizon_ms = horizon * 60 * 1000
            horizon_time_ms = signal_time_ms + horizon_ms

            # Find the candle at or after horizon time
            horizon_candle = None
            for candle in candles:
                if candle.timestamp >= horizon_time_ms:
                    horizon_candle = candle
                    break

            if horizon_candle is None:
                # Horizon not yet reached
                continue

            # Calculate MFE and MAE from signal_time to horizon_time
            # Get all candles in the interval
            interval_candles = [
                c for c in candles
                if signal_time_ms <= c.timestamp <= horizon_time_ms
            ]

            if not interval_candles:
                continue

            # For LONG: MFE = max high - entry, MAE = entry - min low
            highs = [c.high for c in interval_candles]
            lows = [c.low for c in interval_candles]

            max_high = max(highs)
            min_low = min(lows)

            mfe = max_high - signal_price
            mae = signal_price - min_low

            # Convert to R units
            mfe_r = mfe / risk if risk > 0 else 0
            mae_r = mae / risk if risk > 0 else 0

            # Store horizon data
            outcome_data[f"mfe_{horizon}m"] = mfe
            outcome_data[f"mae_{horizon}m"] = mae
            outcome_data[f"mfe_{horizon}m_r"] = mfe_r
            outcome_data[f"mae_{horizon}m_r"] = mae_r
            outcome_data[f"evaluated_{horizon}m_at"] = datetime.now(timezone.utc)

        # Determine target hits
        hit_0_5r = False
        hit_1r = False
        hit_1_5r = False
        hit_2r = False

        # Check if any horizon reached the targets
        for horizon in HORIZONS:
            mfe_r = outcome_data.get(f"mfe_{horizon}m_r", 0)
            if mfe_r >= 0.5:
                hit_0_5r = True
            if mfe_r >= 1.0:
                hit_1r = True
            if mfe_r >= 1.5:
                hit_1_5r = True
            if mfe_r >= 2.0:
                hit_2r = True

        # Determine if this is the final evaluation (240m horizon)
        is_final = outcome_data.get("evaluated_240m_at") is not None

        # Save outcome
        self.repo.save_outcome_partial(
            signal_id=signal_id,
            symbol=symbol,
            mfe_15m=outcome_data.get("mfe_15m"),
            mae_15m=outcome_data.get("mae_15m"),
            evaluated_15m_at=outcome_data.get("evaluated_15m_at"),
            mfe_30m=outcome_data.get("mfe_30m"),
            mae_30m=outcome_data.get("mae_30m"),
            evaluated_30m_at=outcome_data.get("evaluated_30m_at"),
            mfe_60m=outcome_data.get("mfe_60m"),
            mae_60m=outcome_data.get("mae_60m"),
            evaluated_60m_at=outcome_data.get("evaluated_60m_at"),
            mfe_120m=outcome_data.get("mfe_120m"),
            mae_120m=outcome_data.get("mae_120m"),
            evaluated_120m_at=outcome_data.get("evaluated_120m_at"),
            mfe_240m=outcome_data.get("mfe_240m"),
            mae_240m=outcome_data.get("mae_240m"),
            evaluated_240m_at=outcome_data.get("evaluated_240m_at"),
            mfe_15m_r=outcome_data.get("mfe_15m_r"),
            mae_15m_r=outcome_data.get("mae_15m_r"),
            mfe_30m_r=outcome_data.get("mfe_30m_r"),
            mae_30m_r=outcome_data.get("mae_30m_r"),
            mfe_60m_r=outcome_data.get("mfe_60m_r"),
            mae_60m_r=outcome_data.get("mae_60m_r"),
            mfe_120m_r=outcome_data.get("mfe_120m_r"),
            mae_120m_r=outcome_data.get("mae_120m_r"),
            mfe_240m_r=outcome_data.get("mfe_240m_r"),
            mae_240m_r=outcome_data.get("mae_240m_r"),
            hit_0_5r=hit_0_5r,
            hit_1r=hit_1r,
            hit_1_5r=hit_1_5r,
            hit_2r=hit_2r,
            is_final=is_final,
        )

        logger.debug(
            "Evaluated signal %d for %s: MFE_60m=%.4f MAE_60m=%.4f",
            signal_id, symbol,
            outcome_data.get("mfe_60m", 0),
            outcome_data.get("mae_60m", 0),
        )
