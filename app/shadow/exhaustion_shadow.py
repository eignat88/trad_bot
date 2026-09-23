"""Shadow Exhaustion Filter Experiment for MOMENTUM_EXHAUSTION_REVERSE_LONG_V2.

Records a classification observation for every V2 signal:
  - exhaustion_magnitude <= 0.4  → PASS
  - exhaustion_magnitude >  0.4  → REJECT
  - missing feature              → MISSING_FEATURE

The classification is recorded but NEVER blocks the V2 trade.
V2 continues to operate identically — this is a shadow/control experiment.

When V2 opens a paper trade for a PASS signal, the actual trade outcome
is used.  For REJECT signals that V2 did NOT trade, a counterfactual
shadow outcome is computed using the same SL/TP/hold parameters as V2.

Tables:
  dds.shadow_exhaustion_observation  — per-signal observation records
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "V2_EXHAUSTION_SHADOW_V1"
THRESHOLD = 0.4
SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"


class ExhaustionShadowObserver:
    """Observes V2 signals and records exhaustion_magnitude classification.

    This class is purely observational — it never prevents V2 from trading.
    """

    def __init__(self, repository: Any) -> None:
        self.repo = repository
        self._observation_count = 0
        self._pass_count = 0
        self._reject_count = 0
        self._missing_count = 0

    def observe_signal(
        self,
        setup_id: str,
        scanner_name: str,
        scanner_version: str | None,
        symbol: str,
        instrument_id: int | None,
        direction: str,
        detected_at: datetime | None,
        signal_candle_open_time: int | None,
        features: dict,
    ) -> dict[str, Any]:
        """Classify a V2 signal and persist the observation.

        Parameters
        ----------
        setup_id : str
            The scanner_setup.setup_id for this signal.
        features : dict
            The features JSONB from scanner_setup — must contain
            'exhaustion_magnitude'.

        Returns
        -------
        dict with keys: filter_result, exhaustion_magnitude, observation_id
        """
        exhaustion_mag = features.get("exhaustion_magnitude")
        if exhaustion_mag is None:
            filter_result = "MISSING_FEATURE"
        elif float(exhaustion_mag) <= THRESHOLD:
            filter_result = "PASS"
        else:
            filter_result = "REJECT"

        self._observation_count += 1
        if filter_result == "PASS":
            self._pass_count += 1
        elif filter_result == "REJECT":
            self._reject_count += 1
        else:
            self._missing_count += 1

        observation_id = self._save_observation(
            setup_id=setup_id,
            scanner_name=scanner_name,
            scanner_version=scanner_version,
            symbol=symbol,
            instrument_id=instrument_id,
            direction=direction,
            detected_at=detected_at,
            signal_candle_open_time=signal_candle_open_time,
            exhaustion_magnitude=float(exhaustion_mag) if exhaustion_mag is not None else None,
            filter_result=filter_result,
        )

        logger.info(
            "shadow exhaustion observation: setup=%s symbol=%s exhaustion=%.4f → %s",
            setup_id, symbol,
            float(exhaustion_mag) if exhaustion_mag is not None else 0.0,
            filter_result,
        )

        return {
            "filter_result": filter_result,
            "exhaustion_magnitude": exhaustion_mag,
            "observation_id": observation_id,
        }

    def record_trade_link(
        self,
        setup_id: str,
        trade_id: int,
        entry_price: float,
        stop_price: float,
        target_1: float,
        entered_at: datetime,
    ) -> None:
        """Link a V2 paper trade to its observation record.

        Called after paper trade is opened for a V2 signal.
        """
        sql = """
        UPDATE dds.shadow_exhaustion_observation
        SET has_trade = TRUE,
            source_trade_id = %(trade_id)s,
            entry_price = %(entry_price)s,
            stop_price = %(stop_price)s,
            target_1 = %(target_1)s,
            entered_at = %(entered_at)s,
            status = 'OPEN',
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND setup_id = %(setup_id)s
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
                "trade_id": trade_id,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_1": target_1,
                "entered_at": entered_at,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception("Failed to record trade link for shadow observation setup=%s", setup_id)

    def record_trade_close(
        self,
        setup_id: str,
        exit_price: float,
        exit_reason: str,
        closed_at: datetime,
        pnl_usdt: float,
        pnl_r: float,
        mfe_r: float,
        mae_r: float,
        holding_minutes: float,
    ) -> None:
        """Update observation with actual V2 trade outcome."""
        sql = """
        UPDATE dds.shadow_exhaustion_observation
        SET exit_price = %(exit_price)s,
            exit_reason = %(exit_reason)s,
            closed_at = %(closed_at)s,
            pnl_usdt = %(pnl_usdt)s,
            pnl_r = %(pnl_r)s,
            mfe_r = %(mfe_r)s,
            mae_r = %(mae_r)s,
            holding_minutes = %(holding_minutes)s,
            status = 'CLOSED',
            outcome_source = 'LIVE_TRADE',
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND setup_id = %(setup_id)s
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "closed_at": closed_at,
                "pnl_usdt": pnl_usdt,
                "pnl_r": pnl_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "holding_minutes": holding_minutes,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception("Failed to record trade close for shadow observation setup=%s", setup_id)

    def record_no_trade(
        self,
        setup_id: str,
    ) -> None:
        """Mark observation as having no corresponding paper trade."""
        sql = """
        UPDATE dds.shadow_exhaustion_observation
        SET status = 'NO_TRADE',
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND setup_id = %(setup_id)s
          AND has_trade = FALSE
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception("Failed to mark no_trade for shadow observation setup=%s", setup_id)

    def _save_observation(
        self,
        setup_id: str,
        scanner_name: str,
        scanner_version: str | None,
        symbol: str,
        instrument_id: int | None,
        direction: str,
        detected_at: datetime | None,
        signal_candle_open_time: int | None,
        exhaustion_magnitude: float | None,
        filter_result: str,
    ) -> int | None:
        """Persist a new shadow observation."""
        sql = """
        INSERT INTO dds.shadow_exhaustion_observation (
            experiment_id, setup_id, scanner_name, scanner_version,
            symbol, instrument_id, direction, detected_at,
            signal_candle_open_time, exhaustion_magnitude, threshold,
            filter_result, status
        ) VALUES (
            %(experiment_id)s, %(setup_id)s, %(scanner_name)s, %(scanner_version)s,
            %(symbol)s, %(instrument_id)s, %(direction)s, %(detected_at)s,
            %(signal_candle_open_time)s, %(exhaustion_magnitude)s, %(threshold)s,
            %(filter_result)s, 'PENDING'
        )
        ON CONFLICT (experiment_id, setup_id) DO UPDATE SET
            exhaustion_magnitude = EXCLUDED.exhaustion_magnitude,
            filter_result = EXCLUDED.filter_result,
            updated_at = now()
        RETURNING observation_id
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
                "scanner_name": scanner_name,
                "scanner_version": scanner_version,
                "symbol": symbol,
                "instrument_id": instrument_id,
                "direction": direction,
                "detected_at": detected_at,
                "signal_candle_open_time": signal_candle_open_time,
                "exhaustion_magnitude": exhaustion_magnitude,
                "threshold": THRESHOLD,
                "filter_result": filter_result,
            })
            self.repo._conn.commit()
            row = self.repo._fetchone("SELECT lastval()")
            return row[0] if row else None
        except Exception:
            logger.exception("Failed to save shadow observation for setup=%s", setup_id)
            return None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "observations": self._observation_count,
            "pass": self._pass_count,
            "reject": self._reject_count,
            "missing": self._missing_count,
        }
