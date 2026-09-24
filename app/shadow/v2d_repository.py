"""Repository layer for V2_D prospective OOS signal storage.

Stores PASS candidates in dds.v2d_signal / dds.v2d_outcome tables.
Separate from V1 shadow_signal — no cross-contamination.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class V2DSaveStatus(str, Enum):
    """Result status for save_signal()."""
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"


class V2DSaveResult:
    """Structured result from save_signal()."""
    __slots__ = ("status", "signal_id")

    def __init__(self, status: V2DSaveStatus, signal_id: int | None = None) -> None:
        self.status = status
        self.signal_id = signal_id

    def __repr__(self) -> str:
        return f"V2DSaveResult(status={self.status.value!r}, signal_id={self.signal_id})"


class V2DRepository:
    """Repository for V2_D prospective OOS signals and outcomes."""

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
        atr: float,
        atr_pct: float,
        wick_size: float,
        wick_atr: float,
        upper_wick_pct: float,
        close_location: float,
        rsi: float,
        stoch_rsi: float | None,
        bb_upper: float,
        bb_mid: float,
        bb_lower: float,
        bb_width: float,
        distance_to_upper_bb: float,
        ema_fast: float,
        ema_medium: float,
        ema_slow: float,
        ema_slope: float,
        volume_ratio: float,
        filter_pass: bool,
        filter_reason: str,
        signal_version: str = "1.0.0",
    ) -> V2DSaveResult:
        """Save a V2_D PASS candidate to the database.

        Returns V2DSaveResult with status:
        - INSERTED: new row created, signal_id set
        - DUPLICATE: ON CONFLICT DO NOTHING, no row inserted
        - ERROR: exception during execute/fetchone, rolled back
        """
        if not self._conn:
            return V2DSaveResult(V2DSaveStatus.ERROR)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.v2d_signal (
                    experiment_id, symbol, timeframe, direction,
                    signal_time, signal_price,
                    open, high, low, close, volume,
                    atr, atr_pct,
                    wick_size, wick_atr, upper_wick_pct, close_location,
                    rsi, stoch_rsi,
                    bb_upper, bb_mid, bb_lower, bb_width, distance_to_upper_bb,
                    ema_fast, ema_medium, ema_slow, ema_slope,
                    volume_ratio,
                    filter_pass, filter_reason,
                    signal_version
                ) VALUES (
                    'ATR_WICK_FILTER_OOS_V2_D', %s, '5m', 'SHORT',
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s,
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
                    atr, atr_pct,
                    wick_size, wick_atr, upper_wick_pct, close_location,
                    rsi, stoch_rsi,
                    bb_upper, bb_mid, bb_lower, bb_width, distance_to_upper_bb,
                    ema_fast, ema_medium, ema_slow, ema_slope,
                    volume_ratio,
                    filter_pass, filter_reason,
                    signal_version,
                ),
            )
            # fetchone BEFORE commit — pg8000 requires this
            row = cursor.fetchone()
            self._conn.commit()

            if row:
                return V2DSaveResult(V2DSaveStatus.INSERTED, signal_id=row[0])
            # ON CONFLICT DO NOTHING returned no row → duplicate
            return V2DSaveResult(V2DSaveStatus.DUPLICATE)

        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save V2D signal for %s", symbol)
            return V2DSaveResult(V2DSaveStatus.ERROR)

    def signal_exists(self, symbol: str, signal_time: datetime) -> bool:
        """Check if a V2_D signal already exists for this symbol and time."""
        if not self._conn:
            return False

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM dds.v2d_signal
            WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
              AND symbol = %s
              AND signal_time = %s
            """,
            (symbol, signal_time),
        )
        return cursor.fetchone() is not None

    def get_eligible_signals(self, limit: int = 1000) -> list[dict]:
        """Get V2_D signals that need evaluation of at least one horizon.

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
                o.signal_id AS outcome_id,
                o.evaluated_15m_at, o.evaluated_30m_at, o.evaluated_60m_at,
                o.evaluated_120m_at, o.evaluated_240m_at,
                o.is_final
            FROM dds.v2d_signal s
            LEFT JOIN dds.v2d_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
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
                "outcome_id": r[4],
                "evaluated_15m_at": r[5],
                "evaluated_30m_at": r[6],
                "evaluated_60m_at": r[7],
                "evaluated_120m_at": r[8],
                "evaluated_240m_at": r[9],
                "is_final": r[10],
            }
            for r in rows
        ]

    def save_outcome_partial(
        self,
        signal_id: int,
        symbol: str,
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
        reached_minus_0_5: bool | None = None,
        reached_minus_1_0: bool | None = None,
        reached_minus_1_5: bool | None = None,
        reached_minus_2_0: bool | None = None,
        hit_plus_0_5_before_target: bool | None = None,
        hit_plus_1_0_before_target: bool | None = None,
        hit_plus_1_5_before_target: bool | None = None,
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
            ("reached_minus_0_5", reached_minus_0_5), ("reached_minus_1_0", reached_minus_1_0),
            ("reached_minus_1_5", reached_minus_1_5), ("reached_minus_2_0", reached_minus_2_0),
            ("hit_plus_0_5_before_target", hit_plus_0_5_before_target),
            ("hit_plus_1_0_before_target", hit_plus_1_0_before_target),
            ("hit_plus_1_5_before_target", hit_plus_1_5_before_target),
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
            signal_id, "ATR_WICK_FILTER_OOS_V2_D", symbol,
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
            INSERT INTO dds.v2d_outcome (
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
            logger.exception("Failed to save V2D outcome for signal %d", signal_id)
            return False

    def get_cohort_stats(self) -> list[dict]:
        """Get V2_D cohort statistics grouped by experiment_id."""
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                s.experiment_id,
                COUNT(*) AS signals,
                COUNT(o.signal_id) AS outcomes,
                MIN(s.signal_time) AS first_signal,
                MAX(s.signal_time) AS last_signal
            FROM dds.v2d_signal s
            LEFT JOIN dds.v2d_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
            GROUP BY s.experiment_id
            """
        )
        rows = cursor.fetchall()
        return [
            {
                "experiment_id": r[0],
                "signals": r[1],
                "outcomes": r[2],
                "first_signal": r[3],
                "last_signal": r[4],
            }
            for r in rows
        ]

    def validate_filter_compliance(self) -> dict[str, Any]:
        """Validate that all V2_D signals comply with the filter rules.

        Returns dict with:
        - total_signals: total V2_D signals
        - invalid_direction: signals where direction != SHORT
        - invalid_stoch_rsi: signals where stoch_rsi outside [0.20, 0.60)
        - invalid_null_stoch_rsi: signals where stoch_rsi is NULL
        - all_valid: True if no violations
        """
        if not self._conn:
            return {"total_signals": 0, "all_valid": True}

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE direction != 'SHORT') AS bad_dir,
                COUNT(*) FILTER (WHERE stoch_rsi IS NULL) AS null_stoch,
                COUNT(*) FILTER (WHERE stoch_rsi < 0.20 OR stoch_rsi >= 0.60) AS out_of_range
            FROM dds.v2d_signal
            WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
            """
        )
        row = cursor.fetchone()
        total, bad_dir, null_stoch, out_of_range = row[0], row[1], row[2], row[3]

        return {
            "total_signals": total,
            "invalid_direction": bad_dir,
            "invalid_null_stoch_rsi": null_stoch,
            "invalid_stoch_rsi": out_of_range,
            "all_valid": (bad_dir == 0 and null_stoch == 0 and out_of_range == 0),
        }
