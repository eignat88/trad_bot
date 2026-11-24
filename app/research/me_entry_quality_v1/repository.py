"""Issue #296: isolated PostgreSQL research repository.

The database role must have SELECT-only access to V3 source.
Research writes are restricted to the isolated schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

try:  # pragma: no cover - driver present in deployment, optional in unit tests
    from psycopg2.extras import Json, RealDictCursor
except ImportError:  # noqa: F401
    Json = RealDictCursor = None

from .contracts import (
    EXPERIMENT_ID,
    SourceIntegrityError,
    ValidatedSourceEvent,
    as_utc,
)
from .integrity import audit_source_integrity as _audit_source_integrity

# Re-exported for backwards import compatibility.
__all__ = [
    "PgObserverRepository",
    "SourceIntegrityError",
]


class PgObserverRepository:

    def __init__(self, connection: Any):
        self.connection = connection

        if connection.autocommit:
            raise ValueError("AUTOCOMMIT_NOT_ALLOWED")

        if connection.readonly:
            raise ValueError("WRITER_CONNECTION_READ_ONLY")

    def acquire_processing_lock(self) -> bool:
        # Transaction-scoped lock. Released on COMMIT or ROLLBACK.
        # Fixed namespace for Issue #296.
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (296, 1),
            )
            return bool(cur.fetchone()[0])

    def rollback(self) -> None:
        self.connection.rollback()

    def audit_source_integrity(
        self,
        *,
        gap_grace_seconds: int,
        as_of: Any = None,
    ) -> dict:
        # Read-only reconciliation against the V3 source ledger.
        # Never repairs, backfills or modifies source data.
        return _audit_source_integrity(
            self.connection,
            gap_grace_seconds=gap_grace_seconds,
            as_of=as_of,
        )

    def load_contour_state(self) -> dict | None:
        """Latest research-contour state (append-only state ledger)."""

        with self.connection.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT state, reason_code, details, recorded_at
                FROM research_me_entry_quality.observer_state_event
                WHERE experiment_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (EXPERIMENT_ID,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def record_state(
        self,
        state: str,
        reason_code: str,
        details: Mapping[str, Any],
    ) -> None:
        """Append a contour-state event. Never updates or deletes rows.

        The observer only ever records ``BLOCKED``. ``RESUMED`` is
        written exclusively by the administrative recovery contour
        (deploy/sql/296_observer_recovery.sql) — the observer role has
        no UPDATE/DELETE rights anywhere in this schema.
        """

        if state not in ("BLOCKED", "RESUMED"):
            raise ValueError("INVALID_CONTOUR_STATE")

        if not reason_code:
            raise ValueError("INVALID_STATE_REASON")

        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_me_entry_quality.observer_state_event (
                    experiment_id,
                    state,
                    reason_code,
                    details
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    EXPERIMENT_ID,
                    state,
                    reason_code,
                    Json(dict(details)),
                ),
            )

    def load_activation(self) -> dict | None:
        with self.connection.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT experiment_id, status, freeze_ts,
                       protocol_version
                FROM research_me_entry_quality.activation
                WHERE experiment_id = %s
                """,
                (EXPERIMENT_ID,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def fetch_pending_events(
        self,
        freeze_ts: datetime,
        limit: int,
    ) -> list[dict]:

        freeze_ts = as_utc(freeze_ts)

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("INVALID_LIMIT")

        with self.connection.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT
                    s.event_key,
                    s.symbol,
                    s.signal_time,
                    s.created_at,
                    s.captured_at,
                    s.source_scanner,
                    s.source_scanner_version,
                    s.source_direction,
                    s.source_setup_id,
                    s.capture_route,
                    s.entry_timeframe,
                    s.signal_candle_open_time,
                    s.source_reference_price,
                    s.lineage
                FROM dds.me_long_v2_inverse_short_v3_event s
                WHERE s.signal_time > %s
                  AND s.created_at >= %s
                  AND s.captured_at >= %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM research_me_entry_quality.processed_event p
                      WHERE p.event_key = s.event_key
                  )
                ORDER BY s.created_at, s.event_key
                LIMIT %s
                """,
                (freeze_ts, freeze_ts, freeze_ts, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def save_event(
        self,
        event: ValidatedSourceEvent,
    ) -> str:

        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_me_entry_quality.source_event (
                    event_key,
                    experiment_id,
                    symbol,
                    signal_time,
                    source_captured_at,
                    source_created_at,
                    activation_freeze_ts,
                    source_setup_id,
                    source_scanner,
                    source_scanner_version,
                    source_direction,
                    entry_timeframe,
                    signal_candle_open_time,
                    source_reference_price,
                    lineage,
                    lineage_hash
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (event_key) DO NOTHING
                RETURNING event_key
                """,
                (
                    event.event_key,
                    EXPERIMENT_ID,
                    event.symbol,
                    event.signal_time,
                    event.source_captured_at,
                    event.source_created_at,
                    event.freeze_ts,
                    event.source_setup_id,
                    event.source_scanner,
                    event.source_scanner_version,
                    event.source_direction,
                    event.entry_timeframe,
                    event.signal_candle_open_time,
                    event.source_reference_price,
                    Json(event.lineage),
                    event.lineage_hash,
                ),
            )

            inserted = cur.fetchone() is not None

            if not inserted:
                cur.execute(
                    """
                    SELECT lineage_hash
                    FROM research_me_entry_quality.source_event
                    WHERE event_key = %s
                    """,
                    (event.event_key,),
                )
                existing = cur.fetchone()

                if (
                    existing is None
                    or existing[0] != event.lineage_hash
                ):
                    raise SourceIntegrityError(
                        "EVENT_KEY_LINEAGE_CONFLICT"
                    )

            cur.execute(
                """
                INSERT INTO research_me_entry_quality.processed_event (
                    event_key,
                    experiment_id,
                    processing_status
                )
                VALUES (%s, %s, 'ACCEPTED')
                ON CONFLICT (event_key) DO NOTHING
                RETURNING event_key
                """,
                (event.event_key, EXPERIMENT_ID),
            )

            if cur.fetchone() is None:
                cur.execute(
                    """
                    SELECT processing_status
                    FROM research_me_entry_quality.processed_event
                    WHERE event_key = %s
                    """,
                    (event.event_key,),
                )
                existing = cur.fetchone()
                if existing is None or existing[0] != "ACCEPTED":
                    raise SourceIntegrityError(
                        "PROCESSING_LEDGER_CONFLICT"
                    )

            return "INSERTED" if inserted else "DUPLICATE"

    def mark_invalid(
        self,
        event_key: str,
        reason_code: str,
    ) -> None:

        if not event_key or not reason_code:
            raise ValueError("INVALID_REJECTION_RECORD")

        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_me_entry_quality.processed_event (
                    event_key,
                    experiment_id,
                    processing_status,
                    reason_code
                )
                VALUES (%s, %s, 'REJECTED', %s)
                ON CONFLICT (event_key) DO NOTHING
                RETURNING event_key
                """,
                (event_key, EXPERIMENT_ID, reason_code),
            )

            if cur.fetchone() is None:
                cur.execute(
                    """
                    SELECT processing_status, reason_code
                    FROM research_me_entry_quality.processed_event
                    WHERE event_key = %s
                    """,
                    (event_key,),
                )
                existing = cur.fetchone()
                if existing != ("REJECTED", reason_code):
                    raise SourceIntegrityError(
                        "REJECTION_LEDGER_CONFLICT"
                    )

    def record_health(
        self,
        status: str,
        source_rows_seen: int,
        events_inserted: int,
        diagnostics: Mapping[str, Any],
    ) -> None:

        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_me_entry_quality.source_health (
                    status,
                    source_rows_seen,
                    events_inserted,
                    diagnostics
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    status,
                    source_rows_seen,
                    events_inserted,
                    Json(dict(diagnostics)),
                ),
            )

        # Atomic commit of events, processing ledger and health.
        self.connection.commit()
