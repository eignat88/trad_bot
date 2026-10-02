"""Prospective OOS Evaluator — multi-horizon evaluation for prospective experiments.

Reuses the same evaluation logic as the generic research evaluator but
operates on prospective_observation/prospective_outcome tables.

Key differences from generic evaluator:
1. Uses variant geometry (variant_entry/variant_stop/variant_target) when available
2. Supports AMBIGUOUS_INTRABAR flag for uncertain TP/SL ordering
3. Strict anti-leakage: only uses information available at signal_time
"""
from __future__ import annotations

import logging
import signal as _signal
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.research.constants import HORIZONS

logger = logging.getLogger(__name__)


def _is_mature(signal_time: datetime, horizon_minutes: int, now: datetime) -> bool:
    return now >= signal_time + timedelta(minutes=horizon_minutes)


def _calculate_mfe_mae(
    candles: list[Candle], entry_price: float, max_minutes: int,
    signal_time: datetime, is_short: bool,
) -> tuple[float | None, float | None]:
    """MFE/MAE for given direction within time window."""
    if not candles or entry_price <= 0:
        return None, None

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    max_fav = 0.0
    max_adv = 0.0
    found = False

    for c in candles:
        if c.timestamp <= signal_ts or c.timestamp >= cutoff_ts:
            continue
        found = True
        if is_short:
            fav = (entry_price - c.low) / entry_price * 100
            adv = (c.high - entry_price) / entry_price * 100
        else:
            fav = (c.high - entry_price) / entry_price * 100
            adv = (entry_price - c.low) / entry_price * 100
        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

    return (max_fav, max_adv) if found else (None, None)


def _check_tp_sl(
    candles: list[Candle], entry_price: float, stop_price: float,
    target_price: float | None, max_minutes: int, signal_time: datetime,
    is_short: bool, intrabar_policy: str = "ORDER_FIRST",
) -> dict[str, Any]:
    """Check TP/SL hit sequence with ambiguous intrabar detection."""
    result = {
        "tp_hit": False, "sl_hit": False,
        "tp_before_sl": False, "sl_before_tp": False,
        "ambiguous_intrabar": False,
        "time_to_tp": None, "time_to_sl": None,
    }

    if entry_price <= 0 or stop_price is None:
        return result

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    tp_seen = False
    sl_seen = False

    for c in candles:
        if c.timestamp <= signal_ts or c.timestamp >= cutoff_ts:
            break

        candle_minutes = (c.timestamp - signal_ts) / 60_000.0

        if is_short:
            tp_hit_now = target_price is not None and c.low <= target_price
            sl_hit_now = c.high >= stop_price
        else:
            tp_hit_now = target_price is not None and c.high >= target_price
            sl_hit_now = c.low <= stop_price

        # Both TP and SL in same candle = ambiguous
        if tp_hit_now and sl_hit_now:
            result["ambiguous_intrabar"] = True
            result["tp_hit"] = True
            result["sl_hit"] = True
            if intrabar_policy == "STOP_FIRST":
                result["sl_before_tp"] = True
            elif intrabar_policy == "TP_FIRST":
                result["tp_before_sl"] = True
            if result["time_to_tp"] is None:
                result["time_to_tp"] = round(candle_minutes, 1)
            if result["time_to_sl"] is None:
                result["time_to_sl"] = round(candle_minutes, 1)
            break

        if not tp_seen and tp_hit_now:
            tp_seen = True
            result["tp_hit"] = True
            if result["time_to_tp"] is None:
                result["time_to_tp"] = round(candle_minutes, 1)

        if not sl_seen and sl_hit_now:
            sl_seen = True
            result["sl_hit"] = True
            if result["time_to_sl"] is None:
                result["time_to_sl"] = round(candle_minutes, 1)

        # First-hit sequence (only if not ambiguous)
        if not result["ambiguous_intrabar"]:
            if tp_seen and not sl_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
                result["tp_before_sl"] = True
            if sl_seen and not tp_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
                result["sl_before_tp"] = True

    return result


