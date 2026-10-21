"""Issue #176 Phase-2 persistence repository (Blocker B6).

Database-API-agnostic (pg8000 / psycopg2) access to the dedicated Phase-2
storage created by ``sql/migrations/074_htf_baseline_short_phase2_registration.sql``.

Delivery guarantee (explicit): AT-LEAST-ONCE emission with IDEMPOTENT writes.
Every event is validated against the freeze boundary in the application AND
enforced again by the database insert function; duplicates on
(experiment_id, source_event_id) are silently suppressed and reported, never
errored. The runner advances its checkpoint cursor only after the transaction
commits, so a crash between emit and checkpoint replays the same events and the
unique key absorbs them. Exactly-once is NOT claimed; exactly-once *effect* is
achieved through idempotency.

No production connection is ever opened by this module implicitly; callers pass
an existing connection.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.research.htf_baseline_short_phase2_freeze_v1 import Phase2FreezeBoundary

EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


class P2PersistenceError(RuntimeError):
    pass


class P2ExperimentStateError(P2PersistenceError):
    pass


@dataclass(frozen=True)
class ExperimentState:
    experiment_id: str
    status: str
    freeze_ts: object
    protocol_fingerprint: object
    observer_enabled: bool

    @property
    def is_runnable(self) -> bool:
        return (
            self.status == "RUNNING"
            and self.freeze_ts is not None
            and bool(self.protocol_fingerprint)
        )


@dataclass
class EmitResult:
    inserted: int = 0
    deduplicated: int = 0
    rejected: int = 0

    def as_dict(self) -> dict:
        return {
            "inserted": self.inserted,
            "deduplicated": self.deduplicated,
            "rejected": self.rejected,
        }


def load_experiment_state(conn, experiment_id: str = EXPERIMENT_ID) -> ExperimentState:
    """Read the experiment registration row (read-only)."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT experiment_id, status, freeze_ts, protocol_fingerprint, observer_enabled "
            "FROM research.htf_baseline_short_phase2_experiment WHERE experiment_id = %s",
            (experiment_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        raise P2ExperimentStateError(f"P2_EXPERIMENT_NOT_REGISTERED: {experiment_id}")
    return ExperimentState(
        experiment_id=row[0],
        status=row[1],
        freeze_ts=row[2],
        protocol_fingerprint=row[3],
        observer_enabled=bool(row[4]),
    )


def freeze_sources(conn, experiment_id: str = EXPERIMENT_ID) -> dict:
    """Database-side freeze authority values (one of THREE independent sources).

    This function reports ONLY what the database says. The protocol authority
    (``protocol_v1.approved_freeze_ts``) and the runtime activation config are
    maintained elsewhere and independently; agreement between all three is
    enforced by the runner, which must fail closed on any mismatch or missing
    source. Nothing here mirrors one source into another.
    """
    state = load_experiment_state(conn, experiment_id)
    return {
        "database": state.freeze_ts,
        "status": state.status,
        "observer_enabled": state.observer_enabled,
        "database_fingerprint": state.protocol_fingerprint,
    }


def insert_observation_raw(
    conn,
    *,
    experiment_id: str,
    source_event_id: str,
    episode_id: str,
    level_id: str,
    symbol: str,
    signal_time,
    first_touch_time,
    known_at,
    level_price,
    entry_reference_price,
    warmup_origin: bool = False,
    resumed_pre_freeze: bool = False,
    features: dict | None = None,
) -> int | None:
    """Call the DB-level enforcing insert. Returns the id, or None on duplicate.

    ``warmup_origin``/``resumed_pre_freeze`` are authoritative eligibility
    flags: the SQL function rejects any non-FALSE value, so direct callers
    cannot smuggle ineligible episodes past the persistence boundary.
    """
    import json

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_insert_observation("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                experiment_id,
                source_event_id,
                episode_id,
                level_id,
                symbol,
                signal_time,
                first_touch_time,
                known_at,
                level_price,
                entry_reference_price,
                bool(warmup_origin),
                bool(resumed_pre_freeze),
                json.dumps(features or {}, sort_keys=True),
            ),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return row[0] if row else None


class Phase2ObservationWriter:
    """Idempotent observation writer with mandatory freeze-boundary gating."""

    def __init__(self, conn, *, experiment_id: str = EXPERIMENT_ID):
        self.conn = conn
        self.experiment_id = experiment_id

    def emit(self, events: list[dict], boundary: Phase2FreezeBoundary) -> EmitResult:
        """Emit one atomic batch. Raises on any failure; caller must NOT advance
        its cursor before this returns (at-least-once + idempotent writes).

        ``boundary`` is MANDATORY: eligibility filtering must never be silently
        bypassed. A missing boundary is a programming error and raises.
        """
        if boundary is None:
            raise ValueError(
                "Phase2ObservationWriter.emit requires a freeze boundary; "
                "eligibility filtering must never be bypassed"
            )
        result = EmitResult()
        try:
            for event in events:
                decision = boundary.check_event(
                    signal_time=event["known_at_ms"],
                    first_touch_time=event["first_touch_open_ms"],
                    warmup_origin=bool(event.get("warmup_origin")),
                    resumed_pre_freeze=bool(event.get("resumed_pre_freeze", False)),
                )
                if not decision:
                    result.rejected += 1
                    continue
                row_id = insert_observation_raw(
                    self.conn,
                    experiment_id=self.experiment_id,
                    source_event_id=event["source_event_id"],
                    episode_id=event["episode_id"],
                    level_id=event["level_id"],
                    symbol=event["symbol"],
                    signal_time=_ms_to_iso(event["known_at_ms"]),
                    first_touch_time=_ms_to_iso(event["first_touch_open_ms"]),
                    known_at=_ms_to_iso(event["known_at_ms"]),
                    level_price=event.get("level_price_at_touch"),
                    entry_reference_price=event.get("entry_reference_price"),
                    warmup_origin=bool(event.get("warmup_origin")),
                    resumed_pre_freeze=bool(event.get("resumed_pre_freeze", False)),
                    features={
                        "level_available_ms": event.get("level_available_ms"),
                        "direction": event.get("direction", "SHORT"),
                        "protocol": "HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1",
                        "provenance": event.get("provenance", {}),
                    },
                )
                if row_id is None:
                    result.deduplicated += 1
                else:
                    result.inserted += 1
            self.conn.commit()
        except Exception as exc:
            try:
                self.conn.rollback()
            except Exception:
                pass
            if isinstance(exc, (P2PersistenceError, ValueError)):
                raise
            raise P2PersistenceError(f"observation batch failed: {exc}") from exc
        return result


def _ms_to_iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
