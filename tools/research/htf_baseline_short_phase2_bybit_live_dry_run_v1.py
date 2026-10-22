"""Issue #181 — bounded live-source dry-run for the Phase-2 Bybit candle source.

RESEARCH_ONLY / OBSERVE_ONLY.  This tool proves the live Bybit source is safe
for a continuous prospective OOS observation WITHOUT touching any production
state:

* candle input: Bybit public READ-ONLY endpoints only (``/v5/market/time``,
  ``/v5/market/kline``), bounded pagination with an explicit request budget;
* persistence: ``InMemoryWriter`` plus a throwaway checkpoint file — never a
  database, never the production state store;
* audits: bounded coverage, causal ASOF (no decision past the verified ASOF),
  checkpoint/restart equivalence, per-symbol fail-closed status, and — with
  ``--recovery-simulate-downtime-bars`` — a bounded historical backfill
  recovery simulation (uninterrupted vs crash/restart/backfill on the same
  historical set).

Fail-closed verdict (review FIX-2): the overall verdict can be PASS only when
EVERY requested symbol processed with complete coverage, ``source_errors == 0``,
``symbols_blocked == 0``, ``quarantine_bars == 0`` and all audits pass.  The
success of one symbol never masks the failure of another; every violation is
reported as a concrete ``readiness_blockers`` entry with a reason code and the
symbol.

Production activation stays BLOCKED behind gate #181: this tool never starts a
service, never writes OOS observations and never applies migrations 074/075.

Usage (from the repository root)::

    python tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py \
        --symbols BTCUSDT,ETHUSDT --warmup-bars 400 \
        --recovery-simulate-downtime-bars 3000 \
        --report docs/research/HTF_BASELINE_SHORT_PHASE2_BYBIT_SOURCE_V1_DRY_RUN_REPORT.json

Exit codes: 0 = overall PASS, 1 = FAIL, 3 = not runnable.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    DEFAULT_GAP_RECOVERY_ATTEMPTS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BACKFILL_BARS,
    DEFAULT_MAX_GAP_WINDOWS,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_LIMIT,
    DEFAULT_RETRY_BACKOFF_S,
    BybitPhase2CandleSource,
    CandleSourceError,
)
from app.research.htf_baseline_short_phase2_contracts_v1 import BAR_MS
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    EXIT_NOT_RUNNABLE,
    EXIT_OK,
    InMemoryWriter,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
)
from app.research.htf_baseline_short_phase2_state_store_v1 import read_checkpoint

log = logging.getLogger("htf.phase2.bybit.dry_run")

DEFAULT_REPORT = (
    "docs/research/HTF_BASELINE_SHORT_PHASE2_BYBIT_SOURCE_V1_DRY_RUN_REPORT.json"
)
DEFAULT_FREEZE_TS = "2025-01-01T00:00:00Z"

STATUS_OK = "OK"
STATUS_SOURCE_BLOCKED = "SOURCE_BLOCKED"
STATUS_QUARANTINED = "QUARANTINED"
STATUS_NOT_PROCESSED = "NOT_PROCESSED"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CountingClient:
    """Read-only proxy that tallies every public request across all loads."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def _public_get(self, endpoint: str, **params):
        self.calls.append((endpoint, dict(params)))
        return self._inner._public_get(endpoint, **params)


def causal_asof_audit(rows: list[dict], evaluation_asof_ms: int) -> dict:
    """No observation may look past the verified evaluation ASOF."""
    failures: list[str] = []
    for row in rows:
        known_at = int(row["known_at_ms"])
        first_touch = int(row["first_touch_open_ms"])
        available = int(row["level_available_ms"])
        if known_at > evaluation_asof_ms:
            failures.append(f"{row['source_event_id']}: known_at past ASOF")
        if known_at % BAR_MS:
            failures.append(f"{row['source_event_id']}: known_at unaligned")
        if first_touch % BAR_MS:
            failures.append(f"{row['source_event_id']}: first_touch unaligned")
        if not (available <= first_touch < known_at):
            failures.append(f"{row['source_event_id']}: noncausal level/touch/confirm order")
    return {
        "checked_events": len(rows),
        "evaluation_asof_ms": evaluation_asof_ms,
        "failures": failures,
        "pass": not failures,
    }


