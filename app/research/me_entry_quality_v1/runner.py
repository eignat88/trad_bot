"""Issue #296: isolated prospective research observer runner.

Exit codes (deterministic; the systemd unit must not loop on them):

====  ==================================================================
Code  Meaning
====  ==================================================================
0     Cycle(s) completed normally.
1     A cycle failed unexpectedly (transient; transaction rolled back —
      nothing partial is ever committed).
2     Configuration or database connection failure.
3     Not activated / not running / missing freeze_ts.
4     Another instance holds the processing lock (--once mode).
5     Research contour BLOCKED (fail-closed after a deterministic source
      integrity or validation violation). Requires manual administrative
      recovery (deploy/sql/296_observer_recovery.sql). systemd is
      configured with RestartPreventExitStatus=5 so a deterministic
      violation never causes an infinite restart loop.
====  ==================================================================
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import psycopg2

from .observer import EntryQualityObserver
from .repository import PgObserverRepository


LOG = logging.getLogger("me_eq296")

EXIT_OK = 0
EXIT_CYCLE_FAILED = 1
EXIT_CONFIG = 2
EXIT_NOT_ACTIVATED = 3
EXIT_ALREADY_RUNNING = 4
EXIT_BLOCKED = 5


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
    parser.add_argument(
        "--audit-interval",
        type=int,
        default=300,
        help="Seconds between periodic source integrity audits",
    )
    parser.add_argument(
        "--coverage-gap-grace",
        type=int,
        default=900,
        help=(
            "Seconds an eligible event may stay unprocessed before it "
            "counts as a persistent COVERAGE_GAP"
        ),
    )

    args = parser.parse_args()

    if not 5 <= args.interval <= 3600:
        parser.error("interval must be between 5 and 3600")

    if not 1 <= args.batch_limit <= 1000:
        parser.error("batch-limit must be between 1 and 1000")

    if not 30 <= args.audit_interval <= 86400:
        parser.error("audit-interval must be between 30 and 86400")

    if not 0 <= args.coverage_gap_grace <= 86400:
        parser.error("coverage-gap-grace must be between 0 and 86400")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.check_config:
        print("RUNNER_CONFIG_PASS")
        return EXIT_OK

    dsn = os.environ.get("ME_EQ296_DSN")

    if not dsn:
        LOG.error("ME_EQ296_DSN_NOT_CONFIGURED")
        return EXIT_CONFIG

    try:
        conn = psycopg2.connect(
            dsn,
            application_name="me_eq296_source_observer",
            connect_timeout=5,
        )
    except psycopg2.Error:
        LOG.exception("DATABASE_CONNECTION_FAILED")
        return EXIT_CONFIG

    try:
        conn.autocommit = False
        repo = PgObserverRepository(conn)
        observer = EntryQualityObserver(
            repo,
            batch_limit=args.batch_limit,
            audit_interval_seconds=float(args.audit_interval),
            coverage_gap_grace_seconds=args.coverage_gap_grace,
        )

        while True:
            try:
                result = observer.run_once()
                LOG.info(
                    "OBSERVER_CYCLE status=%s seen=%d "
                    "inserted=%d duplicates=%d invalid=%d "
                    "audit=%s blocked_reason=%s",
                    result.status,
                    result.source_rows_seen,
                    result.events_inserted,
                    result.duplicates,
                    result.invalid,
                    result.audit_status,
                    result.blocked_reason,
                )

                if result.status in {
                    "NOT_ACTIVATED",
                    "NOT_RUNNING",
                    "MISSING_FREEZE_TS",
                }:
                    return EXIT_NOT_ACTIVATED

                if result.status == "BLOCKED":
                    # Deterministic fail-closed state: exit cleanly so
                    # systemd never loops on a validation violation.
                    LOG.error(
                        "OBSERVER_FAIL_CLOSED reason=%s",
                        result.blocked_reason,
                    )
                    return EXIT_BLOCKED

                if result.status == "ALREADY_RUNNING":
                    LOG.warning("OBSERVER_CONCURRENT_INSTANCE")
                    if args.once:
                        return EXIT_ALREADY_RUNNING
                    time.sleep(args.interval)
                    continue

            except Exception:
                conn.rollback()
                LOG.exception("OBSERVER_CYCLE_FAILED")
                return EXIT_CYCLE_FAILED

            if args.once:
                return EXIT_OK

            time.sleep(args.interval)

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
