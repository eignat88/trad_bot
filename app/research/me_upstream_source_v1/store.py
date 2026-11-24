"""Issue #305 — persistence for the upstream source capture.

Two implementations behind ONE small interface:

``InMemoryUpstreamSourceStore``
    Process-local store for unit tests and dry-runs.  Enforces exactly the
    same invariants as the PostgreSQL store (activation guard, idempotent
    keys, immutable-lineage conflict detection, append-only ledgers).

``PgUpstreamSourceStore``
    PostgreSQL implementation over a DEDICATED connection (never the scanner
    or paper-runner transaction), writing ONLY to the capture's own schema::

        research_me_upstream_coverage.activation          (read-only here)
        research_me_upstream_coverage.source_event
        research_me_upstream_coverage.rejected_event
        research_me_upstream_coverage.capture_health
        research_me_upstream_coverage.capture_state_event

Isolation invariants enforced here:

* the store never writes ``dds.*``, ``research.*`` or any trading table —
  the V3 ledger is READ-ONLY input for the coverage reconciler only;
* events are idempotent on ``source_event_key`` (``ON CONFLICT DO NOTHING``)
  and the first-capture snapshot is preserved verbatim — a redelivery never
  overwrites it;
* a re-delivery of the same key with a DIFFERENT IMMUTABLE SOURCE lineage
  (``immutable_source_hash``: stable identity + immutable provenance only)
  raises :class:`SourceIntegrityError` (``EVENT_KEY_LINEAGE_CONFLICT``) —
  never silently swallowed.  VOLATILE detector facts (price / entry-stop-
  target geometry / feature values) legitimately drift between re-detections
  while the candle is still forming, and re-detection variance that is
  EXPECTED for one signal (a fresh ``setup_id`` and ``detected_at`` per
  re-scan) is likewise outside that subset — all of it stays a plain
  idempotent ``DUPLICATE`` (criterion 5).  The cross-ledger snapshot
  comparison (``comparable_lineage_hash``) is a SEPARATE mechanism used by
  the coverage reconciler, not by this conflict check;
* the event INSERT itself re-checks RUNNING activation + fingerprint +
  ``freeze_ts`` in SQL (defense in depth: an event can never land without a
  frozen activation, even if a caller bypasses the Python guard);
* every statement is idempotent and self-committing; a failed statement is
  rolled back so the shared connection is never left aborted.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import (
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    SourceIntegrityError,
    UpstreamSourceEvent,
    immutable_source_hash,
)
from .protocol import EXPERIMENT_ID, protocol_fingerprint

logger = logging.getLogger(__name__)

SCHEMA = "research_me_upstream_coverage"
ACTIVATION_TABLE = f"{SCHEMA}.activation"
EVENT_TABLE = f"{SCHEMA}.source_event"
REJECTED_TABLE = f"{SCHEMA}.rejected_event"
HEALTH_TABLE = f"{SCHEMA}.capture_health"
STATE_TABLE = f"{SCHEMA}.capture_state_event"

# Honest save_event outcomes.
SAVE_CAPTURED = "CAPTURED"
SAVE_DUPLICATE = "DUPLICATE"
SAVE_NOT_ACTIVE = "NOT_ACTIVE"

# Capture contour states (append-only state ledger).
STATE_HEALTHY = "HEALTHY"
STATE_BLOCKED = "BLOCKED"
STATE_RESUMED = "RESUMED"
VALID_STATES = frozenset({STATE_HEALTHY, STATE_BLOCKED, STATE_RESUMED})

_EVENT_COLUMNS = (
    "source_event_key", "experiment_id", "source_identity", "symbol",
    "signal_time", "captured_at", "activation_freeze_ts", "source_setup_id",
    "source_scanner", "source_scanner_version", "source_direction",
    "entry_timeframe", "signal_candle_open_time", "source_reference_price",
    "source_invalidation_price", "source_target_price", "source_target_2",
    "entry_zone_low", "entry_zone_high", "source_score", "market_regime",
    "source_fingerprint", "capture_route", "features", "lineage",
    "lineage_hash", "comparable_lineage_hash",
)

_EVENT_VALUES_SQL = ", ".join(
    "%s::jsonb" if column in ("features", "lineage") else "%s"
    for column in _EVENT_COLUMNS
)

# Defense in depth: the INSERT itself only lands when a frozen RUNNING
# activation with the CURRENT protocol fingerprint exists (migration 094
# additionally CHECKs strict post-freeze on every row).
INSERT_EVENT_SQL = f"""
    INSERT INTO {EVENT_TABLE} ({", ".join(_EVENT_COLUMNS)})
    SELECT {_EVENT_VALUES_SQL}
    WHERE EXISTS (
        SELECT 1 FROM {ACTIVATION_TABLE}
        WHERE experiment_id = %s
          AND status = 'RUNNING'
          AND protocol_fingerprint = %s
          AND freeze_ts IS NOT NULL
    )
    ON CONFLICT (source_event_key) DO NOTHING
    RETURNING source_event_key
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UpstreamSourceStore:
    """Interface of the capture's persistence layer.

    ``broken`` — set by an implementation when its underlying connection
    fails (driver-level error).  Callers must then DISCARD the store and
    reconnect rather than keep hammering a dead connection (bounded
    retry/backoff lives on the caller side).
    """

    broken: bool = False

    def load_activation(self) -> Mapping[str, Any] | None:
        raise NotImplementedError

    def load_capture_state(self) -> Mapping[str, Any] | None:
        raise NotImplementedError

    def save_event(self, event: UpstreamSourceEvent) -> str:
        raise NotImplementedError

    def reject_event(
        self,
        *,
        source_event_key: str | None,
        source_identity: str | None,
        reason_code: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    def record_state(
        self,
        state: str,
        reason_code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    def record_health(
        self,
        status: str,
        *,
        events_seen: int = 1,
        events_inserted: int = 0,
        duplicates: int = 0,
        errors: int = 0,
        delivery_latency_seconds: float | None = None,
        error_code: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        return None


# ---------------------------------------------------------------------------
# In-memory implementation (unit tests / dry-run)
# ---------------------------------------------------------------------------

class InMemoryUpstreamSourceStore(UpstreamSourceStore):
    """Process-local store with the same invariants as the PG store."""

    def __init__(
        self,
        activation: Mapping[str, Any] | None = None,
        *,
        expected_fingerprint: str | None = None,
    ) -> None:
        self._activation = dict(activation) if activation else None
        self._events: dict[str, dict[str, Any]] = {}
        self._rejected: list[dict[str, Any]] = []
        self._states: list[dict[str, Any]] = []
        self._health: list[dict[str, Any]] = []
        # Mirror the PG store exactly: the fingerprint of the CURRENT code is
        # always required (defense-in-depth parity between the two stores).
        self.expected_fingerprint = (
            expected_fingerprint if expected_fingerprint is not None
            else protocol_fingerprint()
        )
        self.writes = 0

    def set_activation(self, activation: Mapping[str, Any] | None) -> None:
        self._activation = dict(activation) if activation else None

    def load_activation(self) -> Mapping[str, Any] | None:
        return dict(self._activation) if self._activation else None

    def load_capture_state(self) -> Mapping[str, Any] | None:
        return dict(self._states[-1]) if self._states else None

    def _activation_allows_capture(self) -> bool:
        if self._activation is None:
            return False
        if self._activation.get("experiment_id") != EXPERIMENT_ID:
            return False
        if self._activation.get("status") != "RUNNING":
            return False
        if self._activation.get("freeze_ts") is None:
            return False
        if self._activation.get("protocol_fingerprint") != (
            self.expected_fingerprint
        ):
            return False
        return True

    def save_event(self, event: UpstreamSourceEvent) -> str:
        if not self._activation_allows_capture():
            return SAVE_NOT_ACTIVE
        existing = self._events.get(event.source_event_key)
        if existing is not None:
            # Idempotent re-delivery unless the IMMUTABLE SOURCE fields
            # changed (volatile price/geometry/feature drift inside the
            # forming candle and re-detection variance of setup_id /
            # detected_at are expected and never a conflict).  The stored
            # first-capture snapshot is preserved — never overwritten.
            if (
                existing["immutable_source_hash"]
                != event.immutable_source_hash
            ):
                raise SourceIntegrityError(REASON_EVENT_KEY_LINEAGE_CONFLICT)
            return SAVE_DUPLICATE
        self._events[event.source_event_key] = {
            "source_event_key": event.source_event_key,
            "source_identity": event.source_identity,
            "symbol": event.symbol,
            "signal_time": event.signal_time,
            "captured_at": event.captured_at,
            "lineage": dict(event.lineage),
            "lineage_hash": event.lineage_hash,
            "comparable_lineage_hash": event.comparable_lineage_hash,
            "immutable_source_hash": event.immutable_source_hash,
        }
        self.writes += 1
        return SAVE_CAPTURED

    def reject_event(
        self,
        *,
        source_event_key: str | None,
        source_identity: str | None,
        reason_code: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._rejected.append({
            "source_event_key": source_event_key,
            "source_identity": source_identity,
            "reason_code": reason_code,
            "payload": dict(payload or {}),
        })
        self.writes += 1

    def record_state(
        self,
        state: str,
        reason_code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if state not in VALID_STATES:
            raise ValueError("INVALID_CAPTURE_STATE")
        self._states.append({
            "state": state,
            "reason_code": reason_code,
            "details": dict(details or {}),
            "recorded_at": _utc_now(),
        })
        self.writes += 1

    def record_health(
        self,
        status: str,
        *,
        events_seen: int = 1,
        events_inserted: int = 0,
        duplicates: int = 0,
        errors: int = 0,
        delivery_latency_seconds: float | None = None,
        error_code: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self._health.append({
            "status": status,
            "events_seen": events_seen,
            "events_inserted": events_inserted,
            "duplicates": duplicates,
            "errors": errors,
            "delivery_latency_seconds": delivery_latency_seconds,
            "error_code": error_code,
            "diagnostics": dict(diagnostics or {}),
            "recorded_at": _utc_now(),
        })
        self.writes += 1

    # -- test helpers ------------------------------------------------------
    @property
    def events(self) -> dict[str, dict[str, Any]]:
        return dict(self._events)

    @property
    def rejected(self) -> list[dict[str, Any]]:
        return list(self._rejected)

    @property
    def states(self) -> list[dict[str, Any]]:
        return list(self._states)

    @property
    def health(self) -> list[dict[str, Any]]:
        return list(self._health)


# ---------------------------------------------------------------------------
# PostgreSQL implementation (DB-API: pg8000 or psycopg2)
# ---------------------------------------------------------------------------

class PgUpstreamSourceStore(UpstreamSourceStore):
    """PostgreSQL store over a dedicated connection.

    The connection must NOT be shared with the scanner/paper transactions:
    capture commits and rollbacks can never touch a production transaction.
    Driver-agnostic DB-API semantics (``%s`` placeholders, JSON passed as a
    canonical string with explicit ``::jsonb`` casts) so both pg8000 and
    psycopg2 connections work.
    """

    def __init__(
        self,
        connection: Any,
        *,
        expected_fingerprint: str | None = None,
    ) -> None:
        self._conn = connection
        self.broken = False
        self.expected_fingerprint = (
            expected_fingerprint if expected_fingerprint is not None
            else _default_fingerprint()
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("me upstream source store close failed", exc_info=True)

    # -- helpers ----------------------------------------------------------
    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.debug("me upstream source store rollback failed", exc_info=True)

    def _execute(self, sql: str, params: tuple | list = ()) -> Any:
        try:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur
        except Exception:
            # A failed cursor/execute means this dedicated connection can no
            # longer be trusted (driver-level break, aborted transaction or
            # driver error).  Mark the store broken so the caller DISCARDS it
            # and reconnects instead of hammering a dead connection; the
            # caller-side retry/backoff keeps that bounded.
            self.broken = True
            self._rollback()
            raise

    def load_activation(self) -> Mapping[str, Any] | None:
        cur = self._execute(
            f"""
            SELECT experiment_id, status, protocol_version,
                   protocol_fingerprint, freeze_ts
            FROM {ACTIVATION_TABLE}
            WHERE experiment_id = %s
            """,
            (EXPERIMENT_ID,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "experiment_id": row[0],
            "status": row[1],
            "protocol_version": row[2],
            "protocol_fingerprint": row[3],
            "freeze_ts": row[4],
        }

    def load_capture_state(self) -> Mapping[str, Any] | None:
        cur = self._execute(
            f"""
            SELECT state, reason_code, details, recorded_at
            FROM {STATE_TABLE}
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "state": row[0],
            "reason_code": row[1],
            "details": row[2],
            "recorded_at": row[3],
        }

    def save_event(self, event: UpstreamSourceEvent) -> str:
        columns = (
            event.source_event_key,
            EXPERIMENT_ID,
            event.source_identity,
            event.symbol,
            event.signal_time,
            event.captured_at,
            event.activation_freeze_ts,
            event.source_setup_id,
            event.source_scanner,
            event.source_scanner_version,
            event.source_direction,
            event.entry_timeframe,
            event.signal_candle_open_time,
            event.source_reference_price,
            event.source_invalidation_price,
            event.source_target_price,
            event.source_target_2,
            event.entry_zone_low,
            event.entry_zone_high,
            event.source_score,
            event.market_regime,
            event.source_fingerprint,
            event.capture_route,
            json.dumps(event.features, allow_nan=False),
            json.dumps(event.lineage, allow_nan=False),
            event.lineage_hash,
            event.comparable_lineage_hash,
        )
        cur = self._execute(
            INSERT_EVENT_SQL,
            columns + (
                EXPERIMENT_ID,
                self.expected_fingerprint,
            ),
        )
        inserted = cur.fetchone() is not None

        if not inserted:
            # Either an idempotent re-delivery (DUPLICATE) or the activation
            # guard refused the INSERT (NOT_ACTIVE) — distinguish honestly.
            cur = self._execute(
                f"""
                SELECT lineage
                FROM {EVENT_TABLE}
                WHERE source_event_key = %s
                """,
                (event.source_event_key,),
            )
            existing = cur.fetchone()
            self._conn.commit()
            if existing is None:
                return SAVE_NOT_ACTIVE
            # Idempotent re-delivery unless the IMMUTABLE SOURCE fields
            # changed (volatile price/geometry/feature drift inside the
            # forming candle and expected re-detection variance of
            # setup_id / detected_at are never a conflict).  The stored
            # first-capture snapshot is preserved — never overwritten.
            stored_lineage = existing[0]
            if isinstance(stored_lineage, str):
                stored_lineage = json.loads(stored_lineage)
            if (
                immutable_source_hash(stored_lineage)
                != event.immutable_source_hash
            ):
                raise SourceIntegrityError(REASON_EVENT_KEY_LINEAGE_CONFLICT)
            return SAVE_DUPLICATE

        self._conn.commit()
        return SAVE_CAPTURED

    def reject_event(
        self,
        *,
        source_event_key: str | None,
        source_identity: str | None,
        reason_code: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {REJECTED_TABLE} (
                source_event_key, source_identity, reason_code, payload
            )
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                source_event_key,
                source_identity,
                reason_code,
                json.dumps(dict(payload or {}), allow_nan=False),
            ),
        )
        self._conn.commit()

    def record_state(
        self,
        state: str,
        reason_code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if state not in VALID_STATES:
            raise ValueError("INVALID_CAPTURE_STATE")
        self._execute(
            f"""
            INSERT INTO {STATE_TABLE} (state, reason_code, details)
            VALUES (%s, %s, %s::jsonb)
            """,
            (
                state,
                reason_code,
                json.dumps(dict(details or {}), allow_nan=False),
            ),
        )
        self._conn.commit()

    def record_health(
        self,
        status: str,
        *,
        events_seen: int = 1,
        events_inserted: int = 0,
        duplicates: int = 0,
        errors: int = 0,
        delivery_latency_seconds: float | None = None,
        error_code: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {HEALTH_TABLE} (
                status, events_seen, events_inserted, duplicates, errors,
                delivery_latency_seconds, error_code, diagnostics
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                status,
                int(events_seen),
                int(events_inserted),
                int(duplicates),
                int(errors),
                delivery_latency_seconds,
                error_code,
                json.dumps(dict(diagnostics or {}), allow_nan=False),
            ),
        )
        self._conn.commit()


def _default_fingerprint() -> str:
    from .protocol import protocol_fingerprint

    return protocol_fingerprint()


def connect_pg_store(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    unix_sock: str | None = None,
) -> PgUpstreamSourceStore:
    """Open a DEDICATED pg8000 connection for the upstream capture.

    Never shared with the scanner/paper runners — capture commits and
    rollbacks can never touch a production transaction.  Every socket
    operation is bounded (10s) so an unreachable host can never stall a
    scanner cycle.
    """
    import pg8000

    kwargs: dict[str, Any] = {
        "database": database,
        "user": user,
        "password": password,
        "timeout": 10,
    }
    if unix_sock:
        kwargs["unix_sock"] = unix_sock
    else:
        kwargs["host"] = host
        kwargs["port"] = port
    conn = pg8000.connect(**kwargs)
    return PgUpstreamSourceStore(conn)
