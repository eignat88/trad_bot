"""Atomic, V3-only research execution outcome persistence.

No activation, migrations, or network connections at import time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .protocol import EXPERIMENT_ID

STATUSES = frozenset({
    "PENDING", "FINALIZED", "COVERAGE_INVALID",
    "TEMPORAL_UNVERIFIED", "INVALID_GEOMETRY",
})
GROUPS = frozenset({"PASS", "REJECT", "MISSING_FEATURE"})
EXITS = frozenset({
    "TP_FIRST", "SL_FIRST", "AMBIGUOUS_STOP_FIRST", "TIMEOUT",
})


@dataclass(frozen=True)
class V3OutcomeRecord:
    observation_id: int
    status: str
    filter_group: str
    execution_entry_at: datetime | None
    evaluated_at: datetime
    execution_entry_price: float | None = None
    execution_stop_price: float | None = None
    execution_target_price: float | None = None
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_r: float | None = None
    net_r_normal: float | None = None
    net_r_elevated: float | None = None
    ambiguous: bool | None = None
    coverage_reason: str | None = None
    candle_count: int | None = None


def _aware(value: datetime | None) -> bool:
    return (
        value is not None
        and isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def validate_record(record: V3OutcomeRecord) -> None:
    if isinstance(record.observation_id, bool) or record.observation_id <= 0:
        raise ValueError("INVALID_OBSERVATION_ID")
    if record.status not in STATUSES or record.filter_group not in GROUPS:
        raise ValueError("INVALID_V3_STATUS_OR_GROUP")
    if not _aware(record.evaluated_at):
        raise ValueError("INVALID_EVALUATION_TIME")

    economics = (
        record.exit_at, record.exit_price, record.exit_reason,
        record.gross_r, record.net_r_normal, record.net_r_elevated,
    )

    if record.status != "FINALIZED":
        if any(value is not None for value in economics):
            raise ValueError("NONFINAL_ECONOMICS_FORBIDDEN")
        if record.ambiguous is not None:
            raise ValueError("NONFINAL_AMBIGUITY_FORBIDDEN")
        if any(v is not None for v in (
            record.execution_entry_price,
            record.execution_stop_price,
            record.execution_target_price,
        )):
            raise ValueError("NONFINAL_EXECUTION_GEOMETRY_FORBIDDEN")
        if record.status == "COVERAGE_INVALID" and not record.coverage_reason:
            raise ValueError("COVERAGE_REASON_REQUIRED")
        return

    if (
        not _aware(record.execution_entry_at)
        or not _aware(record.exit_at)
        or any(value is None for value in economics)
        or record.exit_reason not in EXITS
        or type(record.ambiguous) is not bool
        or record.coverage_reason is not None
        or record.candle_count != 48
        or any(v is None for v in (
            record.execution_entry_price,
            record.execution_stop_price,
            record.execution_target_price,
        ))
    ):
        raise ValueError("INVALID_FINALIZED_OUTCOME")

    for value in (
        record.exit_price, record.gross_r,
        record.net_r_normal, record.net_r_elevated,
    ):
        if not math.isfinite(float(value)):
            raise ValueError("NONFINITE_ECONOMICS")
    entry = float(record.execution_entry_price)
    stop = float(record.execution_stop_price)
    target = float(record.execution_target_price)
    if not (
        all(math.isfinite(v) for v in (entry, stop, target))
        and 0 < stop < entry < target
        and abs(stop / entry - 0.975) < 1e-6
        and abs(target / entry - 1.03) < 1e-6
    ):
        raise ValueError("INVALID_EXECUTION_GEOMETRY")

    if record.exit_price <= 0:
        raise ValueError("INVALID_EXIT_PRICE")
    if record.exit_at < record.execution_entry_at:
        raise ValueError("EXIT_BEFORE_ENTRY")


class MEReverseLongV3OutcomeRepository:
    def __init__(self, conn: Any):
        self.conn = conn

    def upsert(self, record: V3OutcomeRecord) -> str:
        """Persist only owned, post-activation V3 observations.

        Returns INSERTED_OR_UPDATED, ALREADY_FINALIZED, or NOT_ELIGIBLE.
        No production setup or generic outcome table is modified.
        """
        validate_record(record)

        cursor = self.conn.cursor()
        try:
            # Lock the V3 source observation and validate authoritative
            # lifecycle within this transaction.
            cursor.execute(
                """
                SELECT o.observation_id
                FROM research.prospective_observation o
                JOIN research.prospective_experiment e
                  ON e.experiment_id = o.experiment_id
                WHERE o.observation_id = %s
                  AND o.experiment_id = %s
                  AND e.status = 'RUNNING'
                  AND e.started_at IS NOT NULL
                  AND o.signal_time > e.started_at
                  AND o.direction = 'LONG'
                  AND o.features->>'_v3_shadow_only' = 'true'
                  AND o.features->>'_v3_temporal_status' = 'TEMPORAL_VALIDATED'
                  AND o.features->>'_v3_filter_group' = %s
                  AND o.features->>'_v3_execution_entry_at' = %s
                FOR UPDATE OF o
                FOR SHARE OF e
                """,
                (
                    record.observation_id,
                    EXPERIMENT_ID,
                    record.filter_group,
                    record.execution_entry_at.isoformat()
                    if record.execution_entry_at else None,
                ),
            )
            if cursor.fetchone() is None:
                self.conn.rollback()
                return "NOT_ELIGIBLE"

            cursor.execute(
                """
                INSERT INTO research.me_reverse_long_v3_execution_outcome (
                    observation_id, experiment_id, status, filter_group,
                    execution_entry_at,
                    execution_entry_price, execution_stop_price,
                    execution_target_price,
                    exit_at, exit_price, exit_reason,
                    gross_r, net_r_normal, net_r_elevated, ambiguous,
                    coverage_reason, candle_count, evaluated_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (observation_id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    filter_group = EXCLUDED.filter_group,
                    execution_entry_at = EXCLUDED.execution_entry_at,
                    execution_entry_price = EXCLUDED.execution_entry_price,
                    execution_stop_price = EXCLUDED.execution_stop_price,
                    execution_target_price = EXCLUDED.execution_target_price,
                    exit_at = EXCLUDED.exit_at,
                    exit_price = EXCLUDED.exit_price,
                    exit_reason = EXCLUDED.exit_reason,
                    gross_r = EXCLUDED.gross_r,
                    net_r_normal = EXCLUDED.net_r_normal,
                    net_r_elevated = EXCLUDED.net_r_elevated,
                    ambiguous = EXCLUDED.ambiguous,
                    coverage_reason = EXCLUDED.coverage_reason,
                    candle_count = EXCLUDED.candle_count,
                    evaluated_at = EXCLUDED.evaluated_at,
                    updated_at = now()
                WHERE
                    research.me_reverse_long_v3_execution_outcome.status
                    <> 'FINALIZED'
                RETURNING observation_id
                """,
                (
                    record.observation_id, EXPERIMENT_ID,
                    record.status, record.filter_group,
                    record.execution_entry_at,
                    record.execution_entry_price,
                    record.execution_stop_price,
                    record.execution_target_price,
                    record.exit_at,
                    record.exit_price, record.exit_reason,
                    record.gross_r, record.net_r_normal,
                    record.net_r_elevated, record.ambiguous,
                    record.coverage_reason, record.candle_count,
                    record.evaluated_at,
                ),
            )
            changed = cursor.fetchone() is not None
            self.conn.commit()
            return "INSERTED_OR_UPDATED" if changed else "ALREADY_FINALIZED"
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()
