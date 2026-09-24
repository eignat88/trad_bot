"""SRR LONG Research Observer — captures SRR LONG signals for research.

This module intercepts SUPPORT_RESISTANCE_REACTION LONG candidates
after they pass scanner validity/risk/score checks and persists them
with a full feature snapshot for downstream outcome evaluation and
parameter analysis.

Research capture is independent from the direction gate and paper trading:
- SRR LONG remains BLOCKED for paper trading (unchanged).
- Research signals are captured regardless of gate ENABLED/BLOCKED state.
- Research capture never creates paper trades or changes tradeability.
- Feature snapshot uses raw numeric values, not normalised booleans.
- Duplicate signals are deduplicated by (experiment, symbol, signal_candle_open_time).

Tables:
  dds.srr_research_signal — immutable feature snapshot per signal
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "SRR_LONG_OUTCOME_V1"
SCANNER_NAME = "SUPPORT_RESISTANCE_REACTION"


class SRRResearchObserver:
    """Observes SRR LONG candidates and persists research signals.

    This class is purely observational — it never enables paper trading.
    Research capture happens independently of the direction gate state.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._observation_count = 0
        self._insert_count = 0
        self._duplicate_count = 0
        self._error_count = 0

    def observe_blocked_candidate(
        self,
        setup_id: str,
        scanner_name: str,
        scanner_version: str,
        symbol: str,
        direction: str,
        detected_at: datetime,
        signal_candle_open_time: int,
        reference_price: float,
        entry_zone_low: float,
        entry_zone_high: float,
        invalidation_price: float,
        target_1: float | None,
        target_2: float | None,
        score: float,
        market_regime: str | None,
        features: dict[str, Any],
        reasons: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        """Record an SRR LONG candidate as a research signal.

        Called for every valid SRR LONG candidate regardless of direction
        gate state.  The name 'observe_blocked_candidate' is retained for
        backward compatibility.

        Parameters
        ----------
        setup_id : str
            UUID from the SetupCandidate.
        features : dict
            Extended features dict from SRR scanner (includes _raw_* keys).
        """
        if not self._conn:
            return {"status": "error", "reason": "no_connection"}

        if direction != "LONG":
            logger.warning(
                "srr_research_observer ignoring non-LONG direction=%s symbol=%s",
                direction, symbol,
            )
            return {"status": "ignored", "reason": "not_long"}

        self._observation_count += 1

        result = self._save_signal(
            setup_id=setup_id,
            scanner_name=scanner_name,
            scanner_version=scanner_version,
            symbol=symbol,
            detected_at=detected_at,
            signal_candle_open_time=signal_candle_open_time,
            reference_price=reference_price,
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            invalidation_price=invalidation_price,
            target_1=target_1,
            target_2=target_2,
            score=score,
            market_regime=market_regime,
            features=features,
            reasons=reasons,
        )

        logger.info(
            "srr_research observation: setup=%s symbol=%s regime=%s "
            "touches=%s wick_ratio=%s vol_ratio=%s → %s",
            setup_id, symbol, market_regime,
            features.get("_raw_touch_count", "?"),
            features.get("_raw_wick_body_ratio", "?"),
            features.get("_raw_volume_ratio", "?"),
            result["status"],
        )

        return result

    def _save_signal(
        self,
        setup_id: str,
        scanner_name: str,
        scanner_version: str,
        symbol: str,
        detected_at: datetime,
        signal_candle_open_time: int,
        reference_price: float,
        entry_zone_low: float,
        entry_zone_high: float,
        invalidation_price: float,
        target_1: float | None,
        target_2: float | None,
        score: float,
        market_regime: str | None,
        features: dict[str, Any],
        reasons: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        """Persist a new research signal."""
        import json

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.srr_research_signal (
                    experiment_id, setup_id, scanner_name, scanner_version,
                    symbol, direction, timeframe,
                    htf_timeframe, setup_timeframe, entry_timeframe,
                    signal_time, signal_candle_open_time,
                    reference_price, entry_zone_low, entry_zone_high,
                    invalidation_price, target_1, target_2,
                    -- normalised features
                    level_touch_count, rejection_strength,
                    rr_ratio, stop_distance_atr,
                    volume_spike, regime_alignment,
                    -- raw features
                    raw_touch_count, raw_level_distance_pct,
                    raw_atr, raw_atr_pct,
                    raw_candle_range, raw_candle_body,
                    raw_upper_wick, raw_lower_wick,
                    raw_wick_body_ratio,
                    raw_volume, raw_volume_ratio,
                    raw_rr, raw_risk_distance,
                    raw_risk_distance_pct, raw_stop_distance_atr,
                    -- context
                    score, market_regime, level_type,
                    reasons
                ) VALUES (
                    %s, %s, %s, %s, %s, 'LONG', '5m',
                    '1h', '15m', '5m',
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (experiment_id, symbol, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO UPDATE SET
                    reference_price = EXCLUDED.reference_price,
                    score = EXCLUDED.score,
                    market_regime = EXCLUDED.market_regime,
                    updated_at = now()
                RETURNING signal_id
                """,
                (
                    EXPERIMENT_ID, setup_id, scanner_name, scanner_version,
                    symbol, detected_at, signal_candle_open_time,
                    reference_price, entry_zone_low, entry_zone_high,
                    invalidation_price, target_1, target_2,
                    # normalised
                    features.get("level_touch_count", 0),
                    features.get("rejection_strength", 0),
                    features.get("rr_ratio", 0),
                    features.get("stop_distance_atr", 0),
                    features.get("volume_spike", False),
                    features.get("regime_alignment", 0),
                    # raw
                    features.get("_raw_touch_count", 0),
                    features.get("_raw_level_distance_pct", 0),
                    features.get("_raw_atr", 0),
                    features.get("_raw_atr_pct", 0),
                    features.get("_raw_candle_range", 0),
                    features.get("_raw_candle_body", 0),
                    features.get("_raw_upper_wick", 0),
                    features.get("_raw_lower_wick", 0),
                    features.get("_raw_wick_body_ratio", 0),
                    features.get("_raw_volume", 0),
                    features.get("_raw_volume_ratio", 0),
                    features.get("_raw_rr", 0),
                    features.get("_raw_risk_distance", 0),
                    features.get("_raw_risk_distance_pct", 0),
                    features.get("_raw_stop_distance_atr", 0),
                    # context
                    score, market_regime,
                    features.get("_raw_level_type", "support"),
                    json.dumps(list(reasons)),
                ),
            )
            row = cursor.fetchone()
            self._conn.commit()

            if row:
                self._insert_count += 1
                return {"status": "inserted", "signal_id": row[0]}
            # ON CONFLICT DO UPDATE returned no row → duplicate
            self._duplicate_count += 1
            return {"status": "duplicate"}

        except Exception:
            self._conn.rollback()
            self._error_count += 1
            logger.exception("Failed to save srr_research signal for %s", symbol)
            return {"status": "error"}

    @property
    def stats(self) -> dict[str, int]:
        return {
            "observations": self._observation_count,
            "inserted": self._insert_count,
            "duplicates": self._duplicate_count,
            "errors": self._error_count,
        }