class ProspectiveOOSEvaluator:
    """Multi-horizon evaluator for prospective OOS experiments."""

    def __init__(self, conn: Any, client: BybitClient, repo: Any = None) -> None:
        self._conn = conn
        self.client = client
        self._repo = repo  # ResearchRepository for get_eligible_signals if needed

    # Maximum fallback horizon for TP/SL checking when no frozen_max_hold is set.
    _DEFAULT_MAX_HOLD_MINUTES = 240

    @staticmethod
    def _get_frozen_max_hold(obs: dict) -> int:
        """Extract frozen max_hold from observation features.

        The observer embeds ``_frozen_max_hold`` in the features JSON for
        experiments that define a frozen exit window.  When present, TP/SL
        first-hit is checked ONLY within this window.  Events after the
        frozen max_hold do NOT change the frozen trade outcome.

        When absent (standard prospective experiments), falls back to the
        default 240-minute horizon so that existing behaviour is preserved.
        """
        features = obs.get("features")
        if features is None:
            return ProspectiveOOSEvaluator._DEFAULT_MAX_HOLD_MINUTES

        # features may arrive as a JSON string or as a dict
        if isinstance(features, str):
            try:
                import json as _json
                features = _json.loads(features)
            except (ValueError, TypeError):
                return ProspectiveOOSEvaluator._DEFAULT_MAX_HOLD_MINUTES

        hold = features.get("_frozen_max_hold")
        if isinstance(hold, (int, float)) and hold > 0:
            return int(hold)

        return ProspectiveOOSEvaluator._DEFAULT_MAX_HOLD_MINUTES

    def _get_candles(self, symbol: str, from_time: datetime, to_time: datetime) -> list[Candle]:
        try:
            start_ms = int((from_time - timedelta(minutes=5)).timestamp() * 1000)
            end_ms = int(to_time.timestamp() * 1000)
            needed = min((end_ms - start_ms) // 300_000 + 10, 1000)
            candles = self.client.get_klines(symbol, "5", needed)
            return [c for c in candles if start_ms <= c.timestamp <= end_ms]
        except Exception:
            logger.exception("prospective evaluator: failed to fetch candles for %s", symbol)
            return []

    def evaluate_observation(
        self, obs: dict, all_candles: list[Candle], now: datetime, stats: dict,
    ) -> None:
        """Evaluate a single prospective observation across all horizons."""
        signal_time = obs["signal_time"]
        symbol = obs["symbol"]
        is_short = obs["direction"] == "SHORT"

        # Determine entry/stop/target for this experiment variant
        if obs.get("variant_entry") is not None:
            entry_price = float(obs["variant_entry"])
            stop_price = float(obs["variant_stop"]) if obs["variant_stop"] is not None else None
            target_price = float(obs["variant_target"]) if obs["variant_target"] is not None else None
        else:
            entry_price = float(obs["reference_price"])
            stop_price = float(obs["invalidation_price"]) if obs["invalidation_price"] is not None else None
            target_price = float(obs["target_1"]) if obs["target_1"] is not None else None

        if entry_price <= 0:
            return

        risk_1r = abs(entry_price - stop_price) if stop_price else 0
        if risk_1r <= 0:
            if obs.get("experiment_id") == "VC_SHORT_EXECUTION_V1":
                return
            risk_1r = entry_price * 0.01

        signal_ts = int(signal_time.timestamp() * 1000)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        # Extract frozen_max_hold from observation features.
        # When present, TP/SL first-hit is checked only within this window.
        # Events AFTER frozen_max_hold do NOT change the frozen trade outcome.
        # MFE/MAE at all horizons (including 240m) continue to be computed.
        frozen_max_hold = self._get_frozen_max_hold(obs)

        updates = {}
        obs_id = obs["observation_id"]

        for label, minutes in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if obs.get(eval_field) is not None:
                continue
            if not _is_mature(signal_time, minutes, now):
                continue

            mfe_pct, mae_pct = _calculate_mfe_mae(
                post_candles, entry_price, minutes, signal_time, is_short,
            )

            risk_pct = risk_1r / entry_price * 100 if entry_price > 0 else 0
            mfe_r = round(mfe_pct / risk_pct, 4) if mfe_pct is not None and risk_pct > 0 else None
            mae_r = round(mae_pct / risk_pct, 4) if mae_pct is not None and risk_pct > 0 else None

            # Return at horizon
            ret = None
            if post_candles and entry_price > 0:
                last_close = None
                cutoff = int((signal_time + timedelta(minutes=minutes)).timestamp() * 1000)
                for c in post_candles:
                    if c.timestamp >= cutoff:
                        break
                    last_close = c.close
                if last_close is not None:
                    ret = (last_close - entry_price) / entry_price * 100

            updates[f"mfe_{label}"] = mfe_pct
            updates[f"mae_{label}"] = mae_pct
            updates[f"mfe_r_{label}"] = mfe_r
            updates[f"mae_r_{label}"] = mae_r
            updates[f"return_at_{label}"] = ret
            updates[eval_field] = now

        # Finalize: check TP/SL within the frozen_max_hold window (or 240m if none).
        all_done = all(
            obs.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )
        if all_done and not obs.get("is_final"):
            tp_sl = _check_tp_sl(
                post_candles, entry_price, stop_price, target_price,
                frozen_max_hold, signal_time, is_short,
                intrabar_policy="STOP_FIRST" if obs.get("experiment_id") == "VC_SHORT_EXECUTION_V1" else "ORDER_FIRST",
            )
            updates.update(tp_sl)
            updates["is_final"] = True

        if not updates:
            return

        # Upsert outcome
        try:
            cursor = self._conn.cursor()
            # Build SET clause dynamically
            set_parts = []
            values = []
            for key, val in updates.items():
                set_parts.append(f"{key} = %s")
                values.append(val)
            values.append(obs_id)

            cursor.execute(
                f"""
                INSERT INTO research.prospective_outcome (observation_id, experiment_id, {', '.join(updates.keys())})
                SELECT %s, o.experiment_id, {', '.join(['%s'] * len(updates))}
                FROM research.prospective_observation o
                WHERE o.observation_id = %s
                ON CONFLICT (observation_id) DO UPDATE SET
                    {', '.join(f'{k} = EXCLUDED.{k}' for k in updates.keys())}
                """,
                [obs_id] + list(updates.values()) + [obs_id],
            )
            self._conn.commit()

            for label, _ in HORIZONS:
                eval_field = f"evaluated_{label}_at"
                if eval_field in updates and obs.get(eval_field) is None:
                    stats["horizons_updated"][label] = stats["horizons_updated"].get(label, 0) + 1

            if updates.get("is_final"):
                stats["finalized"] = stats.get("finalized", 0) + 1

        except Exception:
            self._conn.rollback()
            stats["errors"] = stats.get("errors", 0) + 1
            logger.exception("prospective evaluator: upsert outcome failed for obs %d", obs_id)

    def run_evaluation_cycle(self, experiment_id: str) -> dict[str, Any]:
        """Run one evaluation cycle for a prospective experiment.

        CRITICAL: Only evaluates observations with signal_time >= experiment.started_at.
        Warm-up observations captured before activation are NEVER evaluated.

        Direction-level lifecycle:
        - Closed directions do NOT receive new observation creation (handled by observer).
        - Already-created immature observations for closed directions CONTINUE to mature.
        - Only truly new observations (no outcome row yet) are skipped for closed directions.
        """
        now = datetime.now(timezone.utc)

        # Get the experiment's started_at to enforce the prospective boundary
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT started_at FROM research.prospective_experiment WHERE experiment_id = %s",
            (experiment_id,),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return {"experiment_id": experiment_id, "signals_checked": 0,
                    "horizons_updated": {}, "finalized": 0, "errors": 0}
        started_at = row[0]

        # Load closed directions for this experiment (if any)
        closed_directions: set[str] = set()
        try:
            cursor.execute(
                "SELECT direction, status FROM research.prospective_experiment_direction_state "
                "WHERE experiment_id = %s",
                (experiment_id,),
            )
            for row_dir in cursor.fetchall():
                if row_dir[1] in {"CANCELLED", "COMPLETED", "CLOSED_NEGATIVE"}:
                    closed_directions.add(row_dir[0])
        except Exception:
            logger.debug(
                "prospective evaluator: direction lifecycle lookup failed for %s; "
                "falling back to experiment-level status only",
                experiment_id, exc_info=True,
            )
        finally:
            cursor.close()

        # ONLY observations with signal_time >= started_at are prospective
        # For closed directions: only evaluate observations that ALREADY have an
        # outcome row (immature = partial horizons, is_final=False). New
        # observations (no outcome row) for closed directions are excluded —
        # the observer should not create them, but this is defense in depth.
        cursor = self._conn.cursor()
        if closed_directions:
            direction_filter = " AND NOT (o.direction = ANY(%s) AND r.observation_id IS NULL)"
            cursor.execute(
                f"""
                SELECT o.observation_id, o.symbol, o.signal_time, o.experiment_id,
                       o.direction,
                       o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                       o.variant_entry, o.variant_stop, o.variant_target,
                       o.features,
                       r.evaluated_15m_at, r.evaluated_30m_at, r.evaluated_60m_at,
                       r.evaluated_120m_at, r.evaluated_240m_at, r.is_final
                FROM research.prospective_observation o
                LEFT JOIN research.prospective_outcome r ON r.observation_id = o.observation_id
                WHERE o.experiment_id = %s
                  AND o.signal_time >= %s
                  AND (r.observation_id IS NULL
                       OR r.is_final = FALSE)
                  {direction_filter}
                """,
                (experiment_id, started_at, sorted(closed_directions)),
            )
        else:
            cursor.execute(
                """
                SELECT o.observation_id, o.symbol, o.signal_time, o.experiment_id,
                       o.direction,
                       o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                       o.variant_entry, o.variant_stop, o.variant_target,
                       o.features,
                       r.evaluated_15m_at, r.evaluated_30m_at, r.evaluated_60m_at,
                       r.evaluated_120m_at, r.evaluated_240m_at, r.is_final
                FROM research.prospective_observation o
                LEFT JOIN research.prospective_outcome r ON r.observation_id = o.observation_id
                WHERE o.experiment_id = %s
                  AND o.signal_time >= %s
                  AND (r.observation_id IS NULL
                       OR r.is_final = FALSE)
                """,
                (experiment_id, started_at),
            )
        eligible = cursor.fetchall()
        cursor.close()
        self._conn.commit()

        stats = {
            "experiment_id": experiment_id,
            "signals_checked": len(eligible),
            "horizons_updated": {h: 0 for h, _ in HORIZONS},
            "finalized": 0,
            "errors": 0,
        }

        by_symbol: dict[str, list] = {}
        for row in eligible:
            sym = row[1]
            by_symbol.setdefault(sym, []).append(row)

        for symbol, rows in by_symbol.items():
            if not rows:
                continue
            oldest = min(r[2] for r in rows)
            candles = self._get_candles(symbol, oldest, now)
            if not candles:
                stats["errors"] += len(rows)
                continue
            for row in rows:
                obs = {
                    "observation_id": row[0], "symbol": row[1],
                    "signal_time": row[2], "experiment_id": row[3],
                    "direction": row[4],
                    "reference_price": row[5], "invalidation_price": row[6],
                    "target_1": row[7], "target_2": row[8],
                    "variant_entry": row[9], "variant_stop": row[10],
                    "variant_target": row[11],
                    "features": row[12],
                    "evaluated_15m_at": row[13], "evaluated_30m_at": row[14],
                    "evaluated_60m_at": row[15], "evaluated_120m_at": row[16],
                    "evaluated_240m_at": row[17], "is_final": row[18],
                }
                try:
                    self.evaluate_observation(obs, candles, now, stats)
                except Exception:
                    stats["errors"] += 1
                    logger.exception("prospective evaluator: error for obs %d", row[0])

        return stats

    def start(self, experiment_ids: list[str], interval_seconds: int = 300) -> None:
        """Start continuous evaluation loop."""
        self._shutdown = threading.Event()
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("prospective evaluator: signal %d, stopping", signum)
            self._running = False
            self._shutdown.set()

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)
        logger.info("prospective evaluator started: experiments=%s interval=%ds",
                     experiment_ids, interval_seconds)

        while self._running:
            for exp_id in experiment_ids:
                if not self._running:
                    break
                try:
                    stats = self.run_evaluation_cycle(exp_id)
                    logger.info("prospective evaluation [%s]: checked=%d finalized=%d errors=%d",
                                 exp_id, stats["signals_checked"], stats["finalized"], stats["errors"])
                except Exception:
                    logger.exception("prospective evaluation cycle failed for %s", exp_id)
            if self._running:
                self._shutdown.wait(timeout=interval_seconds)

        logger.info("prospective evaluator stopped")
