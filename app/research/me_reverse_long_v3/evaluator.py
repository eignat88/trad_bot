"""Independent ME Reverse Long V3 execution evaluator.

Explicit invocation only. Does not start background services.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .candle_adapter import build_v3_window
from .historical_source import fetch_v3_execution_candles
from .execution import evaluate_v3_execution
from .entry_policy import (
    next_executable_entry_at,
    execution_geometry,
)
from .protocol import EXPERIMENT_ID, MAX_HOLD_MINUTES, V3Geometry
from .repository import (
    MEReverseLongV3OutcomeRepository,
    V3OutcomeRecord,
)

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return value.astimezone(timezone.utc)


def _features(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        data = json.loads(value)
        if isinstance(data, dict):
            return data
    raise ValueError("INVALID_FEATURES")


class MEReverseLongV3ExecutionEvaluator:
    """Process V3-only observations with a dedicated outcome repository."""

    def __init__(self, conn: Any, candle_source: Any):
        self.conn = conn
        self.candle_source = candle_source
        self.repository = MEReverseLongV3OutcomeRepository(conn)

    def _eligible(self) -> list[tuple]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    o.observation_id,
                    o.symbol,
                    o.signal_time,
                    o.variant_entry,
                    o.variant_stop,
                    o.variant_target,
                    o.features,
                    e.started_at
                FROM research.prospective_observation o
                JOIN research.prospective_experiment e
                  ON e.experiment_id = o.experiment_id
                LEFT JOIN research.me_reverse_long_v3_execution_outcome r
                  ON r.observation_id = o.observation_id
                WHERE o.experiment_id = %s
                  AND e.status = 'RUNNING'
                  AND e.started_at IS NOT NULL
                  AND o.signal_time > e.started_at
                  AND o.direction = 'LONG'
                  AND o.features->>'_v3_shadow_only' = 'true'
                  AND o.features->>'_v3_temporal_status' = 'TEMPORAL_VALIDATED'
                  AND (r.observation_id IS NULL OR r.status <> 'FINALIZED')
                ORDER BY o.signal_time, o.observation_id
                """,
                (EXPERIMENT_ID,),
            )
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def run_evaluation_cycle(
        self, now: datetime | None = None
    ) -> dict[str, int]:
        asof = _utc(now or datetime.now(timezone.utc))
        stats = {
            "checked": 0,
            "finalized": 0,
            "pending": 0,
            "coverage_invalid": 0,
            "errors": 0,
        }

        for row in self._eligible():
            stats["checked"] += 1
            (
                observation_id, symbol, signal_time,
                entry, stop, target, raw_features, started_at,
            ) = row

            try:
                features = _features(raw_features)
                group = features["_v3_filter_group"]
                entry_at = _utc(datetime.fromisoformat(
                    features["_v3_execution_entry_at"].replace("Z", "+00:00")
                ))
                detected_at = _utc(signal_time)
                freeze_at = _utc(started_at)

                if (
                    detected_at <= freeze_at
                    or entry_at != next_executable_entry_at(detected_at)
                ):
                    raise ValueError("INVALID_TEMPORAL_BOUNDARY")

                deadline = entry_at + timedelta(
                    minutes=MAX_HOLD_MINUTES
                )

                if asof < deadline:
                    record = V3OutcomeRecord(
                        observation_id=observation_id,
                        status="PENDING",
                        filter_group=group,
                        execution_entry_at=entry_at,
                        evaluated_at=asof,
                        coverage_reason="IMMATURE_WINDOW",
                    )
                    key = "pending"
                else:
                    source_candles, fetch_error = fetch_v3_execution_candles(
                        self.candle_source, symbol, entry_at, asof
                    )
                    if fetch_error:
                        window, coverage_error = [], fetch_error
                    else:
                        window, coverage_error = build_v3_window(
                            source_candles, entry_at, asof
                        )

                    if coverage_error:
                        record = V3OutcomeRecord(
                            observation_id=observation_id,
                            status="COVERAGE_INVALID",
                            filter_group=group,
                            execution_entry_at=entry_at,
                            evaluated_at=asof,
                            coverage_reason=coverage_error,
                            candle_count=len(window),
                        )
                        key = "coverage_invalid"
                    else:
                        # The complete closed entry candle is now available.
                        # Its opening price is a research-only fill assumption.
                        model_entry = float(window[0].open)
                        entry_price, stop_price, target_price = (
                            execution_geometry(model_entry)
                        )
                        geometry = V3Geometry(
                            entry_price, stop_price, target_price
                        )
                        outcome, execution_error = evaluate_v3_execution(
                            window, entry_at, geometry
                        )
                        if execution_error or outcome is None:
                            raise ValueError(
                                execution_error or "EMPTY_EXECUTION_OUTCOME"
                            )

                        record = V3OutcomeRecord(
                            observation_id=observation_id,
                            status="FINALIZED",
                            filter_group=group,
                            execution_entry_at=entry_at,
                            evaluated_at=asof,
                            execution_entry_price=entry_price,
                            execution_stop_price=stop_price,
                            execution_target_price=target_price,
                            exit_at=outcome.exit_at,
                            exit_price=outcome.exit_price,
                            exit_reason=outcome.exit_reason,
                            gross_r=outcome.gross_r,
                            net_r_normal=outcome.net_r_normal,
                            net_r_elevated=outcome.net_r_elevated,
                            ambiguous=outcome.ambiguous,
                            candle_count=len(window),
                        )
                        key = "finalized"

                action = self.repository.upsert(record)
                if action == "INSERTED_OR_UPDATED":
                    stats[key] += 1

            except Exception:
                stats["errors"] += 1
                logger.exception(
                    "V3 execution evaluation failed: observation_id=%s",
                    observation_id,
                )

        return stats
