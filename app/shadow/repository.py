"""Repository layer for shadow signal storage."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.scanners.atr_wick_rejection_short import WickRejectionSignal

logger = logging.getLogger(__name__)


class ShadowSignalRepository:
    """Repository for storing and retrieving shadow signals."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def save_signal(self, signal: WickRejectionSignal) -> int | None:
        """Save a shadow signal to the database.

        Returns the signal_id if saved, None if duplicate or error.
        """
        if not self._conn:
            return None

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
                    volume_ratio, signal_version
                ) VALUES (
                    'ATR_WICK_REJECTION_SHORT_V1', %s, '5m', %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s
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
                    signal.signal_version,
                ),
            )
            self._conn.commit()
            row = cursor.fetchone()
            if row:
                return row[0]
            return None
        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save shadow signal for %s", signal.symbol)
            return None

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

    def get_signals_without_outcomes(
        self,
        min_age_minutes: int = 60,
        limit: int = 1000,
    ) -> list[dict]:
        """Get signals that haven't been evaluated yet."""
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT s.signal_id, s.symbol, s.signal_time, s.signal_price,
                   s.open, s.high, s.low, s.close, s.volume,
                   s.atr, s.atr_pct,
                   s.wick_size, s.wick_atr, s.upper_wick_pct, s.close_location,
                   s.rsi, s.stoch_rsi,
                   s.bb_upper, s.bb_mid, s.bb_lower, s.bb_width, s.distance_to_upper_bb,
                   s.ema_fast, s.ema_medium, s.ema_slow, s.ema_slope,
                   s.volume_ratio, s.signal_version
            FROM dds.shadow_signal s
            LEFT JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
            WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
              AND o.signal_id IS NULL
              AND s.signal_time < now() - (%s * interval '1 minute')
            ORDER BY s.signal_time ASC
            LIMIT %s
            """,
            (min_age_minutes, limit),
        )

        rows = cursor.fetchall()
        return [
            {
                "signal_id": r[0],
                "symbol": r[1],
                "signal_time": r[2],
                "signal_price": float(r[3]),
                "open": float(r[4]),
                "high": float(r[5]),
                "low": float(r[6]),
                "close": float(r[7]),
                "volume": float(r[8]),
                "atr": float(r[9]),
                "atr_pct": float(r[10]),
                "wick_size": float(r[11]),
                "wick_atr": float(r[12]),
                "upper_wick_pct": float(r[13]),
                "close_location": float(r[14]),
                "rsi": float(r[15]),
                "stoch_rsi": float(r[16]) if r[16] is not None else None,
                "bb_upper": float(r[17]),
                "bb_mid": float(r[18]),
                "bb_lower": float(r[19]),
                "bb_width": float(r[20]),
                "distance_to_upper_bb": float(r[21]),
                "ema_fast": float(r[22]),
                "ema_medium": float(r[23]),
                "ema_slow": float(r[24]),
                "ema_slope": float(r[25]),
                "volume_ratio": float(r[26]),
                "signal_version": r[27],
            }
            for r in rows
        ]

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
        if not self._conn:
            return False

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.shadow_signal_outcome (
                    signal_id, experiment_id, symbol,
                    mfe_15m, mae_15m, mfe_30m, mae_30m,
                    mfe_60m, mae_60m, mfe_120m, mae_120m,
                    mfe_240m, mae_240m, mfe_eod, mae_eod,
                    reached_minus_0_5, reached_minus_1_0,
                    reached_minus_1_5, reached_minus_2_0,
                    hit_plus_0_5_before_target, hit_plus_1_0_before_target,
                    hit_plus_1_5_before_target
                )
                SELECT
                    %s, 'ATR_WICK_REJECTION_SHORT_V1', s.symbol,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                FROM dds.shadow_signal s
                WHERE s.signal_id = %s
                ON CONFLICT (signal_id) DO UPDATE SET
                    mfe_15m = EXCLUDED.mfe_15m, mae_15m = EXCLUDED.mae_15m,
                    mfe_30m = EXCLUDED.mfe_30m, mae_30m = EXCLUDED.mae_30m,
                    mfe_60m = EXCLUDED.mfe_60m, mae_60m = EXCLUDED.mae_60m,
                    mfe_120m = EXCLUDED.mfe_120m, mae_120m = EXCLUDED.mae_120m,
                    mfe_240m = EXCLUDED.mfe_240m, mae_240m = EXCLUDED.mae_240m,
                    mfe_eod = EXCLUDED.mfe_eod, mae_eod = EXCLUDED.mae_eod,
                    reached_minus_0_5 = EXCLUDED.reached_minus_0_5,
                    reached_minus_1_0 = EXCLUDED.reached_minus_1_0,
                    reached_minus_1_5 = EXCLUDED.reached_minus_1_5,
                    reached_minus_2_0 = EXCLUDED.reached_minus_2_0,
                    hit_plus_0_5_before_target = EXCLUDED.hit_plus_0_5_before_target,
                    hit_plus_1_0_before_target = EXCLUDED.hit_plus_1_0_before_target,
                    hit_plus_1_5_before_target = EXCLUDED.hit_plus_1_5_before_target,
                    updated_at = now()
                """,
                (
                    signal_id,
                    mfe_15m, mae_15m, mfe_30m, mae_30m,
                    mfe_60m, mae_60m, mfe_120m, mae_120m,
                    mfe_240m, mae_240m, mfe_eod, mae_eod,
                    reached_minus_0_5, reached_minus_1_0,
                    reached_minus_1_5, reached_minus_2_0,
                    hit_plus_0_5_before_target, hit_plus_1_0_before_target,
                    hit_plus_1_5_before_target,
                    signal_id,
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save outcome for signal %d", signal_id)
            return False