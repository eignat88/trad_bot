"""Issue #296: prospective source coverage and integrity audit.

Read-only. Never repairs, backfills or modifies source events.
"""

from __future__ import annotations

from typing import Any

from psycopg2.extras import RealDictCursor

from .contracts import EXPERIMENT_ID


def audit_source_integrity(connection: Any) -> dict:
    """Compare original V3 source with the research ledger."""

    with connection.cursor(
        cursor_factory=RealDictCursor
    ) as cur:
        cur.execute(
            """
            WITH activation AS (
                SELECT freeze_ts
                FROM research_me_entry_quality.activation
                WHERE experiment_id = %s
                  AND freeze_ts IS NOT NULL
            ),
            eligible AS (
                SELECT s.event_key, s.lineage, s.symbol,
                       s.signal_time, s.source_direction,
                       s.source_scanner
                FROM dds.me_long_v2_inverse_short_v3_event s
                CROSS JOIN activation a
                WHERE s.signal_time > a.freeze_ts
                  AND s.created_at >= a.freeze_ts
                  AND s.captured_at >= a.freeze_ts
            )
            SELECT
                COUNT(*) AS eligible_source,
                COUNT(*) FILTER (
                    WHERE p.event_key IS NULL
                ) AS unprocessed,
                COUNT(*) FILTER (
                    WHERE p.processing_status = 'ACCEPTED'
                ) AS accepted,
                COUNT(*) FILTER (
                    WHERE p.processing_status = 'REJECTED'
                ) AS rejected,
                COUNT(*) FILTER (
                    WHERE p.processing_status = 'ACCEPTED'
                      AND (
                          e.event_key IS NULL
                          OR e.lineage IS DISTINCT FROM s.lineage
                          OR e.symbol IS DISTINCT FROM s.symbol
                          OR e.signal_time IS DISTINCT FROM s.signal_time
                          OR e.source_direction
                             IS DISTINCT FROM s.source_direction
                          OR e.source_scanner
                             IS DISTINCT FROM s.source_scanner
                      )
                ) AS integrity_conflicts
            FROM eligible s
            LEFT JOIN research_me_entry_quality.processed_event p
              ON p.event_key = s.event_key
             AND p.experiment_id = %s
            LEFT JOIN research_me_entry_quality.source_event e
              ON e.event_key = s.event_key
             AND e.experiment_id = %s
            """,
            (EXPERIMENT_ID, EXPERIMENT_ID, EXPERIMENT_ID),
        )

        result = dict(cur.fetchone())

    result["status"] = (
        "INTEGRITY_CONFLICT"
        if result["integrity_conflicts"]
        else "COVERAGE_GAP"
        if result["unprocessed"]
        else "SOURCE_COVERAGE_VALIDATED"
    )

    return result
