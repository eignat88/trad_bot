"""MFE/MAE evaluation module for shadow signals — multi-horizon incremental.

Horizons are evaluated independently as they mature:
  15m, 30m, 60m, 120m, 240m, EOD

EOD semantics (half-open interval):
  signal_time <= candle_time < next UTC midnight

No lookahead: when evaluating a horizon, only candles within
[signal_time, signal_time + horizon) are used.
EOD uses [signal_time, next UTC midnight).

Candle source: the shared ``app.research.candle_coverage_guard_v1`` — no
silent 1000-candle truncation, bounded pagination for large windows,
classified source statuses (``SOURCE_VALIDATED`` / ``SOURCE_FETCH_ERROR`` /
``SOURCE_INCOMPLETE_COVERAGE`` / ``SOURCE_NOT_YET_AVAILABLE``) and NO
fallback request after a Global Limiter deny.

Fail-closed contract (OOS_SHADOW_CANDLE_COVERAGE_FAIL_CLOSED_V1)::

    INCOMPLETE CANDLE COVERAGE
        -> NO evaluated_*_at WRITE
        -> NO is_final

Before every ``evaluated_<horizon>_at`` mark the complete closed 5m candle
path of ``[signal_time, signal_time + horizon)`` (EOD:
``[signal_time, next UTC midnight)``) is proven; before ``is_final=True``
the frozen target-flag window to EOD is proven separately.  An unproven
horizon writes nothing, stays pending and is re-evaluated on the next cycle;
previously finalized outcomes are never rewritten automatically.
"""
from __future__ import annotations

import argparse
import logging
import signal as _signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.research.candle_coverage_guard_v1 import (
    SOURCE_INCOMPLETE_COVERAGE,
    datetime_to_ms,
    fetch_candles_for_evaluation,
    fetch_status_stats_key,
    record_block_event,
    record_coverage_block,
    record_finalization_block,
    validate_candle_coverage,
)
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)

# Must match the experiment_id hardcoded in ShadowSignalRepository's upsert.
EXPERIMENT_ID = "ATR_WICK_REJECTION_SHORT_V1"

# Horizon definitions: (name, minutes)
HORIZONS: list[tuple[str, int]] = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]


@dataclass
class HorizonResult:
    mfe: float | None
    mae: float | None
    evaluated_at: datetime | None


# ── helpers ──────────────────────────────────────────────────

def _is_horizon_mature(signal_time: datetime, horizon_minutes: int, now: datetime) -> bool:
    """Return True when the full horizon window has elapsed."""
    return now >= signal_time + timedelta(minutes=horizon_minutes)


def _get_eod_exclusive(signal_time: datetime) -> datetime:
    """Return the half-open EOD boundary: next UTC midnight.

    For signal_time = 2026-09-23 10:30 UTC
    returns 2026-09-24 00:00:00 UTC

    Candles with timestamp < this value belong to the signal's UTC day.
    """
    next_midnight = (signal_time + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return next_midnight


def _is_eod_mature(signal_time: datetime, now: datetime) -> bool:
    """Check if EOD has been reached: now >= next UTC midnight."""
    return now >= _get_eod_exclusive(signal_time)


def _calculate_mfe_mae_for_window(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int | None,
    signal_time: datetime,
    eod_exclusive: datetime | None = None,
) -> tuple[float | None, float | None]:
    """MFE/MAE for SHORT using only candles in [signal_time, cutoff).

    max_minutes  = N   →  cutoff = signal_time + N minutes
    max_minutes  = None AND eod_exclusive = T  →  cutoff = T (EOD half-open)
    """
    if not candles:
        return None, None

    signal_ts = int(signal_time.timestamp() * 1000)

    if max_minutes is not None:
        cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)
    elif eod_exclusive is not None:
        cutoff_ts = int(eod_exclusive.timestamp() * 1000)
    else:
        cutoff_ts = None

    max_favorable = 0.0
    max_adverse = 0.0
    found = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if cutoff_ts is not None and c.timestamp >= cutoff_ts:
            continue
        found = True
        fav = (entry_price - c.low) / entry_price * 100
        adv = (c.high - entry_price) / entry_price * 100
        max_favorable = max(max_favorable, fav)
        max_adverse = max(max_adverse, adv)

    if not found:
        return None, None
    return max_favorable, max_adverse


