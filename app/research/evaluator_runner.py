"""Research Evaluator Runner — standalone CLI entrypoint for the background evaluator.

Runs as trad-bot-research-evaluator.service — completely independent from
the scanner process.  Evaluates BOTH in a SINGLE unified scheduler loop:
  1. Generic research experiments (research.research_signal → research.research_outcome)
  2. Prospective OOS experiments (research.prospective_observation → research.prospective_outcome)

Architecture:
  One top-level scheduler loop loads experiment lists dynamically each cycle,
  runs all generic evaluations, then all prospective evaluations, then sleeps.
  No blocking .start() calls — both experiment types share the same cycle.

Usage:
    python -m app.research.evaluator_runner --once
    python -m app.research.evaluator_runner --interval-seconds 300
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.research.me_reverse_long_v3.scheduler_route import (
    route_prospective_experiment,
)

logger = logging.getLogger("research_evaluator")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research Evaluator — unified generic + prospective scheduler",
    )
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
    parser.add_argument(
        "--enable-me-v3-evaluator",
        action="store_true",
        default=False,
        help="Explicit opt-in for V3 research outcomes",
    )
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    from app.config import load_settings
    from app.db.repository import ScannerRepository
    from app.exchange.bybit_client import BybitClient
    from app.research.evaluator import ResearchEvaluator
    from app.research.prospective_evaluator import ProspectiveOOSEvaluator
    from app.research.repository import ResearchRepository
    from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
        BybitHistoricalCandleSource,
        SrrShortExecutionRExpansionProspectiveEvaluator,
    )

    settings = load_settings(args.config)
    db_repo = ScannerRepository(
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password, backend="postgres",
    )
    if not db_repo._use_pg:
        logger.error("PostgreSQL required for research evaluator")
        sys.exit(1)

    client = BybitClient(settings)
    evaluator = ResearchEvaluator(conn=db_repo._conn, client=client)

    # Prospective evaluator: injected with ResearchRepository (not BybitClient)
    research_repo = ResearchRepository(db_repo._conn)
    from contextlib import ExitStack
    from app.research.srr_short_runtime_wiring import prepare_srr_short_writer
    from app.research.srr_short_writer_activation_boundary_v1 import (
        SrrShortWriterActivationMode,
    )

    srr_writer_cfg = settings.srr_short_writer
    with ExitStack() as srr_writer_stack:
        srr_writer = srr_writer_stack.enter_context(
            prepare_srr_short_writer(
                reader_conn=db_repo._conn,
                host=settings.db_host,
                port=settings.db_port,
                database=settings.db_name,
                user=settings.db_user,
                password=settings.db_password,
                enable_writes=srr_writer_cfg.enabled,
                activation_ts=srr_writer_cfg.activation_ts or None,
                activation_mode=SrrShortWriterActivationMode(
                    enabled=srr_writer_cfg.enabled
                ),
            )
        )

        # Only the SRR-specific evaluator can leave dry-run mode. All other
        # prospective experiments remain on the unchanged observe-only path.
        srr_eval = SrrShortExecutionRExpansionProspectiveEvaluator(
            conn=db_repo._conn,
            candle_source=BybitHistoricalCandleSource(client),
            dry_run=not srr_writer_cfg.enabled,
            write_outcomes=srr_writer.write if srr_writer is not None else None,
        )

        prospective_eval = ProspectiveOOSEvaluator(
            conn=db_repo._conn, client=client, repo=research_repo,
            srr_policy_router=srr_eval,
        )

        from app.research.me_reverse_long_v3.activation import (
            resolve_startup_v3_activation,
        )
        from app.research.me_reverse_long_v3.evaluator import (
            MEReverseLongV3ExecutionEvaluator,
        )
        from app.research.me_reverse_long_v3.historical_source import (
            make_v3_historical_source,
        )
        import json
        from pathlib import Path

        # V3 activation is startup-bound by design.
        # After a separately approved registry/DB activation, restart
        # the evaluator with explicit CLI opt-in.
        # Per-cycle guard still stops evaluation after deactivation.
        v3_evaluator = None
        if args.enable_me_v3_evaluator:
            v3_registry_path = (
                Path(__file__).parent / "prospective_registry.json"
            )
            v3_startup_activation = resolve_startup_v3_activation(
                conn=db_repo._conn,
                registry_path=v3_registry_path,
                explicitly_enabled=True,
            )
            if v3_startup_activation is not None:
                from app.research.me_reverse_long_v3.guarded_evaluator import (
                    GuardedV3Evaluator,
                )
                v3_evaluator = GuardedV3Evaluator(
                    conn=db_repo._conn,
                    evaluator=MEReverseLongV3ExecutionEvaluator(
                        db_repo._conn,
                        make_v3_historical_source(client),
                    ),
                    registry_path=(
                        Path(__file__).parent / "prospective_registry.json"
                    ),
                    explicitly_enabled=True,
                    startup_activation=v3_startup_activation,
                )

                logger.info(
                    "ME V3 evaluator activated at startup; "
                    "later activation requires evaluator restart"
                )

        if args.enable_me_v3_evaluator and v3_evaluator is None:
            logger.warning(
                "ME V3 evaluator inactive at startup; "
                "activation requires explicit evaluator restart"
            )

        if args.once:
            _run_single_cycle(db_repo._conn, evaluator, prospective_eval, v3_evaluator)
            return

        # ── Daemon mode: unified scheduler loop ────────────────────
        _run_scheduler_loop(
            conn=db_repo._conn,
            evaluator=evaluator,
            prospective_eval=prospective_eval,
            v3_evaluator=v3_evaluator,
            interval_seconds=args.interval_seconds,
        )


def _run_single_cycle(
    conn: Any,
    evaluator: ResearchEvaluator,
    prospective_eval: ProspectiveOOSEvaluator,
    v3_evaluator: Any | None = None,
) -> None:
    """Execute one evaluation cycle for BOTH generic + prospective, then exit."""
    # ── Generic ──
    experiment_ids = _load_active_experiments(conn)
    if experiment_ids:
        logger.info("Active generic experiments: %s", experiment_ids)
        for exp_id in experiment_ids:
            summary = evaluator.run_evaluation_cycle(exp_id)
            print(f"\n{'=' * 80}")
            print(f"RESEARCH EVALUATION CYCLE — {exp_id}")
            print(f"{'=' * 80}")
            print(f"Signals checked:    {summary['signals_checked']}")
            print(f"Symbols processed:  {summary['symbols_processed']}")
            print(f"Horizons updated:   {summary['horizons_updated']}")
            print(f"Outcomes created:   {summary['outcomes_created']}")
            print(f"Outcomes updated:   {summary['outcomes_updated']}")
            print(f"Finalized:          {summary['finalized']}")
            print(f"Errors:             {summary['errors']}")
            print(f"{'=' * 80}")
    else:
        logger.warning("No active generic research experiments found")

    # ── Prospective ──
    prospective_ids = _load_prospective_experiments(conn)
    if prospective_ids:
        logger.info("Active prospective experiments: %s", prospective_ids)
        for exp_id in prospective_ids:
            summary = route_prospective_experiment(
                exp_id, prospective_eval,
                v3_evaluator=v3_evaluator,
                v3_enabled=v3_evaluator is not None,
            )
            print(f"\n{'=' * 80}")
            print(f"PROSPECTIVE EVALUATION CYCLE — {exp_id}")
            print(f"{'=' * 80}")
            print(f"Signals checked:    {summary['signals_checked']}")
            print(f"Horizons updated:   {summary['horizons_updated']}")
            print(f"Finalized:          {summary['finalized']}")
            print(f"Errors:             {summary['errors']}")
            print(f"{'=' * 80}")
    else:
        logger.info("No active prospective experiments found")

    if not experiment_ids and not prospective_ids:
        logger.warning("No experiments (generic or prospective) found")
        print("No experiments.")


def _run_scheduler_loop(
    conn: Any,
    evaluator: ResearchEvaluator,
    prospective_eval: ProspectiveOOSEvaluator,
    interval_seconds: int = 300,
    v3_evaluator: Any | None = None,
) -> None:
    """Unified daemon scheduler: generic + prospective in one loop.

    Each iteration:
      1. Reload experiment lists from DB (dynamic status changes)
      2. Run generic evaluations
      3. Run prospective evaluations
      4. Interruptible sleep
    """
    shutdown = threading.Event()
    running = True

    def _stop(signum: int, frame: Any) -> None:
        nonlocal running
        logger.info("research evaluator: received signal %d, stopping", signum)
        running = False
        shutdown.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "research evaluator started (unified loop): interval=%ds",
        interval_seconds,
    )

    cycle = 0
    while running:
        cycle += 1

        # ── Generic research experiments ──────────────────────
        experiment_ids = _load_active_experiments(conn)
        if experiment_ids:
            logger.info("Active generic experiments: %s", experiment_ids)
        for exp_id in experiment_ids:
            if not running:
                break
            try:
                evaluator.run_evaluation_cycle(exp_id)
            except Exception:
                logger.exception(
                    "research evaluation cycle failed for %s", exp_id,
                )

        # ── Prospective OOS experiments ───────────────────────
        prospective_ids = _load_prospective_experiments(conn)
        if prospective_ids:
            logger.info("Active prospective experiments: %s", prospective_ids)
        for exp_id in prospective_ids:
            if not running:
                break
            try:
                stats = route_prospective_experiment(
                    exp_id, prospective_eval,
                v3_evaluator=v3_evaluator,
                v3_enabled=v3_evaluator is not None,
                )
                logger.info(
                    "prospective evaluation [%s]: checked=%d finalized=%d errors=%d",
                    exp_id, stats["signals_checked"],
                    stats["finalized"], stats["errors"],
                )
            except Exception:
                logger.exception(
                    "prospective evaluation cycle failed for %s", exp_id,
                )

        if not experiment_ids and not prospective_ids:
            logger.info(
                "cycle #%d: no active experiments (generic or prospective)",
                cycle,
            )

        # ── Interruptible wait ────────────────────────────────
        if running:
            shutdown.wait(timeout=interval_seconds)

    logger.info("research evaluator stopped")


def _load_active_experiments(conn) -> list[str]:
    """Load active experiment IDs from research.research_experiment."""
    if not conn:
        return []
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT experiment_id FROM research.research_experiment "
            "WHERE status = 'ACTIVE' ORDER BY experiment_id"
        )
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.exception("Failed to load active experiments")
        return []


def _load_prospective_experiments(conn) -> list[str]:
    """Load prospective experiment IDs with status='RUNNING'.

    Returns RUNNING experiments and only V3 in DRAINING, with started_at.
    Experiments with started_at=NULL or status != 'RUNNING' are NOT evaluated.
    Direction-level lifecycle does NOT affect experiment selection — a shared
    experiment remains in the active list even if some directions are closed,
    because existing immature outcomes must continue to mature.
    """
    if not conn:
        return []
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT experiment_id FROM research.prospective_experiment "
            "WHERE started_at IS NOT NULL AND ("
            "status = 'RUNNING' OR ("
            "experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' "
            "AND status = 'DRAINING')) "
            "ORDER BY experiment_id"
        )
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.debug("prospective_experiment table not found or empty")
        return []


if __name__ == "__main__":
    main()
