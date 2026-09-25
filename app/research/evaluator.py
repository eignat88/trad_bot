"""Generic multi-horizon research evaluator.

Evaluates research signals on 5 time horizons (15m, 30m, 60m, 120m, 240m).
For each horizon computes:
  - MFE / MAE (as % from entry)
  - MFE_R / MAE_R (normalised to 1R = abs(entry - invalidation))
  - return_at_horizon (total return at horizon close)
  - TP hit / SL hit / TP-before-SL / SL-before-TP
  - time_to_TP / time_to_SL (in minutes)

Idempotent: running the evaluator multiple times does not create
duplicate outcomes.  Each horizon is evaluated at most once.
"""
from __future__ import annotations

import logging
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.research.constants import HORIZONS
from app.research.repository import ResearchRepository

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────

def _is_horizon_mature(signal_time: datetime, horizon_minutes: int, now: datetime) -> bool:
    """Return True when the full horizon window has elapsed."""
    return now >= signal_time + timedelta(minutes=horizon_minutes)


def _calculate_long_mfe_mae(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int,
    signal_time: datetime,
) -> tuple[float | None, float | None]:
    """MFE/MAE for LONG within [signal_time, signal_time + max_minutes).

    Returns (mfe_pct, mae_pct) or (None, None) if no candles found.
    """
    if not candles:
        return None, None

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    max_favorable = 0.0
    max_adverse = 0.0
    found = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            continue
        found = True
        fav = (c.high - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        adv = (entry_price - c.low) / entry_price * 100 if entry_price > 0 else 0.0
        max_favorable = max(max_favorable, fav)
        max_adverse = max(max_adverse, adv)

    return (max_favorable, max_adverse) if found else (None, None)


def _calculate_short_mfe_mae(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int,
    signal_time: datetime,
) -> tuple[float | None, float | None]:
    """MFE/MAE for SHORT within [signal_time, signal_time + max_minutes).

    SHORT: favorable = price going DOWN (entry - low), adverse = price going UP (high - entry).
    Returns (mfe_pct, mae_pct) or (None, None).
    """
    if not candles:
        return None, None

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    max_favorable = 0.0
    max_adverse = 0.0
    found = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            continue
        found = True
        fav = (entry_price - c.low) / entry_price * 100 if entry_price > 0 else 0.0
        adv = (c.high - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        max_favorable = max(max_favorable, fav)
        max_adverse = max(max_adverse, adv)

    return (max_favorable, max_adverse) if found else (None, None)


def _calculate_return_at_horizon(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int,
    signal_time: datetime,
) -> float | None:
    """Return at horizon close: (close_at_horizon - entry) / entry * 100.

    Uses the last candle close within the horizon window.
    """
    if not candles or entry_price <= 0:
        return None

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    last_close = None
    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            break
        last_close = c.close

    if last_close is None:
        return None
    return (last_close - entry_price) / entry_price * 100


def _check_long_tp_sl(
    candles: list[Candle],
    entry_price: float,
    invalidation_price: float,
    target_1: float,
    max_minutes: int,
    signal_time: datetime,
) -> dict[str, Any]:
    """Check TP/SL hit sequence for LONG within time window.

    Returns: {tp_hit, sl_hit, tp_before_sl, sl_before_tp, time_to_tp, time_to_sl}
    """
    result: dict[str, Any] = {
        "tp_hit": False, "sl_hit": False,
        "tp_before_sl": False, "sl_before_tp": False,
        "time_to_tp": None, "time_to_sl": None,
    }

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    tp_seen = False
    sl_seen = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            break

        candle_minutes = (c.timestamp - signal_ts) / 60_000.0

        # LONG: TP = high >= target_1, SL = low <= invalidation
        if not tp_seen and target_1 is not None and c.high >= target_1:
            tp_seen = True
            result["tp_hit"] = True
            if result["time_to_tp"] is None:
                result["time_to_tp"] = round(candle_minutes, 1)
        if not sl_seen and invalidation_price is not None and c.low <= invalidation_price:
            sl_seen = True
            result["sl_hit"] = True
            if result["time_to_sl"] is None:
                result["time_to_sl"] = round(candle_minutes, 1)

        # First-hit sequence
        if tp_seen and not sl_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["tp_before_sl"] = True
        if sl_seen and not tp_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["sl_before_tp"] = True

    return result


def _check_short_tp_sl(
    candles: list[Candle],
    entry_price: float,
    invalidation_price: float,
    target_1: float,
    max_minutes: int,
    signal_time: datetime,
) -> dict[str, Any]:
    """Check TP/SL hit sequence for SHORT within time window.

    SHORT: TP = low <= target_1, SL = high >= invalidation.
    """
    result: dict[str, Any] = {
        "tp_hit": False, "sl_hit": False,
        "tp_before_sl": False, "sl_before_tp": False,
        "time_to_tp": None, "time_to_sl": None,
    }

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    tp_seen = False
    sl_seen = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            break

        candle_minutes = (c.timestamp - signal_ts) / 60_000.0

        # SHORT: TP = low <= target_1, SL = high >= invalidation
        if not tp_seen and target_1 is not None and c.low <= target_1:
            tp_seen = True
            result["tp_hit"] = True
            if result["time_to_tp"] is None:
                result["time_to_tp"] = round(candle_minutes, 1)
        if not sl_seen and invalidation_price is not None and c.high >= invalidation_price:
            sl_seen = True
            result["sl_hit"] = True
            if result["time_to_sl"] is None:
                result["time_to_sl"] = round(candle_minutes, 1)

        if tp_seen and not sl_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["tp_before_sl"] = True
        if sl_seen and not tp_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["sl_before_tp"] = True

    return result


# ── evaluator ────────────────────────────────────────────────


class ResearchEvaluator:
    """Multi-horizon incremental evaluator for research signals.

    Idempotent: running multiple times does not create duplicates.
    Each horizon is evaluated at most once per signal.
    """

    def __init__(self, conn: Any, client: BybitClient) -> None:
        self._conn = conn
        self.client = client
        self._repo = ResearchRepository(conn)
        self._running = False

    # ── candle fetch ──────────────────────────────────────────

    def _get_candles_for_symbol(
        self, symbol: str, from_time: datetime, to_time: datetime,
    ) -> list[Candle]:
        """Fetch 5m candles covering [from_time, to_time]."""
        try:
            start_ms = int((from_time - timedelta(minutes=5)).timestamp() * 1000)
            end_ms = int(to_time.timestamp() * 1000)
            needed = min((end_ms - start_ms) // 300_000 + 10, 1000)
            candles = self.client.get_klines(symbol, "5", needed)
            return [c for c in candles if start_ms <= c.timestamp <= end_ms]
        except Exception:
            logger.exception("research evaluator: failed to fetch candles for %s", symbol)
            return []

    # ── single-signal evaluation ──────────────────────────────

    def _evaluate_signal(
        self,
        sig: dict,
        all_candles: list[Candle],
        now: datetime,
        stats: dict,
    ) -> None:
        """Evaluate a single signal, updating only mature horizons."""
        signal_time: datetime = sig["signal_time"]
        entry_price: float = sig["entry_price"]
        signal_id: int = sig["signal_id"]
        symbol: str = sig["symbol"]
        is_new = sig["outcome_id"] is None

        invalidation_price = sig.get("invalidation_price")
        target_1 = sig.get("target_1")

        # 1R = abs(entry - invalidation) for R-normalisation
        risk_1r = abs(entry_price - invalidation_price) if invalidation_price else 0
        if risk_1r <= 0:
            risk_1r = entry_price * 0.01  # fallback: 1% of entry

        signal_ts = int(signal_time.timestamp() * 1000)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        # Determine direction from features or default to LONG
        # (direction is not stored in outcome, but needed for MFE/MAE calc)
        # We infer from the signal: if invalidation > entry → SHORT, else LONG
        is_short = invalidation_price is not None and invalidation_price > entry_price

        updates: dict[str, Any] = {}

        # ── time horizons ──
        for label, minutes in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if sig.get(eval_field) is not None:
                continue
            if not _is_horizon_mature(signal_time, minutes, now):
                continue

            # MFE/MAE in %
            if is_short:
                mfe_pct, mae_pct = _calculate_short_mfe_mae(
                    post_candles, entry_price, minutes, signal_time,
                )
            else:
                mfe_pct, mae_pct = _calculate_long_mfe_mae(
                    post_candles, entry_price, minutes, signal_time,
                )

            # R-normalised
            risk_pct = risk_1r / entry_price * 100 if entry_price > 0 else 0
            mfe_r = round(mfe_pct / risk_pct, 4) if mfe_pct is not None and risk_pct > 0 else None
            mae_r = round(mae_pct / risk_pct, 4) if mae_pct is not None and risk_pct > 0 else None

            # Return at horizon
            ret = _calculate_return_at_horizon(post_candles, entry_price, minutes, signal_time)

            updates[f"mfe_{label}"] = mfe_pct
            updates[f"mae_{label}"] = mae_pct
            updates[f"mfe_r_{label}"] = mfe_r
            updates[f"mae_r_{label}"] = mae_r
            updates[f"return_at_{label}"] = ret
            updates[eval_field] = now

        # ── finalization (at 240m horizon) ──
        all_done = all(
            sig.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )

        if all_done and not sig.get("is_final") and invalidation_price is not None:
            # TP/SL at the longest horizon (240m)
            if is_short:
                tp_sl = _check_short_tp_sl(
                    post_candles, entry_price, invalidation_price, target_1,
                    240, signal_time,
                )
            else:
                tp_sl = _check_long_tp_sl(
                    post_candles, entry_price, invalidation_price, target_1,
                    240, signal_time,
                )
            updates.update(tp_sl)
            updates["is_final"] = True

        if not updates:
            return

        # Upsert outcome
        saved = self._repo.upsert_outcome(
            signal_id=signal_id,
            experiment_id="",  # denormalized from signal
            symbol=symbol,
            updates=updates,
        )
        if not saved:
            stats["errors"] += 1
            return

        for label, _ in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if eval_field in updates and sig.get(eval_field) is None:
                stats["horizons_updated"][label] += 1

        if is_new:
            stats["outcomes_created"] += 1
        else:
            stats["outcomes_updated"] += 1

        if updates.get("is_final"):
            stats["finalized"] += 1

    # ── full cycle ────────────────────────────────────────────

    def run_evaluation_cycle(self, experiment_id: str) -> dict[str, Any]:
        """Run one complete evaluation cycle for an experiment.

        Returns stats dict with: signals_checked, symbols_processed,
        horizons_updated, outcomes_created, outcomes_updated, finalized, errors.
        """
        now = datetime.now(timezone.utc)

        eligible = self._repo.get_eligible_signals(experiment_id, limit=5000)

        by_symbol: dict[str, list[dict]] = {}
        for s in eligible:
            by_symbol.setdefault(s["symbol"], []).append(s)

        stats: dict[str, Any] = {
            "experiment_id": experiment_id,
            "start_time": now.isoformat(),
            "signals_checked": len(eligible),
            "symbols_processed": 0,
            "horizons_updated": {h: 0 for h, _ in HORIZONS},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
        }

        for symbol, signals in by_symbol.items():
            stats["symbols_processed"] += 1
            oldest = min(s["signal_time"] for s in signals)
            candles = self._get_candles_for_symbol(symbol, oldest, now)
            if not candles:
                stats["errors"] += len(signals)
                continue
            for s in signals:
                try:
                    self._evaluate_signal(s, candles, now, stats)
                except Exception:
                    stats["errors"] += 1
                    logger.exception("research evaluator: error evaluating signal %d", s["signal_id"])

        stats["end_time"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "research evaluation [%s]: checked=%d symbols=%d created=%d updated=%d "
            "finalized=%d errors=%d",
            experiment_id,
            stats["signals_checked"], stats["symbols_processed"],
            stats["outcomes_created"], stats["outcomes_updated"],
            stats["finalized"], stats["errors"],
        )
        return stats

    # ── lifecycle ─────────────────────────────────────────────

    def start(self, experiment_ids: list[str], interval_seconds: int = 300) -> None:
        """Start continuous evaluation loop for multiple experiments."""
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("research evaluator: received signal %d, stopping", signum)
            self._running = False

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)
        logger.info(
            "research evaluator started: experiments=%s interval=%ds",
            experiment_ids, interval_seconds,
        )

        while self._running:
            for exp_id in experiment_ids:
                try:
                    self.run_evaluation_cycle(exp_id)
                except Exception:
                    logger.exception("research evaluation cycle failed for %s", exp_id)
            if self._running:
                time.sleep(interval_seconds)
        logger.info("research evaluator stopped")