def _check_target_achievement(
    candles: list[Candle],
    signal_price: float,
    eod_exclusive: datetime | None = None,
) -> dict[str, bool]:
    """Target flags using capped candle window.

    If eod_exclusive is set, only candles with timestamp < eod_exclusive
    are considered.  This prevents next-day candles from affecting
    final target results.
    """
    results: dict[str, bool] = {
        "reached_minus_0_5": False, "reached_minus_1_0": False,
        "reached_minus_1_5": False, "reached_minus_2_0": False,
        "hit_plus_0_5_before_target": False,
        "hit_plus_1_0_before_target": False,
        "hit_plus_1_5_before_target": False,
    }

    targets = {0.5: "reached_minus_0_5", 1.0: "reached_minus_1_0",
               1.5: "reached_minus_1_5", 2.0: "reached_minus_2_0"}
    adverse = {0.5: "hit_plus_0_5_before_target",
               1.0: "hit_plus_1_0_before_target",
               1.5: "hit_plus_1_5_before_target"}

    hit_targets: dict[float, bool] = {t: False for t in targets}
    hit_adverse: dict[float, bool] = {a: False for a in adverse}

    cutoff_ts = int(eod_exclusive.timestamp() * 1000) if eod_exclusive else None

    for c in candles:
        if cutoff_ts is not None and c.timestamp >= cutoff_ts:
            break

        for pct, key in targets.items():
            if not hit_targets[pct] and c.low <= signal_price * (1 - pct / 100):
                hit_targets[pct] = True
                results[key] = True
        for pct, key in adverse.items():
            if not hit_adverse[pct] and c.high >= signal_price * (1 + pct / 100):
                hit_adverse[pct] = True
                if not any(hit_targets.values()):
                    results[key] = True

    return results


# ── evaluator ────────────────────────────────────────────────

