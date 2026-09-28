"""Research Evaluator Runner — standalone CLI entrypoint for the background evaluator.

Runs as trad-bot-research-evaluator.service — completely independent from
the scanner process.  Evaluates BOTH:
  1. Generic research experiments (research.research_signal → research.research_outcome)
  2. Prospective OOS experiments (research.prospective_observation → research.prospective_outcome)

Usage:
    python -m app.research.evaluator_runner --once
    python -m app.research.evaluator_runner --interval-seconds 300
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("research_evaluator")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generic Research Evaluator — multi-horizon MFE/MAE/TP/SL",
    )
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
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

    # ── Generic research experiments ────────────────────────────
    experiment_ids = _load_active_experiments(db_repo._conn)
    if experiment_ids:
        logger.info("Active generic experiments: %s", experiment_ids)

        if args.once:
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
            evaluator.start(
                experiment_ids=experiment_ids,
                interval_seconds=args.interval_seconds,
            )
    else:
        logger.warning("No active generic research experiments found")

    # ── Prospective OOS experiments ─────────────────────────────
    prospective_ids = _load_prospective_experiments(db_repo._conn)
    if prospective_ids:
        logger.info("Active prospective experiments: %s", prospective_ids)

        from app.research.prospective_evaluator import ProspectiveOOSEvaluator
        prospective_eval = ProspectiveOOSEvaluator(conn=db_repo._conn, client=client)

        if args.once:
            for exp_id in prospective_ids:
                summary = prospective_eval.run_evaluation_cycle(exp_id)
                print(f"\n{'=' * 80}")
                print(f"PROSPECTIVE EVALUATION CYCLE — {exp_id}")
                print(f"{'=' * 80}")
                print(f"Signals checked:    {summary['signals_checked']}")
                print(f"Horizons updated:   {summary['horizons_updated']}")
                print(f"Finalized:          {summary['finalized']}")
                print(f"Errors:             {summary['errors']}")
                print(f"{'=' * 80}")
        else:
            # Run prospective evaluation in the same loop as generic
            prospective_eval.start(
                experiment_ids=prospective_ids,
                interval_seconds=args.interval_seconds,
            )
    else:
        logger.info("No active prospective experiments found")

    if not experiment_ids and not prospective_ids:
        logger.warning("No experiments (generic or prospective) found")
        if args.once:
            print("No experiments.")
        sys.exit(0)


def _load_active_experiments(conn) -> list[str]:
    """Load active experiment IDs from research.research_experiment."""
    if not conn:
        return []
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT experiment_id FROM research.research_experiment WHERE status = 'ACTIVE' ORDER BY experiment_id"
        )
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.exception("Failed to load active experiments")
        return []


def _load_prospective_experiments(conn) -> list[str]:
    """Load active prospective experiment IDs.

    Returns experiments with status='RUNNING' (started_at IS NOT NULL).
    Experiments with started_at=NULL are NOT evaluated yet.
    """
    if not conn:
        return []
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT experiment_id FROM research.prospective_experiment "
            "WHERE status = 'RUNNING' AND started_at IS NOT NULL "
            "ORDER BY experiment_id"
        )
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.debug("prospective_experiment table not found or empty")
        return []


if __name__ == "__main__":
    main()