# ------------------------------------------------------------ FIX-2 readiness
def build_symbol_status(
    symbols,
    metrics: dict,
    coverage_reports: list[dict],
    cursors: dict,
    symbols_state: dict,
) -> dict[str, dict]:
    """Per-symbol readiness status; one symbol's success never hides another."""
    status: dict[str, dict] = {}
    for symbol in symbols:
        reports = [r for r in (coverage_reports or []) if r.get("symbol") == symbol]
        blocked = [r for r in reports if r.get("action") == "SOURCE_BLOCKED"]
        quarantined = [r for r in reports if r.get("action") == "QUARANTINED"]
        cursor = (cursors or {}).get(symbol)
        processed = bool(cursor) or symbol in (symbols_state or {})
        entry: dict = {
            "status": STATUS_OK,
            "processed": processed,
            "coverage": "COMPLETE" if processed and not reports else "INCOMPLETE",
            "cursor_last_processed_open_ms": (
                cursor.get("last_processed_open_ms") if cursor else None
            ),
            "reason": None,
        }
        if blocked:
            entry.update(
                status=STATUS_SOURCE_BLOCKED,
                coverage="INCOMPLETE",
                reason=blocked[-1].get("reason"),
            )
        elif quarantined:
            entry.update(
                status=STATUS_QUARANTINED,
                coverage="INCOMPLETE",
                reason=quarantined[-1].get("reason"),
            )
        elif not processed:
            entry.update(
                status=STATUS_NOT_PROCESSED,
                coverage="INCOMPLETE",
                reason="no bars processed for this symbol",
            )
        status[symbol] = entry
    return status


def evaluate_readiness(
    *,
    requested_symbols,
    symbol_status: dict,
    metrics: dict,
    audits: dict,
    evaluation_asof_ms: int | None,
    recovery_status: dict | None = None,
) -> tuple[str, list[dict]]:
    """Fail-closed readiness verdict. PASS requires EVERY condition to hold.

    Returns ``(overall_verdict, readiness_blockers)``; each blocker names a
    concrete reason code and the affected symbol.
    """
    blockers: list[dict] = []

    def block(code: str, symbol, detail) -> None:
        blockers.append({"code": code, "symbol": symbol, "detail": detail})

    for symbol in requested_symbols:
        entry = (symbol_status or {}).get(symbol) or {
            "status": STATUS_NOT_PROCESSED,
            "coverage": "INCOMPLETE",
            "reason": "no symbol status available",
        }
        status = entry.get("status")
        reason = entry.get("reason")
        if status == STATUS_SOURCE_BLOCKED:
            block(f"SOURCE_BLOCKED_{symbol}", symbol, reason)
        elif status == STATUS_QUARANTINED:
            block(f"QUARANTINED_{symbol}", symbol, reason)
        elif status == STATUS_NOT_PROCESSED:
            block(f"SYMBOL_NOT_PROCESSED_{symbol}", symbol, reason)
        if entry.get("coverage") != "COMPLETE":
            block(f"INCOMPLETE_COVERAGE_{symbol}", symbol, reason or entry.get("coverage"))

    if int(metrics.get("source_errors", 0) or 0) > 0:
        block("SOURCE_ERRORS_PRESENT", None, f"source_errors={metrics['source_errors']}")
    if int(metrics.get("symbols_blocked", 0) or 0) > 0:
        block("SYMBOLS_BLOCKED_PRESENT", None, f"symbols_blocked={metrics['symbols_blocked']}")
    if int(metrics.get("quarantine_bars", 0) or 0) > 0:
        block("QUARANTINE_BARS_PRESENT", None, f"quarantine_bars={metrics['quarantine_bars']}")
    if int(metrics.get("coverage_gap_bars", 0) or 0) > 0:
        block("COVERAGE_GAP_BARS_PRESENT", None, f"coverage_gap_bars={metrics['coverage_gap_bars']}")
    if evaluation_asof_ms is None:
        block("MISSING_VERIFIED_ASOF", None, "no verified evaluation ASOF")

    for name, code in (
        ("causal_asof", "CAUSAL_ASOF_FAILED"),
        ("bounded_requests", "REQUEST_BUDGET_EXCEEDED"),
        ("checkpoint_restart_equivalence", "RESTART_EQUIVALENCE_BROKEN"),
    ):
        audit = (audits or {}).get(name) or {}
        if not audit.get("pass"):
            block(code, None, audit.get("failures") or name)

    if recovery_status is not None and not recovery_status.get("pass", False):
        block("RECOVERY_EQUIVALENCE_BROKEN", None, recovery_status.get("failures") or "recovery simulation")

    return ("PASS" if not blockers else "FAIL"), blockers