class ShadowSignalEvaluator:
    """Multi-horizon incremental evaluator for shadow signals."""

    def __init__(self, client: BybitClient, repo: ShadowSignalRepository) -> None:
        self.client = client
        self.repo = repo
        self._running = False

    # ── candle fetch ──────────────────────────────────────────

    def _get_candles_for_symbol(
        self, symbol: str, from_time: datetime, to_time: datetime,
    ) -> list[Candle]:
        """Fetch the PROVEN closed 5m path for *symbol* covering the window.

        Delegates to the shared guard ``fetch_candles_for_evaluation``:

        * only closed, grid-aligned candles are returned (a forming candle
          never reaches MFE/MAE or target-flag math);
        * windows needing more than one page are paginated through the
          client's bounded ``get_klines_range`` when available — a window
          larger than one page is NEVER silently truncated to the latest
          1000 candles and treated as complete;
        * a failed / limiter-denied / truncated fetch returns ``[]`` with
          the classified status on ``self._last_fetch_result`` so the cycle
          can fail closed (no outcome write) instead of guessing.
        """
        result = fetch_candles_for_evaluation(
            self.client, symbol, from_time, to_time,
        )
        self._last_fetch_result = result
        if not result.ok:
            logger.warning(
                "shadow evaluation: candle source %s for %s (%s) — "
                "evaluation deferred, no outcome write",
                result.status, symbol, result.reason_code,
            )
            return []
        return list(result.candles)

    # ── single-signal incremental evaluation ──────────────────

    def _evaluate_signal_incremental(
        self,
        sig: dict,
        all_candles: list[Candle],
        now: datetime,
        stats: dict,
    ) -> None:
        """Evaluate a single signal, updating only mature horizons.

        EOD uses half-open interval [signal_time, next UTC midnight).
        Target flags at finalization are also capped to EOD boundary.

        Fail-closed: every horizon write and the finalization write are
        gated by ``validate_candle_coverage`` on the exact window the frozen
        formulas read — an unproven window writes nothing and stays pending.
        """
        signal_time: datetime = sig["signal_time"]
        signal_price: float = sig["signal_price"]
        signal_id: int = sig["signal_id"]
        symbol: str = sig["symbol"]
        is_new = sig["outcome_id"] is None

        # Post-signal candles for time-horizon calculations
        signal_ts = int(signal_time.timestamp() * 1000)
        asof_ms = datetime_to_ms(now)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        # EOD boundary for this signal
        eod_excl = _get_eod_exclusive(signal_time)
        eod_excl_ms = int(eod_excl.timestamp() * 1000)

        updates: dict[str, Any] = {}

        # time-horizons
        for label, minutes in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if sig.get(eval_field) is not None:
                continue
            if not _is_horizon_mature(signal_time, minutes, now):
                continue

            # Fail-closed: prove the exact closed candle path of
            # [signal_time, signal_time + horizon) BEFORE computing.
            # Maturity alone is never proof that the price path is complete.
            horizon_cutoff_ms = int(
                (signal_time + timedelta(minutes=minutes)).timestamp() * 1000
            )
            coverage = validate_candle_coverage(
                all_candles,
                signal_ms=signal_ts,
                cutoff_ms=horizon_cutoff_ms,
                asof_ms=asof_ms,
                scope=f"HORIZON_{label}",
            )
            if not coverage.ok:
                record_coverage_block(stats, coverage, horizon=label)
                record_block_event(
                    stats,
                    event="coverage_blocked",
                    status=coverage.status,
                    reason_code=coverage.reason_code,
                    horizon=label,
                    signal_id=signal_id,
                    symbol=symbol,
                    experiment_id=EXPERIMENT_ID,
                )
                logger.warning(
                    "shadow evaluation: signal_id=%d symbol=%s horizon=%s "
                    "blocked status=%s reason_code=%s experiment_id=%s "
                    "— no evaluated_*_at write",
                    signal_id, symbol, label,
                    coverage.status, coverage.reason_code, EXPERIMENT_ID,
                )
                continue

            mfe, mae = _calculate_mfe_mae_for_window(
                post_candles, signal_price, minutes, signal_time,
            )
            updates[f"mfe_{label}"] = mfe
            updates[f"mae_{label}"] = mae
            updates[eval_field] = now

        # EOD — half-open interval capped at next UTC midnight
        if sig.get("evaluated_eod_at") is None and _is_eod_mature(signal_time, now):
            # Fail-closed: the EOD window must be fully proven as well.
            coverage = validate_candle_coverage(
                all_candles,
                signal_ms=signal_ts,
                cutoff_ms=eod_excl_ms,
                asof_ms=asof_ms,
                scope="HORIZON_EOD",
            )
            if not coverage.ok:
                record_coverage_block(stats, coverage, horizon="eod")
                record_block_event(
                    stats,
                    event="coverage_blocked",
                    status=coverage.status,
                    reason_code=coverage.reason_code,
                    horizon="eod",
                    signal_id=signal_id,
                    symbol=symbol,
                    experiment_id=EXPERIMENT_ID,
                )
                logger.warning(
                    "shadow evaluation: signal_id=%d symbol=%s horizon=eod "
                    "blocked status=%s reason_code=%s experiment_id=%s "
                    "— no evaluated_eod_at write",
                    signal_id, symbol,
                    coverage.status, coverage.reason_code, EXPERIMENT_ID,
                )
            else:
                mfe, mae = _calculate_mfe_mae_for_window(
                    post_candles, signal_price, None, signal_time,
                    eod_exclusive=eod_excl,
                )
                updates["mfe_eod"] = mfe
                updates["mae_eod"] = mae
                updates["evaluated_eod_at"] = now

        # finalization — effective state = sig OR updates
        all_done = all(
            sig.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )
        all_done = all_done and (
            sig.get("evaluated_eod_at") is not None or "evaluated_eod_at" in updates
        )

        if all_done and not sig.get("is_final"):
            # Fail-closed (FR-04): the frozen target-flag verdict reads
            # exactly [signal_time, EOD) — stored horizon marks alone are
            # not proof that THIS window is complete.
            coverage = validate_candle_coverage(
                all_candles,
                signal_ms=signal_ts,
                cutoff_ms=eod_excl_ms,
                asof_ms=asof_ms,
                scope="TARGET_FLAGS_EOD",
            )
            if not coverage.ok:
                record_finalization_block(stats, coverage)
                record_block_event(
                    stats,
                    event="finalization_blocked",
                    status=coverage.status,
                    reason_code=coverage.reason_code,
                    horizon="eod",
                    signal_id=signal_id,
                    symbol=symbol,
                    experiment_id=EXPERIMENT_ID,
                )
                logger.warning(
                    "shadow evaluation: signal_id=%d symbol=%s finalization "
                    "blocked status=%s reason_code=%s experiment_id=%s "
                    "— is_final stays false",
                    signal_id, symbol,
                    coverage.status, coverage.reason_code, EXPERIMENT_ID,
                )
            else:
                # Target flags capped to EOD boundary
                target_flags = _check_target_achievement(
                    post_candles, signal_price, eod_exclusive=eod_excl,
                )
                updates.update(target_flags)
                updates["is_final"] = True

        if not updates:
            return

        saved = self.repo.save_outcome_partial(signal_id=signal_id, symbol=symbol, **updates)

        if not saved:
            stats["errors"] += 1
            return

        for label, _ in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if eval_field in updates and sig.get(eval_field) is None:
                stats["horizons_updated"][label] += 1
        if "evaluated_eod_at" in updates and sig.get("evaluated_eod_at") is None:
            stats["horizons_updated"]["eod"] += 1

        if is_new:
            stats["outcomes_created"] += 1
        else:
            stats["outcomes_updated"] += 1

        if updates.get("is_final"):
            stats["finalized"] += 1

    # ── full cycle ────────────────────────────────────────────

    def run_evaluation_cycle(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = now

        eligible = self.repo.get_eligible_signals(limit=5000)

        by_symbol: dict[str, list[dict]] = {}
        for s in eligible:
            by_symbol.setdefault(s["symbol"], []).append(s)

        stats: dict[str, Any] = {
            "start_time": start.isoformat(),
            "signals_checked": len(eligible),
            "symbols_processed": 0,
            "horizons_updated": {h: 0 for h, _ in HORIZONS} | {"eod": 0},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
            # Fail-closed diagnostics (in-memory only; NO schema change).
            "source_blocked": {
                "source_fetch_error": 0,
                "source_incomplete": 0,
                "source_not_yet_available": 0,
            },
            "coverage_blocked": {},
            "horizons_pending": {h: 0 for h, _ in HORIZONS} | {"eod": 0},
            "finalization_blocked": 0,
        }

        for symbol, signals in by_symbol.items():
            stats["symbols_processed"] += 1
            oldest = min(s["signal_time"] for s in signals)
            candles = self._get_candles_for_symbol(symbol, oldest, now)
            if not candles:
                # Fail-closed: no candles without a SOURCE_VALIDATED fetch.
                # The signals stay eligible (retry) and no outcome is touched.
                result = getattr(self, "_last_fetch_result", None)
                if result is not None and not result.ok:
                    status = result.status
                    status_key = fetch_status_stats_key(status)
                    reason_code = result.reason_code
                else:
                    status = SOURCE_INCOMPLETE_COVERAGE
                    status_key = fetch_status_stats_key(status)
                    reason_code = "NO_CANDLES_IN_WINDOW"
                stats["source_blocked"][status_key] = (
                    stats["source_blocked"].get(status_key, 0) + len(signals)
                )
                stats["errors"] += len(signals)
                for s in signals:
                    record_block_event(
                        stats,
                        event="source_blocked",
                        status=status,
                        reason_code=reason_code,
                        horizon=None,
                        signal_id=s.get("signal_id"),
                        symbol=symbol,
                        experiment_id=EXPERIMENT_ID,
                    )
                logger.warning(
                    "shadow evaluation: symbol=%s source_blocked=%d "
                    "status=%s reason_code=%s experiment_id=%s "
                    "— no outcome write, signals stay retry-eligible",
                    symbol, len(signals), status, reason_code, EXPERIMENT_ID,
                )
                continue
            for s in signals:
                try:
                    self._evaluate_signal_incremental(s, candles, now, stats)
                except Exception:
                    stats["errors"] += 1
                    logger.exception("Error evaluating signal %d", s["signal_id"])

        stats["end_time"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Shadow evaluation: checked=%d symbols=%d created=%d updated=%d "
            "finalized=%d errors=%d source_blocked=%s coverage_blocked=%s "
            "finalization_blocked=%d",
            stats["signals_checked"], stats["symbols_processed"],
            stats["outcomes_created"], stats["outcomes_updated"],
            stats["finalized"], stats["errors"],
            stats["source_blocked"], stats["coverage_blocked"],
            stats["finalization_blocked"],
        )
        logger.info("Horizons: %s", stats["horizons_updated"])
        if stats["coverage_blocked"] or stats["finalization_blocked"] or any(
            stats["source_blocked"].values()
        ):
            logger.warning(
                "Shadow evaluation fail-closed blocks: experiment_id=%s "
                "source_blocked=%s coverage_blocked=%s horizons_pending=%s "
                "finalization_blocked=%d events=%d",
                EXPERIMENT_ID, stats["source_blocked"],
                stats["coverage_blocked"], stats["horizons_pending"],
                stats["finalization_blocked"],
                len(stats.get("diagnostics", [])),
            )
        return stats

    # ── lifecycle ─────────────────────────────────────────────

    def start(self, interval_seconds: int = 300) -> None:
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("Received signal %d, stopping evaluator…", signum)
            self._running = False

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)
        logger.info("Starting shadow evaluator (interval=%ds)", interval_seconds)

        while self._running:
            try:
                self.run_evaluation_cycle()
            except Exception:
                logger.exception("Evaluation cycle failed")
            if self._running:
                time.sleep(interval_seconds)
        logger.info("Shadow evaluator stopped")


