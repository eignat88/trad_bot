"""Repository layer for shadow signal storage."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.scanners.atr_wick_rejection_short import WickRejectionSignal

logger = logging.getLogger(__name__)


class SaveSignalStatus(str, Enum):
    """Result status for save_signal()."""
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"


class SaveSignalResult:
    """Structured result from save_signal()."""
    __slots__ = ("status", "signal_id")

    def __init__(self, status: SaveSignalStatus, signal_id: int | None = None) -> None:
        self.status = status
        self.signal_id = signal_id

    def __repr__(self) -> str:
        return f"SaveSignalResult(status={self.status.value!r}, signal_id={self.signal_id})"


class ShadowSignalRepository:
    """Repository for storing and retrieving shadow signals."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def save_signal(self, signal: WickRejectionSignal) -> SaveSignalResult:
        """Save a shadow signal to the database.

        Returns SaveSignalResult with status:
        - INSERTED: new row created, signal_id set
        - DUPLICATE: ON CONFLICT DO NOTHING, no row inserted
        - ERROR: exception during execute/fetchone, rolled back

        Transaction order (pg8000 safe):
          cursor.execute(...)
          row = cursor.fetchone()
          conn.commit()
        """
        if not self._conn:
            return SaveSignalResult(SaveSignalStatus.ERROR)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.shadow_signal (
                    experiment_id, symbol, timeframe, signal_time, signal_price,
                    open, high, low, close, volume,
                    atr, atr_pct,
                    wick_size, wick_atr, upper_wick_pct, close_location,
                    rsi, stoch_rsi,
                    bb_upper, bb_mid, bb_lower, bb_width, distance_to_upper_bb,
                    ema_fast, ema_medium, ema_slow, ema_slope,
                    volume_ratio, strict_pass, signal_version
                ) VALUES (
                    'ATR_WICK_REJECTION_SHORT_V1', %s, '5m', %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (experiment_id, symbol, signal_time) DO NOTHING
                RETURNING signal_id
                """,
                (
                    signal.symbol,
                    signal.signal_time,
                    signal.signal_price,
                    signal.open,
                    signal.high,
                    signal.low,
                    signal.close,
                    signal.volume,
                    signal.atr,
                    signal.atr_pct,
                    signal.wick_size,
                    signal.wick_atr,
                    signal.upper_wick_pct,
                    signal.close_location,
                    signal.rsi,
                    signal.stoch_rsi,
                    signal.bb_upper,
                    signal.bb_mid,
                    signal.bb_lower,
                    signal.bb_width,
                    signal.distance_to_upper_bb,
                    signal.ema_fast,
                    signal.ema_medium,
                    signal.ema_slow,
                    signal.ema_slope,
                    signal.volume_ratio,
                    signal.strict_pass,
                    signal.signal_version,
                ),
            )
            # fetchone BEFORE commit — pg8000 requires this
            row = cursor.fetchone()
            self._conn.commit()

            if row:
                return SaveSignalResult(SaveSignalStatus.INSERTED, signal_id=row[0])
            # ON CONFLICT DO NOTHING returned no row → duplicate
            return SaveSignalResult(SaveSignalStatus.DUPLICATE)

        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save shadow signal for %s", signal.symbol)
            return SaveSignalResult(SaveSignalStatus.ERROR)

    def signal_exists(self, symbol: str, signal_time: datetime) -> bool:
        """Check if a signal already exists for this symbol and time."""
        if not self._conn:
            return False

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM dds.shadow_signal
            WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
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
                o.signal_id AS outcome_id,
                o.evaluated_15m_at, o.evaluated_30m_at, o.evaluated_60m_at,
                o.evaluated_120m_at, o.evaluated_240m_at, o.evaluated_eod_at,
                o.is_final
            FROM dds.shadow_signal s
            LEFT JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
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
                    OR (o.evaluated_eod_at IS NULL
                        AND s.signal_time < date_trunc('day', now())
                        AND s.signal_time >= date_trunc('day', now()) - interval '1 day')
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
                "evaluated_eod_at": r[10],
                "is_final": r[11],
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
        mfe_eod: float | None = None,
        mae_eod: float | None = None,
        evaluated_eod_at: datetime | None = None,
        # Target flags (only final horizon updates these)
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

        None means "don't touch this column now" — existing values are
        preserved.  0.0 and False are valid values and will be written.

        is_final semantics: passing is_final=False will NOT overwrite a row
        that already has is_final=TRUE (the UPDATE only writes is_final
        when the new value is explicitly True in the current call).
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
            ("mfe_eod", mfe_eod), ("mae_eod", mae_eod), ("evaluated_eod_at", evaluated_eod_at),
            ("reached_minus_0_5", reached_minus_0_5), ("reached_minus_1_0", reached_minus_1_0),
            ("reached_minus_1_5", reached_minus_1_5), ("reached_minus_2_0", reached_minus_2_0),
            ("hit_plus_0_5_before_target", hit_plus_0_5_before_target),
            ("hit_plus_1_0_before_target", hit_plus_1_0_before_target),
            ("hit_plus_1_5_before_target", hit_plus_1_5_before_target),
        ]

        # Separate: is_final is only written when explicitly True (never
        # overwrites an existing TRUE with FALSE).
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
            signal_id, "ATR_WICK_REJECTION_SHORT_V1", symbol,
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
            INSERT INTO dds.shadow_signal_outcome (
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

    # Legacy method kept for backward compatibility
    def save_outcome(
        self,
        signal_id: int,
        mfe_15m: float | None = None,
        mae_15m: float | None = None,
        mfe_30m: float | None = None,
        mae_30m: float | None = None,
        mfe_60m: float | None = None,
        mae_60m: float | None = None,
        mfe_120m: float | None = None,
        mae_120m: float | None = None,
        mfe_240m: float | None = None,
        mae_240m: float | None = None,
        mfe_eod: float | None = None,
        mae_eod: float | None = None,
        reached_minus_0_5: bool = False,
        reached_minus_1_0: bool = False,
        reached_minus_1_5: bool = False,
        reached_minus_2_0: bool = False,
        hit_plus_0_5_before_target: bool = False,
        hit_plus_1_0_before_target: bool = False,
        hit_plus_1_5_before_target: bool = False,
    ) -> bool:
        """Save signal outcome (MFE/MAE evaluation results)."""
        return self.save_outcome_partial(
            signal_id=signal_id,
            symbol="",
            mfe_15m=mfe_15m, mae_15m=mae_15m,
            mfe_30m=mfe_30m, mae_30m=mae_30m,
            mfe_60m=mfe_60m, mae_60m=mae_60m,
            mfe_120m=mfe_120m, mae_120m=mae_120m,
            mfe_240m=mfe_240m, mae_240m=mae_240m,
            mfe_eod=mfe_eod, mae_eod=mae_eod,
            reached_minus_0_5=reached_minus_0_5,
            reached_minus_1_0=reached_minus_1_0,
            reached_minus_1_5=reached_minus_1_5,
            reached_minus_2_0=reached_minus_2_0,
            hit_plus_0_5_before_target=hit_plus_0_5_before_target,
            hit_plus_1_0_before_target=hit_plus_1_0_before_target,
            hit_plus_1_5_before_target=hit_plus_1_5_before_target,
            is_final=True,
        )