# --------------------------------------------------------- recovery simulation
def _event_key(row: dict) -> tuple:
    return (
        row["source_event_id"],
        row["episode_id"],
        row["symbol"],
        row["first_touch_open_ms"],
        row["known_at_ms"],
    )


def run_recovery_simulation(
    *,
    symbols,
    boundary,
    make_source,
    full_history,
    split_index: int,
    warmup_bars: int,
    evaluation_asof_ms: int,
    tmp_dir: Path,
    max_cycles: int = 24,
) -> dict:
    """Uninterrupted vs crash/restart/backfill on one identical historical set.

    Both scenarios consume the SAME bounded window sequence planned from the
    durable timestamp cursor, so the comparison is exact: any difference in
    ``source_event_id`` / ``episode_id`` / ``first_touch_open_ms`` /
    ``known_at_ms`` is a real defect, not window drift.
    """
    result: dict = {"split_index": split_index, "scenarios": {}, "failures": []}
    sets: dict[str, set] = {}
    rows_by_scenario: dict[str, list] = {}
    for scenario in ("uninterrupted", "interrupted"):
        config = RunnerConfig(
            symbols=(symbols[0],),
            checkpoint_path=str(tmp_dir / f"recovery_{scenario}.json"),
            warmup_bars=warmup_bars,
            once=True,
            dry_run=True,
        )
        writer = InMemoryWriter()
        runner = Phase2ObserverRunner(
            config, boundary=boundary, source=make_source(), writer=writer
        )
        backfill_batches = 0
        runner.process_symbol_bars(
            symbols[0],
            full_history[:split_index],
            evaluation_asof_ms=evaluation_asof_ms,
        )
        runner.save_checkpoint()
        cycles = 0
        interrupted_done = False
        while cycles < max_cycles:
            cycles += 1
            before = runner.metrics.bars_processed
            code = runner.run_once()
            if code != EXIT_OK:
                result["failures"].append(f"{scenario}: cycle exit {code}")
                break
            if runner.metrics.bars_processed == before:
                break  # caught up with the verified ASOF
            if scenario == "interrupted" and not interrupted_done:
                # crash after one batch of the backfill: drop the runner, the
                # checkpoint is the only durable state the restart may use
                backfill_batches = runner.metrics.backfill_batches
                runner = Phase2ObserverRunner(
                    config, boundary=boundary, source=make_source(), writer=writer
                )
                interrupted_done = True
        else:
            result["failures"].append(f"{scenario}: not caught up in {max_cycles} cycles")
        bars = runner.metrics.bars_processed
        rows_by_scenario[scenario] = writer.rows
        sets[scenario] = {_event_key(row) for row in writer.rows}
        result["scenarios"][scenario] = {
            "cycles": cycles,
            "bars_processed": bars,
            "events": len(writer.rows),
            "backfill_batches": runner.metrics.backfill_batches,
            "backfill_bars": runner.metrics.backfill_bars,
            "emissions_rejected": runner.metrics.emissions_rejected,
            "duplicate_suppressions": runner.metrics.duplicate_suppressions,
        }
        if scenario == "interrupted":
            result["scenarios"][scenario]["batches_before_crash"] = backfill_batches

    uninterrupted, interrupted = sets["uninterrupted"], sets["interrupted"]
    ids_a = [row["source_event_id"] for row in rows_by_scenario["uninterrupted"]]
    ids_b = [row["source_event_id"] for row in rows_by_scenario["interrupted"]]
    comparison = {
        "uninterrupted_events": len(uninterrupted),
        "interrupted_events": len(interrupted),
        "identical_source_event_ids": {k[0] for k in uninterrupted} == {k[0] for k in interrupted},
        "identical_episode_ids": {k[1] for k in uninterrupted} == {k[1] for k in interrupted},
        "identical_first_touch_open_ms": {k[3] for k in uninterrupted} == {k[3] for k in interrupted},
        "identical_known_at_ms": {k[4] for k in uninterrupted} == {k[4] for k in interrupted},
        "duplicate_first_confirm": len(ids_b) - len(set(ids_b)),
        "missed_events": sorted(k[0] for k in (uninterrupted - interrupted))[:10],
        "extra_events": sorted(k[0] for k in (interrupted - uninterrupted))[:10],
    }
    comparison["pass"] = (
        not comparison["duplicate_first_confirm"]
        and not comparison["missed_events"]
        and not comparison["extra_events"]
        and comparison["identical_source_event_ids"]
        and comparison["identical_episode_ids"]
    )
    result["equivalence"] = comparison
    result["pass"] = comparison["pass"] and not result["failures"]
    return result


