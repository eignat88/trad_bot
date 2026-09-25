"""Research Evaluator Runner — standalone CLI entrypoint for the background evaluator.

Runs as trad-bot-research-evaluator.service — completely independent from
the scanner process.  Reads from research.research_signal, writes to
research.research_outcome.  No production tables touched.

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

    # Active experiments — read from research.research_experiment
    experiment_ids = _load_active_experiments(db_repo._conn)
    if not experiment_ids:
        logger.warning("No active research experiments found")
        if args.once:
            print("No active experiments.")
        sys.exit(0)

    logger.info("Active experiments: %s", experiment_ids)

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


if __name__ == "__main__":
    main()
