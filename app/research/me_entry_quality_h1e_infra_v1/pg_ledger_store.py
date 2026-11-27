"""Issue #324 / H1-E — C.2: PostgreSQL-backed ledger store.

Implements the FROZEN Phase A :class:`H1EStore` contract
(``lookup`` / ``pending_retries`` / ``commit_batch``) against the
SEPARATELY REVIEWED DDL proposal
``sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`` (schema
``research_me_entry_quality_h1e``).  The DDL is applied ONLY to an
isolated test database by the Phase C test-suite — never to a working
database.

Atomicity: ONE transaction per ``commit_batch`` — event inserts, the
append-only classification/status journals, the reject journal, the
conflict journal, the durable pending-retry state and the delivery
checkpoint commit or roll back TOGETHER.  There is no other write path.

Exactly-once effect: the immutable first-capture row is inserted
idempotently (``ON CONFLICT DO NOTHING``); an identical redelivery is a
no-op (the last journal entry with the same classification is never
duplicated); one conflict is recorded EXACTLY ONCE per conflict key
(unique index + ``ON CONFLICT DO NOTHING``).

Fail-closed edges are enforced TWICE: by this store (the same exception
classes as the reference store) and by the database itself
(``h1e_event_prospective_guard`` trigger: no approved activation /
fingerprint mismatch / tampered freeze / pre-freeze event — a direct
write cannot bypass it).  Database exceptions are translated back to the
frozen exception classes.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureCursor,
    LedgerWrite,
    PendingRetry,
    PendingRetryUpdate,
)
from app.research.me_entry_quality_h1e_v1.contracts import H1EFreeze
from app.research.me_entry_quality_h1e_v1.ledger import (
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
    STATUS_REJECTED,
    ActivationFingerprintMismatch,
    CheckpointRegression,
    FreezeTsMismatch,
    LedgerEntry,
    LedgerIdentityConflict,
    NoApprovedActivation,
    PreFreezeWrite,
    SimulatedCrash,
)

H1E_PG_LEDGER_VERSION = "H1E_PG_LEDGER_STORE_V1"

_SCHEMA = "research_me_entry_quality_h1e"

#: exact integer µs -> TIMESTAMPTZ (microsecond resolution, no floats)
_US_TO_TS = (
    "(%s::bigint * INTERVAL '1 microsecond' "
    "+ TIMESTAMP WITH TIME ZONE 'epoch')"
)


def _norm(value: Any) -> Any:
    """JSON-normalize (tuples become lists) for exact value comparison."""
    return json.loads(json.dumps(value))


def _json_text(value: Any) -> str:
    return json.dumps(value)


class PgH1EStore:
    """PostgreSQL implementation of the frozen ``H1EStore`` contract.

    ``connection_factory`` is a zero-argument callable returning a DB-API
    connection (injected; the module never opens connections itself).  One
    connection is taken per call and released at the end of the call.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        activation: H1EFreeze,
        worker_id: str = "h1e-observer-v1",
    ) -> None:
        if not isinstance(activation, H1EFreeze):
            raise ValueError("activation must be an H1EFreeze")
        self._connection_factory = connection_factory
        self.activation = activation
        self.worker_id = str(worker_id)
        #: tests ONLY — when True, ``commit_batch`` rolls back after staging
        self.fault_inject_commit = False

    # ─── H1EStore contract ─────────────────────────────────────────────
    def lookup(self, ledger_identity: str) -> LedgerEntry | None:
        connection = self._connection_factory()
        try:
            with connection.cursor() as statement:
                statement.execute(
                    "SELECT ledger_identity, source_event_key, "
                    "h1e_protocol_fingerprint, preserved_hash, payload "
                    f"FROM {_SCHEMA}.h1e_event WHERE ledger_identity = %s",
                    (ledger_identity,),
                )
                row = statement.fetchone()
                if row is None:
                    return None
                statement.execute(
                    "SELECT event_kind, detail FROM "
                    f"{_SCHEMA}.h1e_status_event WHERE ledger_identity = %s "
                    "ORDER BY id",
                    (ledger_identity,),
                )
                journal_rows = statement.fetchall()
        finally:
            _close(connection)
        journal: list[Mapping[str, Any]] = []
        for event_kind, detail in journal_rows:
            detail = dict(detail or {})
            journal.append({
                "event_kind": str(event_kind),
                "classification": detail.get("classification"),
                "attempt_count": int(detail.get("attempt_count", 1)),
                "captured_at_us": int(detail.get("captured_at_us", 0)),
            })
        return LedgerEntry(
            ledger_identity=str(row[0]),
            source_event_key=str(row[1]),
            protocol_fingerprint=str(row[2]),
            preserved_hash=str(row[3]),
            payload=dict(row[4]),
            journal=tuple(journal),
        )

    def pending_retries(self) -> tuple[PendingRetry, ...]:
        connection = self._connection_factory()
        try:
            with connection.cursor() as statement:
                statement.execute(
                    "SELECT ledger_identity, source_event_key, attempt_count, "
                    "deferred_reason, next_attempt_us, preserved_hash FROM "
                    f"{_SCHEMA}.h1e_pending_retry "
                    "ORDER BY next_attempt_us, ledger_identity"
                )
                rows = statement.fetchall()
        finally:
            _close(connection)
        return tuple(
            PendingRetry(
                ledger_identity=str(row[0]),
                source_event_key=str(row[1]),
                attempt_count=int(row[2]),
                deferred_reason=str(row[3]),
                next_attempt_us=int(row[4]),
                preserved_hash=str(row[5]),
            )
            for row in rows
        )

    @property
    def checkpoint(self) -> CaptureCursor:
        connection = self._connection_factory()
        try:
            with connection.cursor() as statement:
                statement.execute(
                    "SELECT captured_us, source_event_key FROM "
                    f"{_SCHEMA}.h1e_checkpoint WHERE worker_id = %s",
                    (self.worker_id,),
                )
                row = statement.fetchone()
        finally:
            _close(connection)
        if row is None:
            return CaptureCursor()
        return CaptureCursor(int(row[0]), str(row[1]))

    def commit_batch(
        self,
        items: Sequence[LedgerWrite],
        checkpoint: CaptureCursor,
        *,
        conflicts: Sequence[Mapping[str, Any]] = (),
        pending: Sequence[PendingRetryUpdate] = (),
    ) -> None:
        """Apply one atomic batch (all-or-nothing, exactly-once effect)."""
        if not isinstance(checkpoint, CaptureCursor):
            raise ValueError("checkpoint must be a CaptureCursor")
        # validation phase — nothing applied yet (mirrors the reference store)
        for item in items:
            if item.event_kind == STATUS_REJECTED:
                continue
            self._check_activation(item)

        connection = self._connection_factory()
        try:
            with connection.cursor() as statement:
                try:
                    self._apply(statement, items, checkpoint,
                                conflicts=conflicts, pending=pending)
                    if self.fault_inject_commit:
                        self.fault_inject_commit = False
                        raise SimulatedCrash("injected commit crash (tests only)")
                    connection.commit()
                except Exception as exc:  # noqa: BLE001 — translate then raise
                    connection.rollback()
                    _reraise_translated(exc)
        finally:
            _close(connection)

    # ─── internals ─────────────────────────────────────────────────────
    def _check_activation(self, item: LedgerWrite) -> None:
        activation = self.activation
        if item.protocol_fingerprint != activation.protocol_fingerprint:
            raise ActivationFingerprintMismatch(
                f"{item.ledger_identity}: delivered protocol fingerprint "
                f"{item.protocol_fingerprint!r} != approved "
                f"{activation.protocol_fingerprint!r}"
            )
        if item.h1e_freeze_ts_us != int(activation.freeze_ts_us):
            raise FreezeTsMismatch(
                f"h1e_freeze_ts_us {item.h1e_freeze_ts_us} != approved "
                f"{activation.freeze_ts_us}"
            )
        signal_time_us = int(item.payload.get("signal_time_us", 0))
        if signal_time_us <= int(activation.freeze_ts_us):
            raise PreFreezeWrite(
                f"signal_time_us {signal_time_us} <= approved H1-E freeze "
                f"{activation.freeze_ts_us}"
            )

    def _apply(
        self,
        statement: Any,
        items: Sequence[LedgerWrite],
        checkpoint: CaptureCursor,
        *,
        conflicts: Sequence[Mapping[str, Any]],
        pending: Sequence[PendingRetryUpdate],
    ) -> None:
        # explicit checkpoint regression check (fail closed BEFORE writing)
        statement.execute(
            "SELECT captured_us, source_event_key FROM "
            f"{_SCHEMA}.h1e_checkpoint WHERE worker_id = %s FOR UPDATE",
            (self.worker_id,),
        )
        row = statement.fetchone()
        if row is not None:
            current = CaptureCursor(int(row[0]), str(row[1]))
            if checkpoint.as_tuple() < current.as_tuple():
                raise CheckpointRegression(
                    f"checkpoint {checkpoint.as_tuple()} < {current.as_tuple()}"
                )

        for item in items:
            if item.event_kind == STATUS_REJECTED:
                payload = dict(item.payload)
                statement.execute(
                    "INSERT INTO " + _SCHEMA + ".h1e_reject_event "
                    "(source_event_key, h1e_protocol_fingerprint, reason_code, "
                    "detail, captured_at_us, payload) VALUES (%s, %s, %s, %s, "
                    "%s, %s::jsonb)",
                    (
                        item.source_event_key,
                        item.protocol_fingerprint,
                        str(payload.get("reason", "")),
                        str(payload.get("detail", "")),
                        int(item.captured_at_us),
                        _json_text(payload),
                    ),
                )
                continue

            payload = dict(item.payload)
            signal_time_us = int(payload["signal_time_us"])
            # immutable first-capture row (idempotent insert)
            statement.execute(
                "INSERT INTO " + _SCHEMA + ".h1e_event (ledger_identity, "
                "source_event_key, h1e_protocol_fingerprint, h1e_activation_id, "
                "source_identity, symbol, signal_time_us, signal_time, "
                "signal_time_ms, captured_at_us, source_freeze_ts_us, "
                "h1e_freeze_ts_us, lineage_hash, source_fingerprint, "
                "preserved_hash, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                + _US_TO_TS + ", %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (ledger_identity) DO NOTHING",
                (
                    item.ledger_identity,
                    item.source_event_key,
                    item.protocol_fingerprint,
                    item.activation_id,
                    str(payload["source_identity"]),
                    str(payload["symbol"]),
                    signal_time_us,
                    signal_time_us,  # the µs value inside _US_TO_TS
                    int(payload["signal_time_ms"]),
                    int(payload["captured_at_us"]),
                    int(payload["source_freeze_ts_us"]),
                    int(item.h1e_freeze_ts_us),
                    str(payload["lineage_hash"]),
                    str(payload.get("source_fingerprint", "")),
                    item.preserved_hash,
                    _json_text(payload),
                ),
            )
            if item.preserved_hash:
                statement.execute(
                    "SELECT preserved_hash FROM " + _SCHEMA + ".h1e_event "
                    "WHERE ledger_identity = %s FOR UPDATE",
                    (item.ledger_identity,),
                )
                existing_hash = statement.fetchone()[0]
                if existing_hash != item.preserved_hash:
                    raise LedgerIdentityConflict(
                        f"{item.ledger_identity}: committed preserved_hash "
                        f"{existing_hash} != delivered {item.preserved_hash}"
                    )

            # exactly-once effect: an identical redelivery never appends
            if item.event_kind in (STATUS_CLASSIFIED, STATUS_DEFERRED):
                statement.execute(
                    "SELECT detail->'classification' FROM " + _SCHEMA
                    + ".h1e_status_event WHERE ledger_identity = %s "
                    "AND event_kind IN ('CLASSIFIED', 'DEFERRED') "
                    "ORDER BY id DESC LIMIT 1",
                    (item.ledger_identity,),
                )
                last = statement.fetchone()
                last_classification = last[0] if last else None
                if (
                    last_classification is not None
                    and item.classification is not None
                    and last_classification == _norm(item.classification)
                ):
                    continue

            classification = (
                None if item.classification is None
                else dict(item.classification)
            )
            statement.execute(
                "INSERT INTO " + _SCHEMA + ".h1e_status_event "
                "(ledger_identity, event_kind, detail) VALUES (%s, %s, "
                "%s::jsonb)",
                (
                    item.ledger_identity,
                    item.event_kind,
                    _json_text({
                        "classification": classification,
                        "attempt_count": int(item.attempt_count),
                        "captured_at_us": int(item.captured_at_us),
                    }),
                ),
            )
            if item.event_kind in (STATUS_CLASSIFIED, STATUS_DEFERRED):
                record = classification or {}
                statement.execute(
                    "INSERT INTO " + _SCHEMA + ".h1e_classification "
                    "(ledger_identity, classification_version, attempt_count, "
                    "status, asof_us, eligible, eligibility_reason, "
                    "non_eligible_codes, regime_4h, bos_4h, regime_1d, bos_1d, "
                    "combined_regime, coverage_4h_status, coverage_1d_status, "
                    "coverage_4h_present, coverage_1d_present, "
                    "coverage_4h_missing_open_ms, coverage_1d_missing_open_ms, "
                    "snapshot_sha256, diagnostics) VALUES (%s, %s, %s, %s, "
                    "%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb, %s::jsonb, %s, %s::jsonb)",
                    (
                        item.ledger_identity,
                        str(record.get("version", "")),
                        int(item.attempt_count),
                        item.event_kind,
                        int(record.get("asof_us", 0)),
                        bool(record.get("eligible", False)),
                        str(record.get("eligibility_reason", "")),
                        _json_text(list(record.get("non_eligible_codes", []))),
                        record.get("regime_4h"),
                        record.get("bos_4h"),
                        record.get("regime_1d"),
                        record.get("bos_1d"),
                        record.get("combined_regime"),
                        str(record.get("coverage_4h_status", "")),
                        str(record.get("coverage_1d_status", "")),
                        int(record.get("coverage_4h_present", 0)),
                        int(record.get("coverage_1d_present", 0)),
                        _json_text(
                            list(record.get("coverage_4h_missing_open_ms", []))),
                        _json_text(
                            list(record.get("coverage_1d_missing_open_ms", []))),
                        str(record.get("snapshot_sha256", "")),
                        _json_text(
                            {"captured_at_us": int(item.captured_at_us)}),
                    ),
                )

        # conflicts — EXACTLY ONCE per conflict key (B2)
        for conflict in conflicts:
            payload = dict(conflict)
            statement.execute(
                "INSERT INTO " + _SCHEMA + ".h1e_conflict_event "
                "(source_event_key, ledger_identity, existing_preserved_hash, "
                "delivered_preserved_hash, payload) VALUES (%s, %s, %s, %s, "
                "%s::jsonb) ON CONFLICT (source_event_key, "
                "existing_preserved_hash, delivered_preserved_hash) "
                "DO NOTHING",
                (
                    str(payload["source_event_key"]),
                    str(payload["ledger_identity"]),
                    str(payload["existing_preserved_hash"]),
                    str(payload["delivered_preserved_hash"]),
                    _json_text(payload.get("delivered_payload", {})),
                ),
            )

        # durable pending-retry state — atomic with everything else (B1)
        for update in pending:
            if update.retry is None:
                statement.execute(
                    "DELETE FROM " + _SCHEMA + ".h1e_pending_retry "
                    "WHERE ledger_identity = %s",
                    (update.ledger_identity,),
                )
            else:
                retry = update.retry
                statement.execute(
                    "INSERT INTO " + _SCHEMA + ".h1e_pending_retry "
                    "(ledger_identity, source_event_key, attempt_count, "
                    "deferred_reason, next_attempt_us, preserved_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT "
                    "(ledger_identity) DO UPDATE SET attempt_count = "
                    "EXCLUDED.attempt_count, deferred_reason = "
                    "EXCLUDED.deferred_reason, next_attempt_us = "
                    "EXCLUDED.next_attempt_us, preserved_hash = "
                    "EXCLUDED.preserved_hash, updated_at = NOW()",
                    (
                        retry.ledger_identity,
                        retry.source_event_key,
                        int(retry.attempt_count),
                        str(retry.deferred_reason),
                        int(retry.next_attempt_us),
                        retry.preserved_hash,
                    ),
                )

        # delivery checkpoint — same transaction (monotonic trigger guards)
        captured_us, source_event_key = checkpoint.as_tuple()
        statement.execute(
            "INSERT INTO " + _SCHEMA + ".h1e_checkpoint "
            "(worker_id, captured_us, source_event_key) VALUES (%s, %s, %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET captured_us = "
            "EXCLUDED.captured_us, source_event_key = "
            "EXCLUDED.source_event_key",
            (self.worker_id, int(captured_us), str(source_event_key)),
        )


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


_DB_ERROR_MAP: tuple[tuple[str, type[BaseException]], ...] = (
    ("H1E_PRE_FREEZE_EVENT", PreFreezeWrite),
    ("H1E_FREEZE_TS_MISMATCH", FreezeTsMismatch),
    ("H1E_FINGERPRINT_MISMATCH", ActivationFingerprintMismatch),
    ("H1E_NO_APPROVED_ACTIVATION", NoApprovedActivation),
    ("H1E_CHECKPOINT_REGRESSION", CheckpointRegression),
    ("H1E_LEDGER_IMMUTABLE", LedgerIdentityConflict),
)


def _reraise_translated(exc: BaseException) -> None:
    """Translate database guard errors back to the frozen classes."""
    text = str(exc)
    for marker, exception_class in _DB_ERROR_MAP:
        if marker in text:
            raise exception_class(text) from exc
    raise exc
