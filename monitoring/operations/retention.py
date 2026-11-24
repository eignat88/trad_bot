#!/usr/bin/env python3
"""Bounded housekeeping for independent read-only operations monitoring data.

Dry run is the default. --apply deletes only records older than 30 days from
monitoring.api_metric_sec and monitoring.service_sample. Journal cursors and
source gap audit evidence are never touched.
"""
import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys
import time

DATABASE = "trad_bot"
RETENTION_DAYS = 30
TABLES = (
    ("api_metric_sec", "event_second"),
    ("service_sample", "sampled_at"),
)
LOCK_FILE = "/tmp/tradbot_monitor_retention.lock"


def pg(sql):
    result = subprocess.run(
        ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", DATABASE],
        input=sql, text=True, capture_output=True, timeout=20, check=True,
    )
    return result.stdout.strip()


def sql_identifiers(table):
    for pair in TABLES:
        if pair[0] == table:
            return pair[0], pair[1]
    raise ValueError("Unknown retention table")


def inspect_sql(table, cutoff):
    table, column = sql_identifiers(table)
    return (
        f"SELECT COUNT(*)::bigint, "
        f"COALESCE(MIN({column})::text, '') "
        f"FROM monitoring.{table} "
        f"WHERE {column} < TIMESTAMPTZ '{cutoff}';"
    )


def delete_sql(table, cutoff, batch_size):
    table, column = sql_identifiers(table)
    if not 1 <= batch_size <= 5000:
        raise ValueError("Batch size out of range")
    return (
        "BEGIN; "
        "SET LOCAL lock_timeout = '1s'; "
        "SET LOCAL statement_timeout = '8s'; "
        "WITH candidates AS ("
        f" SELECT ctid FROM monitoring.{table}"
        f" WHERE {column} < TIMESTAMPTZ '{cutoff}'"
        f" ORDER BY {column} ASC LIMIT {batch_size}"
        " FOR UPDATE SKIP LOCKED"
        "), removed AS ("
        f" DELETE FROM monitoring.{table} AS t"
        " USING candidates AS c WHERE t.ctid = c.ctid"
        " RETURNING 1"
        ") SELECT COUNT(*)::bigint FROM removed; "
        "COMMIT;"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete expired rows (dry-run by default)")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--max-batches", type=int, default=80)
    parser.add_argument("--max-seconds", type=float, default=55.0)
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 5000:
        parser.error("batch-size must be 1..5000")
    if not 1 <= args.max_batches <= 200:
        parser.error("max-batches must be 1..200")
    if not 5 <= args.max_seconds <= 300:
        parser.error("max-seconds must be 5..300")

    # One invocation only; never let two maintenance sessions overlap.
    with open(LOCK_FILE, "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("RETENTION_SKIPPED another instance already running", flush=True)
            return 0
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=RETENTION_DAYS)).isoformat()
        start = time.monotonic()
        print(f"RETENTION_START mode={'APPLY' if args.apply else 'DRY_RUN'} "
              f"cutoff={cutoff} batch_size={args.batch_size} "
              f"max_batches={args.max_batches}", flush=True)
        total_deleted = 0
        batches = 0
        for table, _ in TABLES:
            inspected = pg(inspect_sql(table, cutoff)).split("|", 1)
            if len(inspected) != 2:
                raise RuntimeError("Unexpected database inspection result")
            print(f"RETENTION_INSPECT table={table} expired_rows={inspected[0]} "
                  f"oldest_expired={inspected[1] or 'none'}", flush=True)
            if not args.apply:
                continue
            deleted_here = 0
            while batches < args.max_batches and time.monotonic() - start < args.max_seconds:
                output = pg(delete_sql(table, cutoff, args.batch_size))
                count = int(output)
                deleted_here += count
                total_deleted += count
                batches += 1
                if count < args.batch_size:
                    break
                time.sleep(0.15)
            print(f"RETENTION_RESULT table={table} deleted={deleted_here}", flush=True)
        print(f"RETENTION_DONE deleted={total_deleted} batches={batches} "
              f"elapsed_s={time.monotonic()-start:.2f}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
        print(f"RETENTION_ERROR type={type(exc).__name__}", file=sys.stderr,
              flush=True)
        sys.exit(1)
