"""Standalone, fail-closed CLI for the V2 research-only evaluator.

Usage:
    python -m app.research.me_r_long_early_mae_v2.runner --once
    python -m app.research.me_r_long_early_mae_v2.runner --interval-seconds 300

This entrypoint never imports or calls paper/live engines, direction gates or
production exit handlers.
"""
from __future__ import annotations

import argparse
import logging
import time

from .evaluator import MeEarlyMaeV2Evaluator
from .repository import MeEarlyMaeV2Repository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    from app.config import load_settings
    from app.db.repository import ScannerRepository

    settings = load_settings(args.config)
    repository = ScannerRepository(
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password, backend="postgres",
    )
    if not repository._use_pg:
        raise SystemExit("PostgreSQL required")

    v2_repository = MeEarlyMaeV2Repository(repository._conn)
    v2_repository.register_protocol()
    evaluator = MeEarlyMaeV2Evaluator(v2_repository)

    while True:
        stats = evaluator.run_cycle()
        logging.getLogger("me_early_mae_v2").info("cycle: %s", stats)
        if args.once:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
