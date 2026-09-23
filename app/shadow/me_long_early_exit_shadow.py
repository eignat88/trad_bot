"""Shadow/OOS Experiment: ME_R_LONG Early MAE Exit.

Records per-observation counterfactual evaluations for every new paper trade
from MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG direction.

Hypothesis:
  If MAE >= 0.50R within 15 minutes after entry, early exit at the close
  of the 15m candle may substantially reduce the loss compared to current
  paper trading logic.

Design:
  - Shadow/observe-only: NEVER closes a real paper position.
  - On trade OPEN: creates observation rows for all 7 variants.
  - Every position-monitor cycle: updates MAE_15m (or 10m/30m) and checks
    rule trigger.  If triggered, records the counterfactual exit price.
  - On trade CLOSE: fills actual_pnl_r, computes delta_r, marks CLOSED.

Variants (frozen — do not change after deployment):
  MAE_10m_040 : 10m / MAE >= 0.40R
  MAE_10m_050 : 10m / MAE >= 0.50R
  MAE_15m_040 : 15m / MAE >= 0.40R
  MAE_15m_050 : 15m / MAE >= 0.50R  <-- PRIMARY
  MAE_15m_060 : 15m / MAE >= 0.60R
  MAE_15m_075 : 15m / MAE >= 0.75R
  MAE_30m_050 : 30m / MAE >= 0.50R

Tables:
  dds.me_r_long_early_exit_observation

Design principles:
  - OOS isolation: only trades entered after experiment started_at.
  - Idempotency: UNIQUE (experiment_id, trade_id, variant_id).
  - No look-ahead: MAE is computed from candles that are closed.
  - Counterfactual exit: close of first 5m candle on or after evaluation_time.
  - OHLC limitation: within-candle price ordering is unknown; results are
    shadow estimates, not tick-level simulation.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ME_R_LONG_EARLY_MAE_EXIT_OOS_V1"
SCANNER_NAME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
DIRECTION = "LONG"

# --- Variant definitions (FROZEN — do not change after deployment) ---

@dataclass(frozen=True)
class VariantDef:
    variant_id: str
    eval_minutes: int  # evaluation age in minutes
    mae_threshold_r: float  # MAE threshold in R units


VARIANTS: tuple[VariantDef, ...] = (
    VariantDef("MAE_10m_040", eval_minutes=10, mae_threshold_r=0.40),
    VariantDef("MAE_10m_050", eval_minutes=10, mae_threshold_r=0.50),
    VariantDef("MAE_15m_040", eval_minutes=15, mae_threshold_r=0.40),
    VariantDef("MAE_15m_050", eval_minutes=15, mae_threshold_r=0.50),  # PRIMARY
    VariantDef("MAE_15m_060", eval_minutes=15, mae_threshold_r=0.60),
    VariantDef("MAE_15m_075", eval_minutes=15, mae_threshold_r=0.75),
    VariantDef("MAE_30m_050", eval_minutes=30, mae_threshold_r=0.50),
)

PRIMARY_VARIANT = VariantDef("MAE_15m_050", eval_minutes=15, mae_threshold_r=0.50)


def compute_risk_price(entry_price: float, stop_price: float) -> float:
    """Risk price for LONG: abs(entry - stop)."""
    return abs(entry_price - stop_price)


def compute_mae_r(
    entry_price: float, stop_price: float, lowest_since_entry: float,
) -> float:
    """MAE in R units for LONG.

    MAE_R = (entry_price - min_low_since_entry) / risk_price
    """
    risk = compute_risk_price(entry_price, stop_price)
    if risk <= 0:
        return 0.0
    return (entry_price - lowest_since_entry) / risk


def compute_counterfactual_r(
    exit_price: float, entry_price: float, stop_price: float,
) -> float:
    """Counterfactual exit R for LONG.

    counterfactual_r = (exit_price - entry_price) / risk_price
    """
    risk = compute_risk_price(entry_price, stop_price)
    if risk <= 0:
        return 0.0
    return (exit_price - entry_price) / risk


def compute_counterfactual_r_with_fees(
    exit_price: float,
    entry_price: float,
    stop_price: float,
    entry_fee: float,
    exit_fee: float,
    position_size: float,
) -> float:
    """Counterfactual exit R accounting for fees, matching paper_trade pnl_r.

    paper_trade.pnl_r = (gross_pnl - entry_fee - exit_fee) / risk_usdt
    For consistency we replicate this formula.
    """
    risk = compute_risk_price(entry_price, stop_price)
    if risk <= 0:
        return 0.0
    gross_pnl = (exit_price - entry_price) * position_size
    net_pnl = gross_pnl - entry_fee - exit_fee
    risk_usdt = risk * position_size
    if risk_usdt <= 0:
        return 0.0
    return net_pnl / risk_usdt


def compute_evaluation_time(
    entered_at: datetime, eval_minutes: int,
) -> datetime:
    """Compute the evaluation timestamp: entered_at + eval_minutes."""
    return entered_at + timedelta(minutes=eval_minutes)


def select_counterfactual_exit_price(
    candle_close_times: list[datetime],
    candle_closes: list[float],
    evaluation_time: datetime,
) -> tuple[datetime, float] | None:
    """Select the counterfactual exit price: close of first 5m candle
    with close_time >= evaluation_time.

    Parameters
    ----------
    candle_close_times : list[datetime]
        Sorted close_times of closed 5m candles in the trade window.
    candle_closes : list[float]
        Corresponding close prices.
    evaluation_time : datetime
        The time at which the rule is evaluated.

    Returns
    -------
    (close_time, close_price) or None if no candle found.
    """
    for close_time, close_price in zip(candle_close_times, candle_closes):
        if close_time >= evaluation_time:
            return (close_time, close_price)
    return None


class MELongEarlyExitShadowObserver:
    """Observes MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG paper trades
    and records counterfactual early-exit evaluations.

    Lifecycle:
      1. on_trade_open(): called when a new paper trade is opened.
         Creates observation rows for all variants (status=PENDING).
      2. evaluate_variants(): called periodically (e.g. position monitor cycle).
         Queries candles to compute MAE at each variant's evaluation horizon.
         If rule triggers, records counterfactual exit (status=EVALUATED).
      3. on_trade_close(): called when the paper trade is closed.
         Fills actual_pnl_r, computes delta_r, marks status=CLOSED.

    This class is purely observational — it NEVER closes paper positions.
    """

    def __init__(self, repository: Any) -> None:
        self.repo = repository
        self._observation_count = 0
        self._evaluated_count = 0
        self._closed_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # LIFECYCLE: Trade Open
    # ------------------------------------------------------------------

    def on_trade_open(
        self,
        trade_id: int,
        symbol: str,
        entered_at: datetime,
        entry_price: float,
        stop_price: float,
    ) -> int:
        """Create observation rows for all variants when a paper trade opens.

        Parameters
        ----------
        trade_id : int
            dds.paper_trade.trade_id
        symbol : str
            Trading pair symbol
        entered_at : datetime
            Trade entry timestamp
        entry_price : float
            Entry price
        stop_price : float
            Stop loss price

        Returns
        -------
        Number of observation rows created.
        """
        risk_price = compute_risk_price(entry_price, stop_price)
        created = 0

        for variant in VARIANTS:
            observation_id = self._save_observation(
                trade_id=trade_id,
                symbol=symbol,
                variant_id=variant.variant_id,
                entered_at=entered_at,
                entry_price=entry_price,
                stop_price=stop_price,
                risk_price=risk_price,
                threshold_mae_r=variant.mae_threshold_r,
            )
            if observation_id is not None:
                created += 1

        self._observation_count += created

        logger.info(
            "ME_R_LONG EARLY_EXIT OOS observations created: trade_id=%d "
            "symbol=%s entries=%d experiment_id=%s",
            trade_id, symbol, created, EXPERIMENT_ID,
        )
        return created

    # ------------------------------------------------------------------
    # LIFECYCLE: Periodic Evaluation
    # ------------------------------------------------------------------

    def evaluate_variants(
        self,
        trade_id: int,
        symbol: str,
        instrument_id: int,
        entered_at: datetime,
        entry_price: float,
        stop_price: float,
        current_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate all variant rules for a trade that is still open.

        Queries market.candle to find the lowest low in each evaluation
        window and the counterfactual exit price.

        Parameters
        ----------
        trade_id : int
        symbol : str
        instrument_id : int
            dds.instrument.instrument_id for candle lookup
        entered_at : datetime
        entry_price : float
        stop_price : float
        current_time : datetime
            The current time for evaluation (defaults to now UTC).

        Returns
        -------
        dict with evaluation results per variant.
        """
        now = current_time or datetime.now(timezone.utc)
        risk_price = compute_risk_price(entry_price, stop_price)
        results: dict[str, Any] = {}

        for variant in VARIANTS:
            try:
                result = self._evaluate_single_variant(
                    trade_id=trade_id,
                    symbol=symbol,
                    instrument_id=instrument_id,
                    entered_at=entered_at,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    risk_price=risk_price,
                    variant=variant,
                    current_time=now,
                )
                results[variant.variant_id] = result
            except Exception:
                logger.exception(
                    "ME_R_LONG EARLY_EXIT evaluation failed: trade_id=%d variant=%s",
                    trade_id, variant.variant_id,
                )
                self._error_count += 1
                results[variant.variant_id] = {"error": True}

        return results

    def _evaluate_single_variant(
        self,
        trade_id: int,
        symbol: str,
        instrument_id: int,
        entered_at: datetime,
        entry_price: float,
        stop_price: float,
        risk_price: float,
        variant: VariantDef,
        current_time: datetime,
    ) -> dict[str, Any]:
        """Evaluate a single variant against candle data."""
        evaluation_time = compute_evaluation_time(entered_at, variant.eval_minutes)

        # If not yet time to evaluate, skip
        if current_time < evaluation_time:
            return {"status": "PENDING", "age_min": 0.0}

        # Query candles in the window [entered_at, current_time]
        candles = self._query_candles(
            instrument_id=instrument_id,
            from_time=entered_at,
            to_time=current_time,
        )

        if not candles:
            return {"status": "NO_CANDLES", "age_min": 0.0}

        # Compute MAE from lowest low across all candles in window
        # We use the low from candles that overlap the evaluation window
        candle_lows = [c["low"] for c in candles]
        min_low = min(candle_lows)
        mae_r = compute_mae_r(entry_price, stop_price, min_low)

        # Determine rule trigger
        rule_triggered = mae_r >= variant.mae_threshold_r

        # Compute counterfactual exit price if triggered
        counterfactual_exit_at = None
        counterfactual_exit_price = None
        counterfactual_exit_r = None

        if rule_triggered:
            # Find the first 5m candle with close_time >= evaluation_time
            cf = select_counterfactual_exit_price(
                candle_close_times=[c["close_time"] for c in candles],
                candle_closes=[c["close"] for c in candles],
                evaluation_time=evaluation_time,
            )
            if cf is not None:
                counterfactual_exit_at, counterfactual_exit_price = cf
                # TODO: Use fee-aware R when fees are available
                counterfactual_exit_r = compute_counterfactual_r(
                    counterfactual_exit_price, entry_price, stop_price,
                )

        # Compute MFE for logging
        candle_highs = [c["high"] for c in candles]
        max_high = max(candle_highs)
        mfe_r = 0.0
        if risk_price > 0:
            mfe_r = (max_high - entry_price) / risk_price

        # Determine age in minutes
        age_min = (current_time - entered_at).total_seconds() / 60.0

        # Update observation in DB
        self._update_observation_evaluation(
            trade_id=trade_id,
            variant_id=variant.variant_id,
            evaluation_at=evaluation_time if current_time >= evaluation_time else None,
            evaluation_age_min=round(age_min, 2),
            mfe_r_at_eval=round(mfe_r, 6),
            mae_r_at_eval=round(mae_r, 6),
            rule_triggered=rule_triggered,
            counterfactual_exit_at=counterfactual_exit_at,
            counterfactual_exit_price=counterfactual_exit_price,
            counterfactual_exit_r=round(counterfactual_exit_r, 6) if counterfactual_exit_r is not None else None,
        )

        if rule_triggered:
            self._evaluated_count += 1
            logger.info(
                "ME_R_LONG EARLY_EXIT OOS trade_id=%d symbol=%s "
                "variant=%s age=%.1fm mfe_r=%.3f mae_r=%.3f "
                "triggered=true shadow_exit_r=%s",
                trade_id, symbol, variant.variant_id, age_min,
                mfe_r, mae_r,
                f"{counterfactual_exit_r:.3f}" if counterfactual_exit_r is not None else "None",
            )

        return {
            "status": "EVALUATED" if rule_triggered else "PENDING",
            "mae_r": mae_r,
            "mfe_r": mfe_r,
            "triggered": rule_triggered,
            "exit_r": counterfactual_exit_r,
        }

    # ------------------------------------------------------------------
    # LIFECYCLE: Trade Close
    # ------------------------------------------------------------------

    def on_trade_close(
        self,
        trade_id: int,
        actual_pnl_r: float,
        actual_exit_reason: str | None = None,
    ) -> int:
        """Update all observations for a closed trade.

        Parameters
        ----------
        trade_id : int
        actual_pnl_r : float
            The real pnl_r from paper_trade.
        actual_exit_reason : str | None

        Returns
        -------
        Number of observations updated.
        """
        # Fetch all observations for this trade
        observations = self._get_observations_for_trade(trade_id)
        updated = 0

        for obs in observations:
            variant_id = obs["variant_id"]
            rule_triggered = obs["rule_triggered"]
            counterfactual_exit_r = obs["counterfactual_exit_r"]

            # Compute delta_r
            if rule_triggered and counterfactual_exit_r is not None:
                delta_r = counterfactual_exit_r - actual_pnl_r
                improved = delta_r > 0
            else:
                delta_r = None
                improved = None

            self._update_observation_close(
                trade_id=trade_id,
                variant_id=variant_id,
                actual_pnl_r=actual_pnl_r,
                actual_exit_reason=actual_exit_reason,
                delta_r=delta_r,
                improved=improved,
            )

            if rule_triggered and counterfactual_exit_r is not None:
                logger.info(
                    "ME_R_LONG EARLY_EXIT OUTCOME trade_id=%d "
                    "variant=%s actual_r=%.3f shadow_r=%s delta_r=%s improved=%s",
                    trade_id, variant_id,
                    actual_pnl_r,
                    f"{counterfactual_exit_r:.3f}" if counterfactual_exit_r is not None else "None",
                    f"{delta_r:+.3f}" if delta_r is not None else "None",
                    improved,
                )

            updated += 1

        self._closed_count += updated
        return updated

    # ------------------------------------------------------------------
    # DB OPERATIONS
    # ------------------------------------------------------------------

    def _save_observation(
        self,
        trade_id: int,
        symbol: str,
        variant_id: str,
        entered_at: datetime,
        entry_price: float,
        stop_price: float,
        risk_price: float,
        threshold_mae_r: float,
    ) -> int | None:
        """Persist a new observation row."""
        sql = """
        INSERT INTO dds.me_r_long_early_exit_observation (
            experiment_id, variant_id, trade_id, symbol,
            scanner_name, direction,
            entered_at, entry_price, stop_price, risk_price,
            threshold_mae_r, status
        ) VALUES (
            %(experiment_id)s, %(variant_id)s, %(trade_id)s, %(symbol)s,
            %(scanner_name)s, %(direction)s,
            %(entered_at)s, %(entry_price)s, %(stop_price)s, %(risk_price)s,
            %(threshold_mae_r)s, 'PENDING'
        )
        ON CONFLICT (experiment_id, trade_id, variant_id) DO UPDATE SET
            entry_price = EXCLUDED.entry_price,
            stop_price = EXCLUDED.stop_price,
            risk_price = EXCLUDED.risk_price,
            updated_at = now()
        RETURNING observation_id
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "variant_id": variant_id,
                "trade_id": trade_id,
                "symbol": symbol,
                "scanner_name": SCANNER_NAME,
                "direction": DIRECTION,
                "entered_at": entered_at,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "risk_price": risk_price,
                "threshold_mae_r": threshold_mae_r,
            })
            self.repo._conn.commit()
            row = self.repo._fetchone("SELECT lastval()")
            return row[0] if row else None
        except Exception:
            logger.exception(
                "Failed to save ME_R_LONG early exit observation trade_id=%d variant=%s",
                trade_id, variant_id,
            )
            self._error_count += 1
            return None

    def _update_observation_evaluation(
        self,
        trade_id: int,
        variant_id: str,
        evaluation_at: datetime | None,
        evaluation_age_min: float,
        mfe_r_at_eval: float,
        mae_r_at_eval: float,
        rule_triggered: bool,
        counterfactual_exit_at: datetime | None,
        counterfactual_exit_price: float | None,
        counterfactual_exit_r: float | None,
    ) -> None:
        """Update observation with evaluation data."""
        sql = """
        UPDATE dds.me_r_long_early_exit_observation
        SET evaluation_at = %(evaluation_at)s,
            evaluation_age_min = %(evaluation_age_min)s,
            mfe_r_at_eval = %(mfe_r_at_eval)s,
            mae_r_at_eval = %(mae_r_at_eval)s,
            rule_triggered = %(rule_triggered)s,
            counterfactual_exit_at = %(counterfactual_exit_at)s,
            counterfactual_exit_price = %(counterfactual_exit_price)s,
            counterfactual_exit_r = %(counterfactual_exit_r)s,
            status = CASE
                WHEN %(rule_triggered)s THEN 'EVALUATED'
                ELSE status
            END,
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND trade_id = %(trade_id)s
          AND variant_id = %(variant_id)s
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "trade_id": trade_id,
                "variant_id": variant_id,
                "evaluation_at": evaluation_at,
                "evaluation_age_min": evaluation_age_min,
                "mfe_r_at_eval": mfe_r_at_eval,
                "mae_r_at_eval": mae_r_at_eval,
                "rule_triggered": rule_triggered,
                "counterfactual_exit_at": counterfactual_exit_at,
                "counterfactual_exit_price": counterfactual_exit_price,
                "counterfactual_exit_r": counterfactual_exit_r,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception(
                "Failed to update ME_R_LONG early exit evaluation trade_id=%d variant=%s",
                trade_id, variant_id,
            )
            self._error_count += 1

    def _update_observation_close(
        self,
        trade_id: int,
        variant_id: str,
        actual_pnl_r: float,
        actual_exit_reason: str | None,
        delta_r: float | None,
        improved: bool | None,
    ) -> None:
        """Update observation with trade close data."""
        sql = """
        UPDATE dds.me_r_long_early_exit_observation
        SET actual_pnl_r = %(actual_pnl_r)s,
            actual_exit_reason = %(actual_exit_reason)s,
            actual_closed_at = now(),
            delta_r = %(delta_r)s,
            improved = %(improved)s,
            status = 'CLOSED',
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND trade_id = %(trade_id)s
          AND variant_id = %(variant_id)s
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "trade_id": trade_id,
                "variant_id": variant_id,
                "actual_pnl_r": actual_pnl_r,
                "actual_exit_reason": actual_exit_reason,
                "delta_r": delta_r,
                "improved": improved,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception(
                "Failed to update ME_R_LONG early exit close trade_id=%d variant=%s",
                trade_id, variant_id,
            )
            self._error_count += 1

    def _get_observations_for_trade(self, trade_id: int) -> list[dict]:
        """Fetch all observations for a given trade."""
        sql = """
        SELECT variant_id, rule_triggered, counterfactual_exit_r
        FROM dds.me_r_long_early_exit_observation
        WHERE experiment_id = %(experiment_id)s
          AND trade_id = %(trade_id)s
        """
        try:
            cursor = self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "trade_id": trade_id,
            })
            rows = cursor.fetchall() if cursor else []
            return [
                {
                    "variant_id": r[0],
                    "rule_triggered": r[1],
                    "counterfactual_exit_r": float(r[2]) if r[2] is not None else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception(
                "Failed to fetch observations for trade_id=%d", trade_id,
            )
            return []

    def _query_candles(
        self,
        instrument_id: int,
        from_time: datetime,
        to_time: datetime,
    ) -> list[dict]:
        """Query closed 5m candles from market.candle.

        Uses the intersection window:
            close_time > from_time AND open_time < to_time

        Note: OHLC limitation — if entry occurred within a candle,
        the intra-candle order of high/low relative to exact entry
        time is unknown. Results are shadow estimates.
        """
        sql = """
        SELECT open_time, close_time, open, high, low, close
        FROM market.candle
        WHERE instrument_id = %(instrument_id)s
          AND timeframe = '5'
          AND is_closed = TRUE
          AND close_time > %(from_time)s
          AND open_time < %(to_time)s
        ORDER BY open_time ASC
        """
        try:
            cursor = self.repo._execute(sql, {
                "instrument_id": instrument_id,
                "from_time": from_time,
                "to_time": to_time,
            })
            rows = cursor.fetchall() if cursor else []
            return [
                {
                    "open_time": r[0],
                    "close_time": r[1],
                    "open": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "close": float(r[5]),
                }
                for r in rows
            ]
        except Exception:
            logger.exception(
                "Failed to query candles for instrument_id=%d", instrument_id,
            )
            return []

    # ------------------------------------------------------------------
    # STATS
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return {
            "observations": self._observation_count,
            "evaluated": self._evaluated_count,
            "closed": self._closed_count,
            "errors": self._error_count,
        }