# ------------------------------------------------------------------- main flow
def main(argv: list[str] | None = None, *, client_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", default="BTCUSDT")
    parser.add_argument("--evaluation-asof-ms", type=int, default=None)
    parser.add_argument("--min-history-bars", type=int, default=3_000)
    parser.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--retry-backoff-s", type=float, default=DEFAULT_RETRY_BACKOFF_S)
    parser.add_argument("--warmup-bars", type=int, default=400)
    parser.add_argument("--freeze-ts", default=DEFAULT_FREEZE_TS)
    parser.add_argument(
        "--recovery-simulate-downtime-bars",
        type=int,
        default=0,
        help="run the backfill recovery simulation with this simulated downtime (bars)",
    )
    parser.add_argument(
        "--recovery-window-bars",
        type=int,
        default=1_200,
        help="steady window (bars) used by the recovery simulation sources",
    )
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="throwaway checkpoint path (default: a fresh temporary directory)",
    )
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    symbols = tuple(s for s in str(args.symbols).split(",") if s)
    if not symbols:
        log.error("--symbols must name at least one symbol")
        return EXIT_NOT_RUNNABLE

    if client_factory is None:
        from app.config import Settings
        from app.exchange.bybit_client import BybitClient

        def client_factory():
            return CountingClient(BybitClient(Settings()))

    report: dict = {
        "tool": "HTF_BASELINE_SHORT_PHASE2_BYBIT_LIVE_DRY_RUN_V1",
        "issue": 181,
        "generated_at": _utc_now(),
        "mode": "RESEARCH_ONLY/OBSERVE_ONLY",
        "production_db_writes": 0,
        "oos_activation": "BLOCKED (gate #181)",
        "bounds": {
            "symbols": list(symbols),
            "min_history_bars": args.min_history_bars,
            "page_limit": args.page_limit,
            "max_pages": args.max_pages,
            "max_attempts": args.max_attempts,
            "retry_backoff_s": args.retry_backoff_s,
            "warmup_bars": args.warmup_bars,
        },
    }

    tmp_dir = Path(tempfile.mkdtemp(prefix="htf_phase2_bybit_dry_run_"))
    checkpoint_path = Path(args.checkpoint) if args.checkpoint else tmp_dir / "state.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    # recovery-sim checkpoints live beside the main checkpoint so every write
    # stays inside the sanctioned workspace (sandbox-safe)
    sim_dir = checkpoint_path.parent / "recovery_sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    try:
        config = RunnerConfig(
            symbols=symbols,
            checkpoint_path=str(checkpoint_path),
            warmup_bars=int(args.warmup_bars),
            once=True,
            dry_run=True,
        )
        source = BybitPhase2CandleSource(
            client_factory(),
            evaluation_asof_ms=args.evaluation_asof_ms,
            page_limit=int(args.page_limit),
            max_pages=int(args.max_pages),
            min_history_bars=int(args.min_history_bars),
            max_attempts=int(args.max_attempts),
            retry_backoff_s=float(args.retry_backoff_s),
        )
        boundary = build_boundary(
            experiment_id=config.experiment_id,
            protocol_freeze_ts=args.freeze_ts,
            registry_freeze_ts=args.freeze_ts,
            database_freeze_ts=args.freeze_ts,
            protocol_fingerprint_hex=config.protocol_fingerprint,
        )
    except Exception as exc:
        log.error("dry-run not runnable: %s", exc)
        report["verdict"] = "NOT_RUNNABLE"
        report["overall_verdict"] = "NOT_RUNNABLE"
        report["error"] = str(exc)
        _write_report(args.report, report)
        return EXIT_NOT_RUNNABLE

    writer = InMemoryWriter()
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    source_error: str | None = None
    try:
        code = runner.run_once()
    except CandleSourceError as exc:
        code = EXIT_NOT_RUNNABLE
        source_error = f"{exc.reason}: {exc.detail}"
    report["cycle1_exit_code"] = code
    report["cycle1_metrics"] = runner.metrics.as_dict()
    report["source_error"] = source_error

    evaluation_asof_ms = getattr(source, "last_evaluation_asof_ms", None)
    report["evaluation_asof_ms"] = evaluation_asof_ms
    report["server_time_ms"] = getattr(source, "last_server_time_ms", None)
    report["load_metrics"] = getattr(source, "last_load_metrics", {})
    report["request_count"] = len(getattr(source.client, "calls", []))

    audits: dict[str, dict] = {}
    if code == EXIT_OK and evaluation_asof_ms is not None:
        # 1) causal ASOF: nothing past the verified ASOF, causal order per event
        audits["causal_asof"] = causal_asof_audit(writer.rows, int(evaluation_asof_ms))

        # 2) bounded requests: strict per-load budget (probe + pages + gap recovery)
        per_load = (
            int(args.max_attempts) * (1 + int(args.max_pages))
            + DEFAULT_MAX_GAP_WINDOWS * DEFAULT_GAP_RECOVERY_ATTEMPTS
        )
        budget = 2 * len(symbols) * per_load  # cycle 1 + restart-equivalence cycle
        audits["bounded_requests"] = {
            "request_count": report["request_count"],
            "budget": budget,
            "per_load_budget": per_load,
            "loads": 2 * len(symbols),
            "pass": report["request_count"] <= budget,
        }

        # 3) checkpoint/restart equivalence: a restart reprocesses nothing and
        #    re-emits nothing (at-least-once delivery with idempotent writes).
        #    The restart is pinned to the cycle-1 verified ASOF: live market
        #    movement between the two cycles must not be counted as a reprocess.
        state_before = read_checkpoint(config.checkpoint_path)
        restart_writer = InMemoryWriter()
        restart_source = BybitPhase2CandleSource(
            source.client,
            evaluation_asof_ms=int(evaluation_asof_ms),
            page_limit=int(args.page_limit),
            max_pages=int(args.max_pages),
            min_history_bars=int(args.min_history_bars),
            max_attempts=int(args.max_attempts),
            retry_backoff_s=float(args.retry_backoff_s),
        )
        restarted = Phase2ObserverRunner(
            config, boundary=boundary, source=restart_source, writer=restart_writer
        )
        restart_code = restarted.run_once()
        state_after = read_checkpoint(config.checkpoint_path)
        audits["checkpoint_restart_equivalence"] = {
            "restart_exit_code": restart_code,
            "bars_reprocessed": restarted.metrics.bars_processed,
            "emissions_on_restart": len(restart_writer.rows),
            "state_unchanged": all(
                state_before.get(key) == state_after.get(key)
                for key in ("symbols", "emitted", "known_levels", "cursors", "warmup_done")
            ),
            "pass": restart_code == EXIT_OK
            and restarted.metrics.bars_processed == 0
            and not restart_writer.rows,
        }

        report["observed_events_total"] = len(writer.rows)
        report["observed_events"] = [
            {
                "source_event_id": row["source_event_id"],
                "episode_id": row["episode_id"],
                "symbol": row["symbol"],
                "level_id": row["level_id"],
                "first_touch_open_ms": row["first_touch_open_ms"],
                "known_at_ms": row["known_at_ms"],
                "warmup_origin": row.get("warmup_origin"),
            }
            for row in writer.rows[:25]  # cap the report; the full list is the checkpoint
        ]
    else:
        audits["causal_asof"] = {"pass": False, "failures": ["no verified evaluation ASOF"]}
        audits["bounded_requests"] = {"pass": False, "failures": ["cycle failed"]}
        audits["checkpoint_restart_equivalence"] = {
            "pass": False,
            "failures": ["cycle failed"],
        }

    # ------------------------------------------- recovery simulation (FIX-1)
    recovery_status: dict | None = None
    downtime_bars = int(args.recovery_simulate_downtime_bars)
    if code == EXIT_OK and evaluation_asof_ms is not None and downtime_bars > 0:
        window_bars = int(args.recovery_window_bars)
        full_history_bars = downtime_bars + 2 * window_bars
        sim_max_pages = max(
            int(args.max_pages), full_history_bars // int(args.page_limit) + 2
        )

        def make_sim_source(min_history_bars=window_bars):
            return BybitPhase2CandleSource(
                client_factory(),
                evaluation_asof_ms=int(evaluation_asof_ms),
                page_limit=int(args.page_limit),
                max_pages=sim_max_pages,
                min_history_bars=int(min_history_bars),
                max_backfill_bars=DEFAULT_MAX_BACKFILL_BARS,
                max_attempts=int(args.max_attempts),
                retry_backoff_s=float(args.retry_backoff_s),
            )

        full_history = make_sim_source(full_history_bars).load(symbols[0])
        split_index = max(1, len(full_history) - downtime_bars)
        if split_index <= int(args.warmup_bars):
            recovery_status = {
                "pass": False,
                "failures": [
                    f"split_index {split_index} <= warmup_bars {args.warmup_bars}"
                ],
            }
        else:
            recovery_status = run_recovery_simulation(
                symbols=(symbols[0],),
                boundary=boundary,
                make_source=make_sim_source,
                full_history=full_history,
                split_index=split_index,
                warmup_bars=int(args.warmup_bars),
                evaluation_asof_ms=int(evaluation_asof_ms),
                tmp_dir=sim_dir,
            )
            recovery_status["history_bars"] = len(full_history)
            recovery_status["downtime_bars"] = downtime_bars
            recovery_status["window_bars"] = window_bars
    report["recovery_status"] = recovery_status or {
        "simulated": False,
        "pass": True,
        "note": "recovery simulation not requested",
    }

    # ------------------------------------- FIX-2: fail-closed readiness verdict
    state = read_checkpoint(config.checkpoint_path)
    symbol_status = build_symbol_status(
        symbols,
        report["cycle1_metrics"],
        state.get("coverage_reports", []),
        state.get("cursors", {}),
        state.get("symbols", {}),
    )
    coverage_status = {
        "complete": [s for s in symbols if symbol_status[s]["coverage"] == "COMPLETE"],
        "incomplete": [s for s in symbols if symbol_status[s]["coverage"] != "COMPLETE"],
        "blocked": [s for s in symbols if symbol_status[s]["status"] == STATUS_SOURCE_BLOCKED],
        "quarantined": [s for s in symbols if symbol_status[s]["status"] == STATUS_QUARANTINED],
    }
    overall_verdict, readiness_blockers = evaluate_readiness(
        requested_symbols=symbols,
        symbol_status=symbol_status,
        metrics=report["cycle1_metrics"],
        audits=audits,
        evaluation_asof_ms=evaluation_asof_ms,
        recovery_status=recovery_status,
    )

    report["audits"] = audits
    report["symbol_status"] = symbol_status
    report["coverage_status"] = coverage_status
    report["source_errors"] = report["cycle1_metrics"]["source_errors"]
    report["symbols_blocked"] = report["cycle1_metrics"]["symbols_blocked"]
    report["quarantine_bars"] = report["cycle1_metrics"]["quarantine_bars"]
    report["readiness_blockers"] = readiness_blockers
    report["overall_verdict"] = overall_verdict
    report["verdict"] = overall_verdict
    _write_report(args.report, report)
    log.info("dry-run verdict: %s (blockers: %d)", overall_verdict, len(readiness_blockers))
    exit_code = EXIT_OK if overall_verdict == "PASS" else 1
    print(f"verdict={overall_verdict} exit_code={exit_code}")
    return exit_code


def _write_report(path: str, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    log.info("report written: %s", target)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
