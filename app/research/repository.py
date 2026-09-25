"""Generic research repository — single DB layer for all experiments.

Provides fail-open insert/update operations for research observations,
signals, and outcomes.  Every method catches exceptions and returns
a safe default so that research failures never propagate to the caller.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.research.models import ResearchObservation

logger = logging.getLogger(__name__)


class ResearchRepository:
    """Generic repository for the research framework.

    Design principles:
    - Every public method is fail-open: catches all exceptions, logs,
      and returns a safe default (None / False / empty list).
    - Never raises — callers are shielded from DB errors.
    - Uses its own connection, independent of production connection.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # ── observation ──────────────────────────────────────────

    def save_observation(self, obs: ResearchObservation) -> int | None:
        """Insert a research observation.  Returns observation_id or None.

        Dedup: UNIQUE (experiment_id, symbol, signal_candle_open_time).
        On conflict: DO NOTHING (returns None — caller treats as duplicate).
        """
        if not self._conn:
            return None

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO research.research_observation (
                    experiment_id, scanner_name, scanner_version, parameter_set_id,
                    symbol, direction, signal_time, signal_candle_open_time,
                    reference_price, entry_zone_low, entry_zone_high,
                    invalidation_price, target_1, target_2, score,
                    status, rejection_stage, rejection_reason,
                    features, parameters, market_regime,
                    htf_timeframe, setup_timeframe, entry_timeframe, setup_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (experiment_id, symbol, direction, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO NOTHING
                RETURNING observation_id
                """,
                (
                    obs.experiment_id, obs.scanner_name, obs.scanner_version,
                    obs.parameter_set_id,
                    obs.symbol, obs.direction, obs.signal_time,
                    obs.signal_candle_open_time,
                    obs.reference_price, obs.entry_zone_low, obs.entry_zone_high,
                    obs.invalidation_price, obs.target_1, obs.target_2, obs.score,
                    obs.status, obs.rejection_stage, obs.rejection_reason,
                    json.dumps(obs.features), json.dumps(obs.parameters),
                    obs.market_regime,
                    obs.htf_timeframe, obs.setup_timeframe, obs.entry_timeframe,
                    obs.setup_id,
                ),
            )
            row = cursor.fetchone()
            self._conn.commit()
            return row[0] if row else None
        except Exception:
            self._conn.rollback()
            logger.exception(
                "research: save_observation failed for %s %s",
                obs.symbol, obs.experiment_id,
            )
            return None

    def update_observation_status(
        self,
        setup_id: str,
        status: str,
        rejection_stage: str | None = None,
        rejection_reason: str | None = None,
    ) -> bool:
        """Update the status of a DETECTED observation after production pipeline.

        Only updates rows still in DETECTED status (append-only semantics).
        """
        if not self._conn or not setup_id:
            return False

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE research.research_observation
                SET status = %s,
                    rejection_stage = COALESCE(%s, rejection_stage),
                    rejection_reason = COALESCE(%s, rejection_reason)
                WHERE setup_id = %s
                  AND status = 'DETECTED'
                """,
                (status, rejection_stage, rejection_reason, setup_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0
        except Exception:
            self._conn.rollback()
            logger.exception("research: update_observation_status failed for setup_id=%s", setup_id)
            return False

    # ── signal promotion ─────────────────────────────────────

    def promote_to_signal(self, observation_id: int) -> int | None:
        """Promote a DETECTED observation to a research signal.

        Only promotes if the observation has valid price levels:
        - reference_price > 0
        - invalidation_price > 0
        - target_1 > 0

        Returns signal_id or None.
        """
        if not self._conn:
            return None

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO research.research_signal (
                    observation_id, experiment_id, scanner_name, parameter_set_id,
                    symbol, direction, signal_time, signal_candle_open_time,
                    reference_price, invalidation_price, target_1, target_2,
                    score, features, parameters, market_regime
                )
                SELECT
                    o.observation_id, o.experiment_id, o.scanner_name, o.parameter_set_id,
                    o.symbol, o.direction, o.signal_time, o.signal_candle_open_time,
                    o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                    o.score, o.features, o.parameters, o.market_regime
                FROM research.research_observation o
                WHERE o.observation_id = %s
                  AND o.reference_price > 0
                  AND o.invalidation_price > 0
                  AND o.target_1 > 0
                ON CONFLICT (experiment_id, symbol, direction, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO NOTHING
                RETURNING signal_id
                """,
                (observation_id,),
            )
            row = cursor.fetchone()
            self._conn.commit()
            return row[0] if row else None
        except Exception:
            self._conn.rollback()
            logger.exception("research: promote_to_signal failed for observation %d", observation_id)
            return None

    # ── batch resolve + promote ─────────────────────────────

    def resolve_and_promote_batch(
        self,
        rejections: list[tuple[str, str, str | None, str | None]],
        setup_ready: list[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch resolve observation statuses and promote eligible to signals.

        Three phases, each independently committed:
          1. Mark rejected observations (DEDUP/GATE/SCORE/GEOMETRY/REGIME)
          2. Mark SETUP_READY observations
          3. Promote ALL non-DETECTED observations to research_signal

        Both SETUP_READY and REJECTED get outcomes — the core goal
        is researching rejected candidates.

        Idempotent: re-running only touches DETECTED observations
        and only inserts signals that don't exist yet.

        Returns dict with counts: resolved, promoted, errors.
        """
        if not self._conn:
            return {"resolved": 0, "promoted": 0, "errors": 0}

        stats = {"resolved": 0, "promoted": 0, "errors": 0}

        # Phase 1: resolve rejected observations
        cursor = self._conn.cursor()
        try:
            for setup_id, status, stage, reason in rejections:
                try:
                    cursor.execute(
                        """
                        UPDATE research.research_observation
                        SET status = %s,
                            rejection_stage = COALESCE(%s, rejection_stage),
                            rejection_reason = COALESCE(%s, rejection_reason)
                        WHERE setup_id = %s
                          AND status = 'DETECTED'
                        """,
                        (status, stage, reason, setup_id),
                    )
                    if cursor.rowcount > 0:
                        stats["resolved"] += 1
                except Exception:
                    stats["errors"] += 1
                    logger.exception("research: resolve rejection failed for %s", setup_id)

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            logger.exception("research: resolve_and_promote_batch phase 1 failed")
            stats["errors"] += 1

        # Phase 2: mark SETUP_READY observations
        cursor = self._conn.cursor()
        try:
            for setup_id, _ in setup_ready:
                try:
                    cursor.execute(
                        """
                        UPDATE research.research_observation
                        SET status = 'SETUP_READY'
                        WHERE setup_id = %s
                          AND status = 'DETECTED'
                        """,
                        (setup_id,),
                    )
                    if cursor.rowcount > 0:
                        stats["resolved"] += 1
                except Exception:
                    stats["errors"] += 1
                    logger.exception("research: resolve setup_ready failed for %s", setup_id)

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            logger.exception("research: resolve_and_promote_batch phase 2 failed")
            stats["errors"] += 1

        # Phase 3: promote ALL resolved observations to signals
        # Both SETUP_READY and REJECTED — research needs outcomes for both
        try:
            cursor.execute(
                """
                INSERT INTO research.research_signal (
                    observation_id, experiment_id, scanner_name, parameter_set_id,
                    symbol, direction, signal_time, signal_candle_open_time,
                    reference_price, invalidation_price, target_1, target_2,
                    score, features, parameters, market_regime
                )
                SELECT
                    o.observation_id, o.experiment_id, o.scanner_name, o.parameter_set_id,
                    o.symbol, o.direction, o.signal_time, o.signal_candle_open_time,
                    o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                    o.score, o.features, o.parameters, o.market_regime
                FROM research.research_observation o
                WHERE o.status != 'DETECTED'
                  AND o.reference_price > 0
                  AND o.invalidation_price > 0
                  AND o.target_1 > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM research.research_signal s
                      WHERE s.observation_id = o.observation_id
                  )
                ON CONFLICT (experiment_id, symbol, direction, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO NOTHING
                """
            )
            stats["promoted"] = cursor.rowcount
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            logger.exception("research: promote batch failed")
            stats["errors"] += 1

        return stats

    # ── eligible signals for evaluator ───────────────────────

    def get_eligible_signals(self, experiment_id: str, limit: int = 5000) -> list[dict]:
        """Get signals needing at least one horizon evaluation.

        A signal is eligible when:
        - It has no outcome row (brand new), OR
        - It has an outcome row with at least one un-evaluated mature horizon
        """
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    s.signal_id, s.symbol, s.signal_time, s.reference_price,
                    s.invalidation_price, s.target_1,
                    o.signal_id AS outcome_id,
                    o.evaluated_15m_at, o.evaluated_30m_at, o.evaluated_60m_at,
                    o.evaluated_120m_at, o.evaluated_240m_at,
                    o.is_final
                FROM research.research_signal s
                LEFT JOIN research.research_outcome o ON o.signal_id = s.signal_id
                WHERE s.experiment_id = %s
                  AND (
                    (o.signal_id IS NULL AND s.signal_time <= now() - interval '15 minutes')
                    OR
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
                (experiment_id, limit),
            )
            rows = cursor.fetchall()
            return [
                {
                    "signal_id": r[0], "symbol": r[1], "signal_time": r[2],
                    "entry_price": float(r[3]),
                    "invalidation_price": float(r[4]) if r[4] is not None else None,
                    "target_1": float(r[5]) if r[5] is not None else None,
                    "outcome_id": r[6],
                    "evaluated_15m_at": r[7], "evaluated_30m_at": r[8],
                    "evaluated_60m_at": r[9], "evaluated_120m_at": r[10],
                    "evaluated_240m_at": r[11], "is_final": r[12],
                }
                for r in rows
            ]
        except Exception:
            logger.exception("research: get_eligible_signals failed for %s", experiment_id)
            return []

    # ── outcome upsert ───────────────────────────────────────

    def upsert_outcome(
        self,
        signal_id: int,
        experiment_id: str,
        symbol: str,
        updates: dict[str, Any],
    ) -> bool:
        """Upsert an outcome row with dynamic columns.

        Known outcome columns: mfe_*, mae_*, mfe_r_*, mae_r_*,
        return_at_*, evaluated_*_at, tp_hit, sl_hit, tp_before_sl,
        sl_before_tp, time_to_tp, time_to_sl, is_final.
        """
        if not self._conn:
            return False

        if not updates:
            return True  # no-op is success

        known_cols = {
            "mfe_15m", "mae_15m", "mfe_r_15m", "mae_r_15m", "return_at_15m", "evaluated_15m_at",
            "mfe_30m", "mae_30m", "mfe_r_30m", "mae_r_30m", "return_at_30m", "evaluated_30m_at",
            "mfe_60m", "mae_60m", "mfe_r_60m", "mae_r_60m", "return_at_60m", "evaluated_60m_at",
            "mfe_120m", "mae_120m", "mfe_r_120m", "mae_r_120m", "return_at_120m", "evaluated_120m_at",
            "mfe_240m", "mae_240m", "mfe_r_240m", "mae_r_240m", "return_at_240m", "evaluated_240m_at",
            "tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp",
            "time_to_tp", "time_to_sl",
            "is_final",
        }

        data_cols: list[str] = []
        data_vals: list[Any] = []
        for col, val in updates.items():
            if col in known_cols and val is not None:
                data_cols.append(col)
                data_vals.append(val)

        if not data_cols:
            return True

        now = datetime.now(timezone.utc)
        all_cols = ["signal_id", "experiment_id", "symbol"] + data_cols + ["updated_at"]
        all_vals: list[Any] = [signal_id, experiment_id, symbol] + data_vals + [now]
        placeholders = ["%s"] * len(all_cols)

        update_parts = [f"{col} = EXCLUDED.{col}" for col in data_cols]
        update_parts.append("updated_at = EXCLUDED.updated_at")

        sql = f"""
            INSERT INTO research.research_outcome (
                {', '.join(all_cols)}
            ) VALUES ({', '.join(placeholders)})
            ON CONFLICT (signal_id) DO UPDATE SET
                {', '.join(update_parts)}
        """

        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, all_vals)
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            logger.exception("research: upsert_outcome failed for signal %d", signal_id)
            return False
