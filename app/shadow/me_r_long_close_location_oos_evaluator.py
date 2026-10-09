"""Outcome evaluator for ME_R_LONG_CLOSE_LOCATION_OOS experiment.

Evaluates MFE/MAE at multiple horizons (15m, 30m, 60m, 120m, 240m)
for both PASS and REJECT signals.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.exchange.bybit_client import BybitClient
from app.shadow.me_r_close_location_historical_source import (
    CANDLE_MS,
    fetch_historical_5m,
)
from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

# Evaluation horizons in minutes
HORIZONS = [15, 30, 60, 120, 240]


def _utc_now() -> datetime:
    """Current UTC time, independently mockable in tests."""
    return datetime.now(timezone.utc)


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
        """Evaluate clean prospective signals only."""
        eligible = self.repo.get_eligible_signals(limit=limit)
        stats = {"checked": 0, "updated": 0, "errors": 0}

        for signal in eligible:
            stats["checked"] += 1
            try:
                saved = self._evaluate_signal(signal)
                if saved is True:
                    stats["updated"] += 1
            except Exception:
                stats["errors"] += 1
                logger.exception(
                    "ME Close Location evaluation failed: signal_id=%s symbol=%s",
                    signal["signal_id"],
                    signal["symbol"],
                )

        return stats

    def _evaluate_signal(self, signal: dict) -> bool:
        """Calculate only fully covered, closed-candle horizons."""
        if signal.get("signal_version") != "1.2.0":
            return False

        decision_time = signal.get("decision_time")
        if not isinstance(decision_time, datetime):
            return False

        if decision_time.tzinfo is None:
            raise ValueError("Naive decision_time")

        signal_price = float(signal["signal_price"])
        if not 0 < signal_price < float("inf"):
            raise ValueError("Invalid signal_price")

        decision_ms = int(decision_time.timestamp() * 1000)
        asof = _utc_now()
        asof_ms = int(asof.timestamp() * 1000)
        risk = signal_price * 0.025

        # Only whole candles beginning at or after the decision.
        first_open = (
            (decision_ms + CANDLE_MS - 1) // CANDLE_MS
        ) * CANDLE_MS

        outcome_data = {}
        evaluated_horizons = []

        for horizon in HORIZONS:
            field = f"evaluated_{horizon}m_at"

            # Never recompute a previously evaluated horizon.
            if signal.get(field) is not None:
                continue

            horizon_end_ms = decision_ms + horizon * 60_000

            if horizon_end_ms > asof_ms:
                continue

            # Last candle must close by horizon_end_ms.
            last_open = (
                (horizon_end_ms - CANDLE_MS) // CANDLE_MS
            ) * CANDLE_MS

            if last_open < first_open:
                continue

            window = fetch_historical_5m(
                self.client,
                signal["symbol"],
                first_open,
                last_open,
                asof_ms=asof_ms,
            )

            highs = [float(c.high) for c in window.candles]
            lows = [float(c.low) for c in window.candles]

            mfe = max(highs) - signal_price
            mae = signal_price - min(lows)

            outcome_data[f"mfe_{horizon}m"] = mfe
            outcome_data[f"mae_{horizon}m"] = mae
            outcome_data[f"mfe_{horizon}m_r"] = mfe / risk
            outcome_data[f"mae_{horizon}m_r"] = mae / risk
            outcome_data[field] = asof
            evaluated_horizons.append(horizon)

        if not evaluated_horizons:
            return False

        if 240 in evaluated_horizons:
            # Hit flags are final-path statistics, not interim values.
            final_mfe_r = outcome_data["mfe_240m_r"]
            outcome_data["hit_0_5r"] = final_mfe_r >= 0.5
            outcome_data["hit_1r"] = final_mfe_r >= 1.0
            outcome_data["hit_1_5r"] = final_mfe_r >= 1.5
            outcome_data["hit_2r"] = final_mfe_r >= 2.0

        return self.repo.save_outcome_partial(
            signal_id=signal["signal_id"],
            symbol=signal["symbol"],
            is_final=(240 in evaluated_horizons),
            **outcome_data,
        )
