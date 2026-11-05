"""SRR LONG Research Outcome Evaluator — multi-horizon MFE/MAE + R normalisation.

Evaluates dds.srr_research_signal outcomes on 5 time horizons:
  15m, 30m, 60m, 120m, 240m

For each horizon computes:
  - MFE / MAE (as %)
  - MFE_R / MAE_R (normalised to 1R = abs(entry - stop))
  - TP hit / SL hit / TP-before-SL / SL-before-TP

Candle source: the shared ``app.research.candle_coverage_guard_v1`` (ISSUE244,
``OOS_GENERIC_CANDLE_COVERAGE_FAIL_CLOSED_V1``) — no silent 1000-candle
truncation, bounded pagination for large windows, classified source statuses
(``SOURCE_VALIDATED`` / ``SOURCE_FETCH_ERROR`` / ``SOURCE_INCOMPLETE_COVERAGE``
/ ``SOURCE_NOT_YET_AVAILABLE``) and NO fallback request after a Global Limiter
deny.

Fail-closed contract (ISSUE244)::

    INCOMPLETE CANDLE COVERAGE
        -> NO evaluated_*_at WRITE
        -> NO is_final

Before every ``evaluated_<horizon>_at`` mark the complete closed 5m candle path
of ``[signal_time, signal_time + horizon)`` is proven; before ``is_final=True``
the full TP/SL path to 240m is proven separately.  An unproven horizon writes
nothing, stays pending and is re-evaluated on the next cycle; previously
finalized outcomes are never rewritten automatically.

No lookahead: only candles within [signal_time, signal_time + horizon) are used.
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
from app.research.candle_coverage_guard_v1 import (
    DIAGNOSTIC_BLOCK_EVENTS,
    DIAGNOSTIC_EVENT_FIELDS,
    MAX_DIAGNOSTIC_EVENTS,
    SOURCE_INCOMPLETE_COVERAGE,
    datetime_to_ms,
    fetch_candles_for_evaluation,
    fetch_status_stats_key,
    record_block_event,
    record_coverage_block,
    record_finalization_block,
    validate_candle_coverage,
)

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "SRR_LONG_OUTCOME_V1"

HORIZONS: list[tuple[str, int]] = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]

# TP/SL verdict window: the frozen SRR checker scans 240 minutes.
TPSL_WINDOW_MINUTES = 240

# ── diagnostics contract (ISSUE244) ────────────────────────────────
#
# Structured, in-memory diagnostics ONLY.  The event vocabulary
# (``DIAGNOSTIC_BLOCK_EVENTS`` / ``DIAGNOSTIC_EVENT_FIELDS`` /
# ``MAX_DIAGNOSTIC_EVENTS``) and the bounded recorder
# (``record_block_event``) are owned by the shared guard
# ``app.research.candle_coverage_guard_v1`` and re-exported here because
# they are part of this evaluator's module contract.  Reason codes are
# byte-stable: the agreed vocabulary for a FUTURE persistence marker on
# ``dds.srr_research_outcome``.  Persisting them (e.g. a nullable
# ``block_reason``/``source_status`` column) requires a separately agreed
# schema and its own PostgreSQL migration PR — until that agreement exists
# this evaluator writes NO diagnostic column and runs NO DML beyond the
# frozen outcome upsert.


def _record_block_event(
    stats: dict[str, Any],
    *,
    event: str,
    status: str,
    reason_code: str,
    horizon: str | None = None,
    signal_id: int | None = None,
    symbol: str | None = None,
    experiment_id: str = EXPERIMENT_ID,
) -> None:
    """Append one structured fail-closed block event to the cycle stats.

    Thin delegate to the shared guard recorder: this module keeps its own
    experiment default while the counters stay exact and the event list
    stays bounded (``MAX_DIAGNOSTIC_EVENTS``).
    """
    record_block_event(
        stats,
        event=event,
        status=status,
        reason_code=reason_code,
        horizon=horizon,
        signal_id=signal_id,
        symbol=symbol,
        experiment_id=experiment_id,
    )


def _calculate_long_mfe_mae(
    candles: list[Candle],
    entry_price: float,
    max_minutes: int,
    signal_time: datetime,
) -> tuple[float | None, float | None]:
    """Calculate MFE/MAE for LONG signal within a time window.

    LONG: MFE = max(favorable) = max(high - entry) / entry * 100
          MAE = max(adverse)   = max(entry - low) / entry * 100
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
        # LONG: favorable = price going up (high - entry), adverse = price going down (entry - low)
        fav = (c.high - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        adv = (entry_price - c.low) / entry_price * 100 if entry_price > 0 else 0.0
        max_favorable = max(max_favorable, fav)
        max_adverse = max(max_adverse, adv)

    if not found:
        return None, None
    return max_favorable, max_adverse


def _check_long_target_sl(
    candles: list[Candle],
    entry_price: float,
    invalidation_price: float,
    target_1: float,
    max_minutes: int,
    signal_time: datetime,
) -> dict[str, bool]:
    """Check TP/SL hit sequence for LONG within time window.

    Returns: {tp_hit, sl_hit, tp_before_sl, sl_before_tp}
    """
    result = {"tp_hit": False, "sl_hit": False, "tp_before_sl": False, "sl_before_tp": False}

    signal_ts = int(signal_time.timestamp() * 1000)
    cutoff_ts = int((signal_time + timedelta(minutes=max_minutes)).timestamp() * 1000)

    tp_seen = False
    sl_seen = False

    for c in candles:
        if c.timestamp <= signal_ts:
            continue
        if c.timestamp >= cutoff_ts:
            break

        # LONG: TP = high >= target_1, SL = low <= invalidation
        if not tp_seen and c.high >= target_1:
            tp_seen = True
            result["tp_hit"] = True
        if not sl_seen and c.low <= invalidation_price:
            sl_seen = True
            result["sl_hit"] = True

        # First hit sequence
        if tp_seen and not sl_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["tp_before_sl"] = True
        if sl_seen and not tp_seen and not result["tp_before_sl"] and not result["sl_before_tp"]:
            result["sl_before_tp"] = True

    return result


class SRRResearchEvaluator:
    """Multi-horizon incremental evaluator for SRR research signals.

    Fail-closed candle coverage (ISSUE244)::

        INCOMPLETE CANDLE COVERAGE
            -> NO evaluated_*_at WRITE
            -> NO is_final

    A horizon mark is written only after its exact ``[signal_time,
    signal_time + horizon)`` 5-minute candle path has been proven complete
    (first and last required candle present, no gaps, duplicates, off-grid or
    unordered timestamps, every required candle closed, valid OHLC).
    ``is_final`` additionally requires a proven 240m TP/SL path.  A blocked
    horizon stays pending and is re-evaluated on a later cycle.
    """

    def __init__(self, conn: Any, client: BybitClient) -> None:
        self._conn = conn
        self.client = client
        self._running = False
        # Classified outcome of the most recent candle fetch (structured
        # diagnostics: SOURCE_FETCH_ERROR vs SOURCE_INCOMPLETE_COVERAGE).
        self._last_fetch_result: Any = None

    # ── candle fetch ──────────────────────────────────────────

    def _get_candles_for_symbol(
        self, symbol: str, from_time: datetime, to_time: datetime,
    ) -> list[Candle]:
        """Fetch 5m candles covering [from_time, to_time].

        Fail-closed wrapper around ``fetch_candles_for_evaluation``
        (ISSUE244):

        * only a ``SOURCE_VALIDATED`` fetch yields candles — an API/limiter
          error, a truncated pagination or an empty response yields ``[]``,
          so a caller can never evaluate against a partial window;
        * windows larger than one Bybit page (1000 candles) are read through
          the bounded ``BybitClient.get_klines_range`` pagination (same retry
          budget and shared limiter as every other request) — the latest-N
          response is never silently truncated and then treated as complete;
        * after a Global Limiter deny there is NO fallback request: the
          classified failure is returned as-is and retried on a later cycle;
        * the classified status is kept on ``self._last_fetch_result`` for
          the caller's structured diagnostics and logging.
        """
        result = fetch_candles_for_evaluation(
            self.client, symbol, from_time, to_time,
        )
        self._last_fetch_result = result
        if not result.ok:
            logger.warning(
                "srr evaluation: candle source %s for %s (%s) — "
                "evaluation deferred, no outcome write",
                result.status, symbol, result.reason_code,
            )
            return []
        return list(result.candles)

    # ── eligible signals query ────────────────────────────────

    def get_eligible_signals(self, limit: int = 5000) -> list[dict]:
        """Get signals needing at least one horizon evaluation.

        Also re-selects NON-final rows whose horizon marks are all present
        but whose finalization gate was blocked, so a blocked verdict stays
        retry-eligible until the frozen TP/SL path is proven (FR-06).
        Finalized rows are never selected.
        """
        if not self._conn:
            return []

        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT
                s.signal_id, s.symbol, s.signal_time, s.reference_price,
                s.invalidation_price, s.target_1,
                o.signal_id AS outcome_id,
                o.evaluated_15m_at, o.evaluated_30m_at, o.evaluated_60m_at,
                o.evaluated_120m_at, o.evaluated_240m_at,
                o.is_final
            FROM dds.srr_research_signal s
            LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
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
                    -- Fail-closed finalization retry (FR-06): a non-final
                    -- row whose horizon marks are ALL present but whose
                    -- finalization gate was blocked stays eligible until
                    -- the frozen TP/SL path is proven.  Rows without a
                    -- target can never finalize (frozen rule) and stay
                    -- dormant; finalized rows never match this branch.
                    OR (o.evaluated_15m_at IS NOT NULL
                        AND o.evaluated_30m_at IS NOT NULL
                        AND o.evaluated_60m_at IS NOT NULL
                        AND o.evaluated_120m_at IS NOT NULL
                        AND o.evaluated_240m_at IS NOT NULL
                        AND s.target_1 IS NOT NULL)
                 ))
              )
            ORDER BY s.signal_time ASC
            LIMIT %s
            """,
            (EXPERIMENT_ID, limit),
        )
        rows = cursor.fetchall()
        return [
            {
                "signal_id": r[0], "symbol": r[1], "signal_time": r[2],
                "entry_price": float(r[3]), "invalidation_price": float(r[4]),
                "target_1": float(r[5]) if r[5] is not None else None,
                "outcome_id": r[6],
                "evaluated_15m_at": r[7], "evaluated_30m_at": r[8],
                "evaluated_60m_at": r[9], "evaluated_120m_at": r[10],
                "evaluated_240m_at": r[11], "is_final": r[12],
            }
            for r in rows
        ]

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
        invalidation_price: float = sig["invalidation_price"]
        target_1 = sig["target_1"]
        signal_id: int = sig["signal_id"]
        symbol: str = sig["symbol"]
        is_new = sig["outcome_id"] is None

        # 1R = abs(entry - stop) for R-normalisation
        risk_1r = abs(entry_price - invalidation_price)
        if risk_1r <= 0:
            risk_1r = entry_price * 0.01  # fallback: 1% of entry

        signal_ts = int(signal_time.timestamp() * 1000)
        asof_ms = datetime_to_ms(now)
        post_candles = [c for c in all_candles if c.timestamp > signal_ts]

        updates: dict[str, Any] = {}

        # time-horizons
        for label, minutes in HORIZONS:
            eval_field = f"evaluated_{label}_at"
            if sig.get(eval_field) is not None:
                continue
            if not (now >= signal_time + timedelta(minutes=minutes)):
                continue

            # Fail-closed (ISSUE244): prove the exact candle path for this
            # horizon BEFORE computing anything.  Maturity alone is never
            # proof that the price path is complete.
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
                _record_block_event(
                    stats,
                    event="coverage_blocked",
                    status=coverage.status,
                    reason_code=coverage.reason_code,
                    horizon=label,
                    signal_id=signal_id,
                    symbol=symbol,
                    experiment_id=sig.get("experiment_id", EXPERIMENT_ID),
                )
                logger.warning(
                    "srr evaluation: signal_id=%d symbol=%s horizon=%s "
                    "blocked status=%s reason_code=%s experiment_id=%s "
                    "— no evaluated_*_at write",
                    signal_id, symbol, label,
                    coverage.status, coverage.reason_code,
                    sig.get("experiment_id", EXPERIMENT_ID),
                )
                continue

            # MFE/MAE in %
            mfe_pct, mae_pct = _calculate_long_mfe_mae(
                post_candles, entry_price, minutes, signal_time,
            )

            # R-normalised
            mfe_r = round(mfe_pct / (risk_1r / entry_price * 100), 4) if mfe_pct is not None and risk_1r > 0 else None
            mae_r = round(mae_pct / (risk_1r / entry_price * 100), 4) if mae_pct is not None and risk_1r > 0 else None

            updates[f"mfe_{label}"] = mfe_pct
            updates[f"mae_{label}"] = mae_pct
            updates[f"mfe_r_{label}"] = mfe_r
            updates[f"mae_r_{label}"] = mae_r
            updates[eval_field] = now

        # finalization
        all_done = all(
            sig.get(f"evaluated_{h}_at") is not None or f"evaluated_{h}_at" in updates
            for h, _ in HORIZONS
        )

        if all_done and not sig.get("is_final") and target_1 is not None:
            # Fail-closed (ISSUE244): the TP/SL verdict is only trustworthy
            # when the full 240m path is proven.  Stored horizon marks (or
            # maturity) are not proof by itself.
            tpsl_cutoff_ms = int(
                (signal_time + timedelta(minutes=TPSL_WINDOW_MINUTES)).timestamp()
                * 1000
            )
            coverage = validate_candle_coverage(
                all_candles,
                signal_ms=signal_ts,
                cutoff_ms=tpsl_cutoff_ms,
                asof_ms=asof_ms,
                scope="TPSL_240M",
            )
            if not coverage.ok:
                record_finalization_block(stats, coverage)
                _record_block_event(
                    stats,
                    event="finalization_blocked",
                    status=coverage.status,
                    reason_code=coverage.reason_code,
                    horizon="240m",
                    signal_id=signal_id,
                    symbol=symbol,
                    experiment_id=sig.get("experiment_id", EXPERIMENT_ID),
                )
                logger.warning(
                    "srr evaluation: signal_id=%d symbol=%s finalization "
                    "blocked status=%s reason_code=%s experiment_id=%s "
                    "— is_final stays false",
                    signal_id, symbol,
                    coverage.status, coverage.reason_code,
                    sig.get("experiment_id", EXPERIMENT_ID),
                )
            else:
                # Check TP/SL at the longest horizon (240m)
                target_flags = _check_long_target_sl(
                    post_candles, entry_price, invalidation_price, target_1,
                    TPSL_WINDOW_MINUTES, signal_time,
                )
                updates.update(target_flags)
                updates["is_final"] = True

        if not updates:
            return

        saved = self._save_outcome_partial(signal_id, symbol, updates)
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

    # ── save outcome ──────────────────────────────────────────

    def _save_outcome_partial(
        self, signal_id: int, symbol: str, updates: dict[str, Any],
    ) -> bool:
        """Upsert outcome row with dynamic columns.

        Known outcome columns: mfe_*, mae_*, mfe_r_*, mae_r_*,
        evaluated_*_at, tp_hit, sl_hit, tp_before_sl, sl_before_tp, is_final.
        """
        if not self._conn:
            return False

        known_cols = {
            "mfe_15m", "mae_15m", "mfe_r_15m", "mae_r_15m", "evaluated_15m_at",
            "mfe_30m", "mae_30m", "mfe_r_30m", "mae_r_30m", "evaluated_30m_at",
            "mfe_60m", "mae_60m", "mfe_r_60m", "mae_r_60m", "evaluated_60m_at",
            "mfe_120m", "mae_120m", "mfe_r_120m", "mae_r_120m", "evaluated_120m_at",
            "mfe_240m", "mae_240m", "mfe_r_240m", "mae_r_240m", "evaluated_240m_at",
            "tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp",
            "is_final",
        }

        # Collect writeable columns from updates
        data_cols: list[str] = []
        data_vals: list[Any] = []
        for col, val in updates.items():
            if col in known_cols and val is not None:
                data_cols.append(col)
                data_vals.append(val)

        if not data_cols:
            return True

        # Always update updated_at
        all_cols = ["signal_id", "experiment_id", "symbol"] + data_cols + ["updated_at"]
        all_vals: list[Any] = [signal_id, EXPERIMENT_ID, symbol] + data_vals + [now()]
        placeholders = ["%s"] * len(all_cols)

        # UPDATE SET for non-PK columns
        update_parts = [f"{col} = EXCLUDED.{col}" for col in data_cols]
        update_parts.append("updated_at = EXCLUDED.updated_at")

        sql = f"""
            INSERT INTO dds.srr_research_outcome (
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
            logger.exception("Failed to save srr outcome for signal %d", signal_id)
            return False

    # ── full cycle ────────────────────────────────────────────

    def run_evaluation_cycle(self) -> dict[str, Any]:
        """Run one complete evaluation cycle."""
        now = datetime.now(timezone.utc)

        eligible = self.get_eligible_signals(limit=5000)

        by_symbol: dict[str, list[dict]] = {}
        for s in eligible:
            by_symbol.setdefault(s["symbol"], []).append(s)

        stats: dict[str, Any] = {
            "experiment_id": EXPERIMENT_ID,
            "start_time": now.isoformat(),
            "signals_checked": len(eligible),
            "symbols_processed": 0,
            "horizons_updated": {h: 0 for h, _ in HORIZONS},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
            # ISSUE244 structured diagnostics (in-memory only; no schema).
            "source_blocked": {
                "source_fetch_error": 0,
                "source_incomplete": 0,
                "source_not_yet_available": 0,
            },
            "coverage_blocked": {},
            "horizons_pending": {h: 0 for h, _ in HORIZONS},
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
                    _record_block_event(
                        stats,
                        event="source_blocked",
                        status=status,
                        reason_code=reason_code,
                        horizon=None,
                        signal_id=s.get("signal_id"),
                        symbol=symbol,
                    )
                logger.warning(
                    "srr evaluation: symbol=%s source_blocked=%d "
                    "status=%s reason_code=%s experiment_id=%s "
                    "— no outcome write, signals stay retry-eligible",
                    symbol, len(signals), status, reason_code, EXPERIMENT_ID,
                )
                continue
            for s in signals:
                try:
                    self._evaluate_signal(s, candles, now, stats)
                except Exception:
                    stats["errors"] += 1
                    logger.exception("Error evaluating srr signal %d", s["signal_id"])

        stats["end_time"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "SRR research evaluation: checked=%d symbols=%d created=%d updated=%d "
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
                "SRR research evaluation fail-closed blocks: "
                "experiment_id=%s source_blocked=%s coverage_blocked=%s "
                "horizons_pending=%s finalization_blocked=%d events=%d",
                EXPERIMENT_ID, stats["source_blocked"],
                stats["coverage_blocked"], stats["horizons_pending"],
                stats["finalization_blocked"],
                len(stats.get("diagnostics", [])),
            )
        return stats

    # ── lifecycle ─────────────────────────────────────────────

    def start(self, interval_seconds: int = 300) -> None:
        """Start continuous evaluation loop."""
        self._running = True

        def _stop(signum: int, frame: Any) -> None:
            logger.info("Received signal %d, stopping SRR evaluator…", signum)
            self._running = False

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)
        logger.info("Starting SRR research evaluator (interval=%ds)", interval_seconds)

        while self._running:
            try:
                self.run_evaluation_cycle()
            except Exception:
                logger.exception("SRR evaluation cycle failed")
            if self._running:
                time.sleep(interval_seconds)
        logger.info("SRR research evaluator stopped")


def now() -> datetime:
    """UTC now helper."""
    return datetime.now(timezone.utc)


# ── CLI ──────────────────────────────────────────────────────

def main() -> None:
    """CLI entrypoint for SRR research evaluator."""
    import argparse

    parser = argparse.ArgumentParser(
        description="SRR LONG Research Outcome Evaluator — incremental MFE/MAE by horizon",
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

    from app.config import load_settings
    from app.db.repository import ScannerRepository
    from app.exchange.bybit_client import BybitClient

    settings = load_settings(args.config)
    db_repo = ScannerRepository(
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password, backend="postgres",
    )
    if not db_repo._use_pg:
        logger.error("PostgreSQL required")
        sys.exit(1)

    client = BybitClient(settings)
    evaluator = SRRResearchEvaluator(conn=db_repo._conn, client=client)

    if args.once:
        summary = evaluator.run_evaluation_cycle()
        print("\n" + "=" * 80)
        print("SRR RESEARCH EVALUATION CYCLE COMPLETE")
        print("=" * 80)
        print(f"Signals checked:    {summary['signals_checked']}")
        print(f"Symbols processed:  {summary['symbols_processed']}")
        print(f"Horizons updated:   {summary['horizons_updated']}")
        print(f"Outcomes created:   {summary['outcomes_created']}")
        print(f"Outcomes updated:   {summary['outcomes_updated']}")
        print(f"Finalized:          {summary['finalized']}")
        print(f"Errors:             {summary['errors']}")
        print(f"Source blocked:     {summary['source_blocked']}")
        print(f"Coverage blocked:   {summary['coverage_blocked']}")
        print(f"Horizons pending:   {summary['horizons_pending']}")
        print(f"Finalization blocked: {summary['finalization_blocked']}")
        print("=" * 80)
    else:
        evaluator.start(interval_seconds=args.interval_seconds)


if __name__ == "__main__":
    main()
