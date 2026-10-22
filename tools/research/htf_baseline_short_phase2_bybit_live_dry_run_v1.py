"""Issue #181 — bounded live-source dry-run for the Phase-2 Bybit candle source.

RESEARCH_ONLY / OBSERVE_ONLY.  This tool proves the live Bybit source is safe
for a continuous prospective OOS observation WITHOUT touching any production
state:

* candle input: Bybit public READ-ONLY endpoints only (``/v5/market/time``,
  ``/v5/market/kline``), bounded pagination with an explicit request budget;
* persistence: ``InMemoryWriter`` plus a throwaway checkpoint file in a
  temporary directory — never a database, never the production state store;
* audits: bounded coverage, causal ASOF (no decision past the verified ASOF),
  checkpoint/restart equivalence (a restart never reprocesses or re-emits),
  and per-symbol fail-closed behaviour on source errors.

Production activation stays BLOCKED behind gate #181: this tool never starts a
service, never writes OOS observations and never applies migrations 074/075.

Usage (from the repository root)::

    python tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py \
        --symbols BTCUSDT --warmup-bars 400 \
        --report docs/research/HTF_BASELINE_SHORT_PHASE2_BYBIT_SOURCE_V1_DRY_RUN_REPORT.json

Exit codes: 0 = all audits PASS, 1 = any audit FAIL, 3 = not runnable.
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
    DEFAULT_MAX_ATTEMPTS,
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


def main(argv: list[str] | None = None) -> int:
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

    from app.config import Settings
    from app.exchange.bybit_client import BybitClient

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
    try:
        config = RunnerConfig(
            symbols=symbols,
            checkpoint_path=str(checkpoint_path),
            warmup_bars=int(args.warmup_bars),
            once=True,
            dry_run=True,
        )
        source = BybitPhase2CandleSource(
            CountingClient(BybitClient(Settings())),
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

        # 2) bounded requests: the request budget must hold for every symbol
        budget = (int(args.max_pages) + 16) * len(symbols)  # pages + gap retries + time probe
        audits["bounded_requests"] = {
            "request_count": report["request_count"],
            "budget": budget,
            "pass": report["request_count"] <= budget,
        }

        # 3) checkpoint/restart equivalence: a restart reprocesses nothing and
        #    re-emits nothing (at-least-once delivery with idempotent writes)
        state_before = read_checkpoint(config.checkpoint_path)
        restart_writer = InMemoryWriter()
        restarted = Phase2ObserverRunner(
            config, boundary=boundary, source=source, writer=restart_writer
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

    report["audits"] = audits
    report["verdict"] = "PASS" if all(a.get("pass") for a in audits.values()) else "FAIL"
    _write_report(args.report, report)
    log.info("dry-run verdict: %s (report: %s)", report["verdict"], args.report)
    exit_code = EXIT_OK if report["verdict"] == "PASS" else 1
    print(f"verdict={report['verdict']} exit_code={exit_code}")
    return exit_code


def _write_report(path: str, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    log.info("report written: %s", target)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
