"""Issue #296: isolated prospective research observer runner."""

from __future__ import annotations

import argparse
import logging
import os
import time

import psycopg2

from .observer import EntryQualityObserver
from .repository import PgObserverRepository


LOG = logging.getLogger("me_eq296")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate runner arguments without DB connection",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one polling cycle",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=500,
    )

    args = parser.parse_args()

    if not 5 <= args.interval <= 3600:
        parser.error("interval must be between 5 and 3600")

    if not 1 <= args.batch_limit <= 1000:
        parser.error("batch-limit must be between 1 and 1000")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.check_config:
        print("RUNNER_CONFIG_PASS")
        return 0

    dsn = os.environ.get("ME_EQ296_DSN")

    if not dsn:
        LOG.error("ME_EQ296_DSN_NOT_CONFIGURED")
        return 2

    try:
        conn = psycopg2.connect(
            dsn,
            application_name="me_eq296_source_observer",
            connect_timeout=5,
        )
    except psycopg2.Error:
        LOG.exception("DATABASE_CONNECTION_FAILED")
        return 2

    try:
        conn.autocommit = False
        repo = PgObserverRepository(conn)
        observer = EntryQualityObserver(
            repo,
            batch_limit=args.batch_limit,
        )

        while True:
            try:
                result = observer.run_once()
                LOG.info(
                    "OBSERVER_CYCLE status=%s seen=%d "
                    "inserted=%d duplicates=%d invalid=%d",
                    result.status,
                    result.source_rows_seen,
                    result.events_inserted,
                    result.duplicates,
                    result.invalid,
                )

                if result.status in {
                    "NOT_ACTIVATED",
                    "NOT_RUNNING",
                    "MISSING_FREEZE_TS",
                }:
                    return 3

                if result.status == "ALREADY_RUNNING":
                    LOG.warning("OBSERVER_CONCURRENT_INSTANCE")
                    if args.once:
                        return 4
                    time.sleep(args.interval)
                    continue

            except Exception:
                conn.rollback()
                LOG.exception("OBSERVER_CYCLE_FAILED")
                return 1

            if args.once:
                return 0

            time.sleep(args.interval)

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
