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
    is_short: bool,
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

    def __init__(self, conn: Any, client: BybitClient) -> None:
        self._conn = conn
        self.client = client

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
            risk_1r = entry_price * 0.01

        signal_ts = int(signal_time.timestamp() * 1000)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

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

        # Finalize at 240m: check TP/SL
        all_done = all(
            obs.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )
        if all_done and not obs.get("is_final"):
            tp_sl = _check_tp_sl(
                post_candles, entry_price, stop_price, target_price,
                240, signal_time, is_short,
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

        # ONLY observations with signal_time >= started_at are prospective
        cursor.execute(
            """
            SELECT o.observation_id, o.symbol, o.signal_time, o.experiment_id,
                   o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                   o.variant_entry, o.variant_stop, o.variant_target,
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
                    "reference_price": row[4], "invalidation_price": row[5],
                    "target_1": row[6], "target_2": row[7],
                    "variant_entry": row[8], "variant_stop": row[9],
                    "variant_target": row[10],
                    "evaluated_15m_at": row[11], "evaluated_30m_at": row[12],
                    "evaluated_60m_at": row[13], "evaluated_120m_at": row[14],
                    "evaluated_240m_at": row[15], "is_final": row[16],
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
