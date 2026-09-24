"""Repository layer for ME_R_LONG_CLOSE_LOCATION_OOS prospective OOS storage.

Stores PASS and REJECT candidates in dds.me_r_long_close_location_oos_signal
and outcomes in dds.me_r_long_close_location_oos_outcome.
Separate from V1 scanner_setup — dedicated OOS observation tables.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MERLongCLoOosSaveStatus(str, Enum):
    """Result status for save_signal()."""
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"


class MERLongCLoOosSaveResult:
    """Structured result from save_signal()."""
    __slots__ = ("status", "signal_id")

    def __init__(self, status: MERLongCLoOosSaveStatus, signal_id: int | None = None) -> None:
        self.status = status
        self.signal_id = signal_id

    def __repr__(self) -> str:
        return f"MERLongCLoOosSaveResult(status={self.status.value!r}, signal_id={self.signal_id})"


class MERLongCLoOosRepository:
    """Repository for ME_R_LONG_CLOSE_LOCATION_OOS signals and outcomes."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def save_signal(
        self,
        symbol: str,
        signal_time: datetime,
        signal_price: float,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        close_location: float | None,
        close_location_threshold: float,
        filter_passed: bool,
        rsi: float | None = None,
        atr: float | None = None,
        signal_version: str = "1.0.0",
    ) -> MERLongCLoOosSaveResult:
        """Save a ME_R_LONG_CLOSE_LOCATION_OOS candidate to the database.

        Both PASS and REJECT candidates are saved for OOS analysis.
        Returns MERLongCLoOosSaveResult with status:
        - INSERTED: new row created, signal_id set
        - DUPLICATE: ON CONFLICT DO NOTHING, no row inserted
        - ERROR: exception during execute/fetchone, rolled back
        """
        if not self._conn:
            return MERLongCLoOosSaveResult(MERLongCLoOosSaveStatus.ERROR)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.me_r_long_close_location_oos_signal (
                    experiment_id, symbol, timeframe, direction,
                    signal_time, signal_price,
                    open, high, low, close, volume,
                    close_location, close_location_threshold, filter_passed,
                    rsi, atr,
                    signal_version
                ) VALUES (
                    'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1', %s, '5m', 'LONG',
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s
                )
                ON CONFLICT (experiment_id, symbol, signal_time) DO NOTHING
                RETURNING signal_id
                """,
                (
                    symbol,
                    signal_time,
                    signal_price,
                    open, high, low, close, volume,
                    close_location, close_location_threshold, filter_passed,
                    rsi, atr,
                    signal_version,
                ),
            )
            # fetchone BEFORE commit — pg8000 requires this
            row = cursor.fetchone()
            self._conn.commit()

            if row:
                return MERLongCLoOosSaveResult(MERLongCLoOosSaveStatus.INSERTED, signal_id=row[0])
            # ON CONFLICT DO NOTHING returned no row → duplicate
            return MERLongCLoOosSaveResult(MERLongCLoOosSaveStatus.DUPLICATE)

        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save ME_R_LONG_CL_OOS signal for %s", symbol)
            return MERLongCLoOosSaveResult(MERLongCLoOosSaveStatus.ERROR)

    def signal_exists(self, symbol: str, signal_time: datetime) -> bool:
        """Check if a signal already exists for this symbol and time."""
        if not self._conn:
            return False

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM dds.me_r_long_close_location_oos_signal
            WHERE experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
              AND symbol = %s
              AND signal_time = %s
            """,
            (symbol, signal_time),
        )
        return cursor.fetchone() is not None

    def get_eligible_signals(self, limit: int = 1000) -> list[dict]:
        """Get signals that need evaluation of at least one horizon.

        A signal is eligible when:
        - It has no outcome row (brand new), OR
        - It has an outcome row with at least one un-evaluated mature horizon
        """
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                s.signal_id, s.symbol, s.signal_time, s.signal_price,
                s.close_location, s.close_location_threshold, s.filter_passed,
                o.signal_id AS outcome_id,
                o.evaluated_15m_at, o.evaluated_30m_at, o.evaluated_60m_at,
                o.evaluated_120m_at, o.evaluated_240m_at,
                o.is_final
            FROM dds.me_r_long_close_location_oos_signal s
            LEFT JOIN dds.me_r_long_close_location_oos_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
              AND (
                -- New signals: at least 15m old
                (o.signal_id IS NULL AND s.signal_time <= now() - interval '15 minutes')
                OR
                -- Existing outcomes with incomplete mature horizons
                (o.signal_id IS NOT NULL AND o.is_final = FALSE
                 AND (
                    (o.evaluated_15m_at IS NULL AND s.signal_time <= now() - interval '15 minutes')
                    OR (o.evaluated_30m_at IS NULL AND s.signal_time <= now() - interval '30 minutes')
                    OR (o.evaluated_60m_at IS NULL AND s.signal_time <= now() - interval '60 minutes')
                    OR (o.evaluated_120m_at IS NULL AND s.signal_time <= now() - interval '120 minutes')
                    OR (o.evaluated_240m_at IS NULL AND s.signal_time <= now() - interval '240 minutes')
                 ))
              )
            ORDER BY s.signal_time ASC
            LIMIT %s
            """,
            (limit,),
        )

        rows = cursor.fetchall()
        return [
            {
                "signal_id": r[0],
                "symbol": r[1],
                "signal_time": r[2],
                "signal_price": float(r[3]),
                "close_location": float(r[4]) if r[4] is not None else None,
                "close_location_threshold": float(r[5]),
                "filter_passed": r[6],
                "outcome_id": r[7],
                "evaluated_15m_at": r[8],
                "evaluated_30m_at": r[9],
                "evaluated_60m_at": r[10],
                "evaluated_120m_at": r[11],
                "evaluated_240m_at": r[12],
                "is_final": r[13],
            }
            for r in rows
        ]

    def save_outcome_partial(
        self,
        signal_id: int,
        symbol: str,
        # Horizon values (only non-None will be written)
        mfe_15m: float | None = None,
        mae_15m: float | None = None,
        evaluated_15m_at: datetime | None = None,
        mfe_30m: float | None = None,
        mae_30m: float | None = None,
        evaluated_30m_at: datetime | None = None,
        mfe_60m: float | None = None,
        mae_60m: float | None = None,
        evaluated_60m_at: datetime | None = None,
        mfe_120m: float | None = None,
        mae_120m: float | None = None,
        evaluated_120m_at: datetime | None = None,
        mfe_240m: float | None = None,
        mae_240m: float | None = None,
        evaluated_240m_at: datetime | None = None,
        # R units
        mfe_15m_r: float | None = None,
        mae_15m_r: float | None = None,
        mfe_30m_r: float | None = None,
        mae_30m_r: float | None = None,
        mfe_60m_r: float | None = None,
        mae_60m_r: float | None = None,
        mfe_120m_r: float | None = None,
        mae_120m_r: float | None = None,
        mfe_240m_r: float | None = None,
        mae_240m_r: float | None = None,
        # Target flags
        hit_0_5r: bool | None = None,
        hit_1r: bool | None = None,
        hit_1_5r: bool | None = None,
        hit_2r: bool | None = None,
        is_final: bool = False,
    ) -> bool:
        """Upsert outcome with dynamic columns for both INSERT and UPDATE.

        Non-None values are included in the INSERT column list so new rows
        get their horizon values immediately.  On conflict the same columns
        are updated via EXCLUDED, so existing rows are also handled.
        """
        if not self._conn:
            return False

        # --- collect non-None columns ---
        all_pairs = [
            ("mfe_15m", mfe_15m), ("mae_15m", mae_15m), ("evaluated_15m_at", evaluated_15m_at),
            ("mfe_30m", mfe_30m), ("mae_30m", mae_30m), ("evaluated_30m_at", evaluated_30m_at),
            ("mfe_60m", mfe_60m), ("mae_60m", mae_60m), ("evaluated_60m_at", evaluated_60m_at),
            ("mfe_120m", mfe_120m), ("mae_120m", mae_120m), ("evaluated_120m_at", evaluated_120m_at),
            ("mfe_240m", mfe_240m), ("mae_240m", mae_240m), ("evaluated_240m_at", evaluated_240m_at),
            ("mfe_15m_r", mfe_15m_r), ("mae_15m_r", mae_15m_r),
            ("mfe_30m_r", mfe_30m_r), ("mae_30m_r", mae_30m_r),
            ("mfe_60m_r", mfe_60m_r), ("mae_60m_r", mae_60m_r),
            ("mfe_120m_r", mfe_120m_r), ("mae_120m_r", mae_120m_r),
            ("mfe_240m_r", mfe_240m_r), ("mae_240m_r", mae_240m_r),
            ("hit_0_5r", hit_0_5r), ("hit_1r", hit_1r),
            ("hit_1_5r", hit_1_5r), ("hit_2r", hit_2r),
        ]

        columns: list[str] = []
        values: list[Any] = []
        for col, val in all_pairs:
            if val is not None:
                columns.append(col)
                values.append(val)

        if not columns:
            return True

        # Build INSERT column list and placeholders
        insert_cols = ["signal_id", "experiment_id", "symbol"] + columns
        insert_placeholders = ["%s"] * (3 + len(columns))
        insert_values: list[Any] = [
            signal_id, "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", symbol,
        ] + values

        # Build ON CONFLICT UPDATE clause using EXCLUDED.* for each column
        update_clauses = [f"{col} = EXCLUDED.{col}" for col in columns]

        # is_final: only write when caller passes True (prevent reset)
        if is_final:
            insert_cols.append("is_final")
            insert_placeholders.append("%s")
            insert_values.append(True)
            update_clauses.append("is_final = EXCLUDED.is_final")

        # Always update updated_at
        update_clauses.append("updated_at = now()")

        sql = f"""
            INSERT INTO dds.me_r_long_close_location_oos_outcome (
                {', '.join(insert_cols)}
            ) VALUES ({', '.join(insert_placeholders)})
            ON CONFLICT (signal_id) DO UPDATE SET
                {', '.join(update_clauses)}
        """

        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, insert_values)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save outcome for signal %d", signal_id)
            return False

    def get_cohort_stats(self) -> list[dict]:
        """Get ME_R_LONG_CLOSE_LOCATION_OOS cohort statistics."""
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                s.experiment_id,
                COUNT(*) AS signals,
                COUNT(*) FILTER (WHERE s.filter_passed = TRUE) AS pass_count,
                COUNT(*) FILTER (WHERE s.filter_passed = FALSE) AS reject_count,
                COUNT(o.signal_id) AS outcomes,
                MIN(s.signal_time) AS first_signal,
                MAX(s.signal_time) AS last_signal
            FROM dds.me_r_long_close_location_oos_signal s
            LEFT JOIN dds.me_r_long_close_location_oos_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
            GROUP BY s.experiment_id
            """
        )
        rows = cursor.fetchall()
        return [
            {
                "experiment_id": r[0],
                "signals": r[1],
                "pass_count": r[2],
                "reject_count": r[3],
                "outcomes": r[4],
                "first_signal": r[5],
                "last_signal": r[6],
            }
            for r in rows
        ]

    def validate_filter_compliance(self) -> dict[str, Any]:
        """Validate that all signals comply with the filter rules.

        Returns dict with:
        - total_signals: total signals
        - invalid_direction: signals where direction != LONG
        - invalid_close_location: signals where close_location is NULL
        - all_valid: True if no violations
        """
        if not self._conn:
            return {"total_signals": 0, "all_valid": True}

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE direction != 'LONG') AS bad_dir,
                COUNT(*) FILTER (WHERE close_location IS NULL) AS null_cl
            FROM dds.me_r_long_close_location_oos_signal
            WHERE experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
            """
        )
        row = cursor.fetchone()
        total, bad_dir, null_cl = row[0], row[1], row[2]

        return {
            "total_signals": total,
            "invalid_direction": bad_dir,
            "invalid_close_location": null_cl,
            "all_valid": (bad_dir == 0),
        }