# ── CLI ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shadow evaluator — incremental MFE/MAE by horizon",
    )
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    settings = load_settings(args.config)
    db_repo = ScannerRepository(
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password, backend="postgres",
    )
    if not db_repo._use_pg:
        logger.error("PostgreSQL required")
        sys.exit(1)

    shadow_repo = ShadowSignalRepository(db_repo._conn)
    client = BybitClient(settings)
    evaluator = ShadowSignalEvaluator(client, shadow_repo)

    if args.once:
        summary = evaluator.run_evaluation_cycle()
        print("\n" + "=" * 80)
        print("SHADOW EVALUATION CYCLE COMPLETE")
        print("=" * 80)
        print(f"Signals checked:    {summary['signals_checked']}")
        print(f"Symbols processed:  {summary['symbols_processed']}")
        print(f"Horizons updated:   {summary['horizons_updated']}")
        print(f"Outcomes created:   {summary['outcomes_created']}")
        print(f"Outcomes updated:   {summary['outcomes_updated']}")
        print(f"Finalized:          {summary['finalized']}")
        print(f"Errors:             {summary['errors']}")
        # Fail-closed diagnostics (present since the coverage guard; .get()
        # keeps this CLI working with summaries produced by older callers).
        print(f"Source blocked:     {summary.get('source_blocked')}")
        print(f"Coverage blocked:   {summary.get('coverage_blocked')}")
        print(f"Finalization blocked: {summary.get('finalization_blocked')}")
        print("=" * 80)
    else:
        evaluator.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
