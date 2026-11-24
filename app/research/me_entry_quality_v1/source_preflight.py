"""Issue #296: read-only validation of existing V3 source events."""

from collections import Counter
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from .contracts import (
    SourceValidationError,
    validate_source_event,
)

SOURCE_TABLE = "dds.me_long_v2_inverse_short_v3_event"


def run() -> None:
    conn = psycopg2.connect(
        dbname="trad_bot",
        user="postgres",
        application_name="me_eq_296_readonly_preflight",
    )

    try:
        conn.set_session(readonly=True, autocommit=False)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '10s'")
            cur.execute(
                f"""
                SELECT
                    event_key,
                    symbol,
                    signal_time,
                    created_at,
                    captured_at,
                    source_scanner,
                    source_scanner_version,
                    source_direction,
                    source_setup_id,
                    capture_route,
                    entry_timeframe,
                    signal_candle_open_time,
                    source_reference_price,
                    lineage
                FROM {SOURCE_TABLE}
                ORDER BY created_at, event_key
                LIMIT 200
                """
            )
            rows = cur.fetchall()

        if not rows:
            print("SOURCE_EMPTY")
            return

        # Diagnostic-only boundary, deliberately preceding all existing
        # rows. This is NOT the prospective experiment freeze.
        diagnostic_freeze = datetime(
            2026, 1, 1, tzinfo=timezone.utc
        )

        valid = 0
        errors = Counter()

        for row in rows:
            try:
                validate_source_event(
                    row,
                    freeze_ts=diagnostic_freeze,
                )
                valid += 1
            except (SourceValidationError, KeyError, TypeError, ValueError) as exc:
                errors[str(exc)] += 1

        print(f"SOURCE_ROWS={len(rows)}")
        print(f"VALID={valid}")
        print(f"INVALID={sum(errors.values())}")
        print(f"ERRORS={dict(errors)}")

        if errors:
            raise SystemExit("SOURCE_PREFLIGHT_FAILED")

        print("SOURCE_PREFLIGHT_PASS")

    finally:
        conn.rollback()
        conn.close()


if __name__ == "__main__":
    run()
