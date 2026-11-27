"""Issue #324 / H1-E.2 — ``H1E_PROSPECTIVE_EVENT_CAPTURE_V1``.

Deterministic, bounded prospective event capture over the independent #305
source — with an INJECTABLE reader (offline fakes in Phase A; the real
read-only SQL reader is Phase B) and an INJECTABLE ledger store.

Cursor semantics (monotonic composite key, stable ordering):

* the delivery cursor is the CAPTURE order ``(captured_at_us,
  source_event_key)`` — never ``signal_time`` — so a late-committed event
  with an earlier signal time is still delivered;
* ``captured_at`` ties are broken by ``source_event_key``; a batch boundary
  can never drop a row;
* the cursor is a STRICTLY monotonic composite: it advances only after an
  atomic commit.

Delivery semantics (at-least-once delivery, exactly-once effect):

* every read row is accounted for — ``read == admitted + rejected +
  duplicates + conflicts`` (no silent skips);
* a redelivery of an already committed identity with the SAME preserved
  facts is an idempotent ``DUPLICATE`` (no second ledger row, first-capture
  snapshot kept verbatim) — INCLUDING queued ``DEFERRED`` identities:
  redelivery never re-triggers classification;
* a redelivery with DIFFERENT preserved facts is a real integrity conflict
  (``H1E_EVENT_KEY_LINEAGE_CONFLICT``): isolated in the conflict journal
  INSIDE the batch transaction (recorded exactly once per conflict key),
  NEVER an overwrite, fail-closed for that identity (review fix B2);
* ledger writes, reject/conflict journals, the durable pending-retry state
  and the checkpoint advance in ONE atomic transaction
  (:meth:`H1EStore.commit_batch`); an interrupted transaction commits
  NOTHING and the next cycle re-reads from the previous checkpoint
  (at-least-once redelivery → idempotent effect).  There is NO durable
  mutation path outside ``commit_batch``.

Durable pending-retry semantics (review fix B1):

* a ``DEFERRED`` classification is queued in a DURABLE pending-retry state
  that is INDEPENDENT of the source cursor — the event is re-attempted
  even after the cursor advanced and after a restart, so a deferred event
  is NEVER lost and NEVER re-counted as a new delivery;
* the queue stores the event identity, the attempt count, the deferral
  reason and the next-attempt time; due entries are re-processed in
  deterministic order and always at the ORIGINAL ``signal_time`` ASOF
  (no later candles — the classification contract is unchanged);
* attempts are BOUNDED (``config.max_attempts``): once the budget is spent
  the event becomes a TERMINAL non-eligible result (``H1E_RETRY_BUDGET_EXHAUSTED``)
  without further classifier calls; terminal events and conflicted
  identities are never re-executed.

The H1-E freeze gate is fail-closed: ``signal_time > freeze_ts`` STRICTLY,
preserved excluded windows rejected, and a SYNTHETIC fixture freeze is
refused unless explicitly allowed (offline tests).

This module performs NO I/O of its own: the reader, the store and the
classifier are injected callables/objects.  No production service is bound,
and old events are never used as live samples (pre-freeze rows are recorded
as explicit rejections).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.research.me_entry_quality_h1c_v1.event_adapter import parse_utc_us
from app.research.me_entry_quality_h1e_v1.classification import (
    ClassificationRecord,
    STATUS_DEFERRED,
    terminal_noneligible_record,
    NONELIGIBLE_RETRY_BUDGET_EXHAUSTED,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    AdmissionDecision,
    H1EFreeze,
    H1ESourceEvent,
    admit_source_row,
    ledger_identity,
)
from app.research.me_entry_quality_h1e_v1.protocol import EXPERIMENT_ID

H1E_CAPTURE_VERSION = "H1E_PROSPECTIVE_EVENT_CAPTURE_V1"

EVENT_KIND_ACCEPTED = "ACCEPTED"
EVENT_KIND_CLASSIFIED = "CLASSIFIED"
EVENT_KIND_REJECTED = "REJECTED"
EVENT_KIND_DEFERRED = "DEFERRED"
EVENT_KIND_DUPLICATE = "DUPLICATE"
EVENT_KIND_CONFLICT = "CONFLICT"


# ─── cursor ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, order=True)
class CaptureCursor:
    """Monotonic composite delivery cursor ``(captured_us, source_event_key)``.

    The zero cursor precedes every real row.  Total order is the tuple
    order — deterministic across ``PYTHONHASHSEED`` by construction.
    """

    captured_us: int = 0
    source_event_key: str = ""

    def as_tuple(self) -> tuple[int, str]:
        return (int(self.captured_us), str(self.source_event_key))

    @classmethod
    def after_rows(cls, rows: Sequence[Mapping[str, Any]]) -> "CaptureCursor":
        """Exclusive cursor of the LAST row of a capture-ordered batch."""
        if not rows:
            return cls()
        last = rows[-1]
        captured_us = parse_utc_us(
            last.get("captured_at"), field_name="captured_at")
        key = last.get("source_event_key")
        return cls(captured_us=captured_us,
                   source_event_key=key if isinstance(key, str) else "")


# ─── durable pending-retry contract (B1) ────────────────────────────────────


@dataclass(frozen=True)
class PendingRetry:
    """Durable state of ONE deferred event awaiting re-processing.

    Independent of the delivery cursor: the event is re-attempted from this
    queue even after the source cursor advanced and after a restart.
    ``attempt_count`` counts classification attempts already made;
    ``next_attempt_us`` is the earliest instant of the next attempt.
    """

    ledger_identity: str
    source_event_key: str
    attempt_count: int
    deferred_reason: str
    next_attempt_us: int
    preserved_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_identity": self.ledger_identity,
            "source_event_key": self.source_event_key,
            "attempt_count": int(self.attempt_count),
            "deferred_reason": self.deferred_reason,
            "next_attempt_us": int(self.next_attempt_us),
            "preserved_hash": self.preserved_hash,
        }


@dataclass(frozen=True)
class PendingRetryUpdate:
    """One atomic pending-queue mutation (``retry=None`` removes the entry)."""

    ledger_identity: str
    retry: PendingRetry | None = None


# ─── injected collaborators ────────────────────────────────────────────────


class H1ESourceReader(Protocol):
    """Read-only #305 delivery source (SELECT-only in Phase B)."""

    def read_after(
        self,
        cursor: CaptureCursor,
        *,
        limit: int,
        ceiling_captured_us: int | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class LedgerWrite:
    """One immutable ledger effect to commit atomically with the checkpoint."""

    ledger_identity: str
    source_event_key: str
    protocol_fingerprint: str
    event_kind: str
    preserved_hash: str
    payload: Mapping[str, Any]
    classification: Mapping[str, Any] | None
    attempt_count: int
    captured_at_us: int
    #: explicit link to the approved H1-E activation (B3)
    activation_id: str = ""
    #: the H1-E freeze instant this event is evaluated against (B3)
    h1e_freeze_ts_us: int = 0


class H1EStore(Protocol):
    """Ledger store with ONE ATOMIC batch transaction.

    ``commit_batch`` applies ledger writes, reject/conflict journals, the
    durable pending-retry state and the checkpoint TOGETHER — there is no
    other durable mutation path (review fix B2).
    """

    def lookup(self, ledger_identity: str) -> Any | None: ...

    def pending_retries(self) -> Sequence[PendingRetry]: ...

    def commit_batch(
        self,
        items: Sequence[LedgerWrite],
        checkpoint: CaptureCursor,
        *,
        conflicts: Sequence[Mapping[str, Any]] = (),
        pending: Sequence[PendingRetryUpdate] = (),
    ) -> None: ...


Classifier = Callable[..., Any]
"""Injected classifier: ``classifier(event, prior_deferrals=0)`` returning
a :class:`~app.research.me_entry_quality_h1e_v1.classification.
ClassificationRecord` (or a ``.record``-carrying outcome)."""


# ─── cycle report ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RejectionRecord:
    source_event_key: str
    reason: str
    detail: str
    captured_at_us: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_key": self.source_event_key,
            "reason": self.reason,
            "detail": self.detail,
            "captured_at_us": self.captured_at_us,
        }


@dataclass(frozen=True)
class ConflictRecord:
    source_event_key: str
    existing_preserved_hash: str
    delivered_preserved_hash: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_key": self.source_event_key,
            "existing_preserved_hash": self.existing_preserved_hash,
            "delivered_preserved_hash": self.delivered_preserved_hash,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CycleReport:
    """Deterministic accounting of ONE capture cycle."""

    version: str
    cursor_before: CaptureCursor
    cursor_after: CaptureCursor
    read_count: int
    admitted_count: int
    rejections: tuple[RejectionRecord, ...]
    duplicates: int
    conflicts: tuple[ConflictRecord, ...]
    deferred_keys: tuple[str, ...]
    committed_count: int
    committed: bool
    commit_error: str = ""
    #: identities re-attempted from the durable pending queue this cycle
    retried_keys: tuple[str, ...] = ()
    #: retries that exhausted the bounded budget (terminal non-eligible)
    retry_terminal_keys: tuple[str, ...] = ()
    #: pending entries skipped WITHOUT execution (terminal/conflicted/etc.)
    retry_skipped_keys: tuple[str, ...] = ()

    @property
    def accounted_count(self) -> int:
        return (
            self.admitted_count + len(self.rejections) + self.duplicates
            + len(self.conflicts)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cursor_before": list(self.cursor_before.as_tuple()),
            "cursor_after": list(self.cursor_after.as_tuple()),
            "read_count": self.read_count,
            "admitted_count": self.admitted_count,
            "rejections": [r.to_dict() for r in self.rejections],
            "duplicates": self.duplicates,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "deferred_keys": list(self.deferred_keys),
            "committed_count": self.committed_count,
            "committed": self.committed,
            "commit_error": self.commit_error,
            "retried_keys": list(self.retried_keys),
            "retry_terminal_keys": list(self.retry_terminal_keys),
            "retry_skipped_keys": list(self.retry_skipped_keys),
        }


# ─── the capture worker ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaptureConfig:
    """Bounded-work configuration (fail-closed defaults)."""

    batch_limit: int = 200
    #: offline tests ONLY — the worker refuses synthetic fixture freezes
    #: unless this is explicitly enabled.
    allow_fixture_freeze: bool = False
    #: expected #305 source pipeline fingerprint (``""`` = not pinned)
    expected_fingerprint: str = ""
    #: hard bound on TOTAL classification attempts per event — the durable
    #: retry queue is drained fail-closed even if the injected classifier
    #: keeps deferring (review fix B1).
    max_attempts: int = 3
    #: earliest-delay before a deferred event becomes due again (µs);
    #: ``0`` = due on the next cycle.
    retry_backoff_us: int = 0


def run_capture_cycle(
    *,
    reader: H1ESourceReader,
    store: H1EStore,
    classifier: Classifier,
    freeze: H1EFreeze,
    cursor: CaptureCursor,
    ceiling_captured_us: int | None = None,
    config: CaptureConfig | None = None,
    now_us: int | None = None,
) -> CycleReport:
    """Run ONE deterministic capture cycle (bounded work, atomic commit).

    The cycle (1) re-processes DUE entries of the durable pending-retry
    queue independent of the source cursor, (2) reads at most
    ``config.batch_limit`` rows after ``cursor``, (3) admits them through
    the fail-closed H1-E checks and classifies each NEW identity through
    the injected classifier, and (4) commits every effect — ledger writes,
    conflicts, pending-retry state and the advanced checkpoint — in ONE
    atomic transaction.  ``now_us`` is the caller-supplied cycle instant
    used for retry scheduling (no clock is read here); when omitted every
    pending entry is considered due.
    """
    cfg = config or CaptureConfig()
    if not isinstance(freeze, H1EFreeze):
        raise ValueError("freeze must be an H1EFreeze")
    if freeze.is_fixture and not cfg.allow_fixture_freeze:
        raise ValueError(
            "synthetic fixture freeze refused outside offline tests"
        )
    if not isinstance(cfg.batch_limit, int) or isinstance(cfg.batch_limit, bool):
        raise ValueError("batch_limit must be a real integer")
    if cfg.batch_limit < 1:
        raise ValueError("batch_limit must be positive")
    if not isinstance(cfg.max_attempts, int) or isinstance(cfg.max_attempts, bool):
        raise ValueError("max_attempts must be a real integer")
    if cfg.max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    writes: list[LedgerWrite] = []
    conflict_payloads: list[Mapping[str, Any]] = []
    pending_updates: list[PendingRetryUpdate] = []
    rejections: list[RejectionRecord] = []
    conflicts: list[ConflictRecord] = []
    deferred_keys: list[str] = []
    retried_keys: list[str] = []
    retry_terminal_keys: list[str] = []
    retry_skipped_keys: list[str] = []
    admitted = 0
    duplicates = 0

    # ── 1. durable pending-retry queue (independent of the cursor) ──────
    pending_map: dict[str, PendingRetry] = {
        retry.ledger_identity: retry for retry in store.pending_retries()
    }
    for retry in store.pending_retries():
        if now_us is not None and int(retry.next_attempt_us) > int(now_us):
            continue  # not due yet
        entry = store.lookup(retry.ledger_identity)
        if entry is None or getattr(entry, "current_status", "") != STATUS_DEFERRED:
            # terminal / conflicted / vanished identities are NEVER
            # re-executed — the stale queue entry is dropped atomically.
            retry_skipped_keys.append(retry.source_event_key)
            pending_updates.append(PendingRetryUpdate(retry.ledger_identity))
            pending_map.pop(retry.ledger_identity, None)
            continue
        if str(getattr(entry, "preserved_hash", "")) != retry.preserved_hash:
            raise ValueError(
                "H1E_PENDING_IDENTITY_MISMATCH: durable retry state does "
                "not match the committed preserved identity"
            )
        event = H1ESourceEvent.from_dict(entry.payload)
        if int(retry.attempt_count) >= int(cfg.max_attempts):
            # bounded budget exhausted — TERMINAL fail-closed result,
            # NO further classifier invocation (no endless attempts).
            record = terminal_noneligible_record(
                event,
                code=NONELIGIBLE_RETRY_BUDGET_EXHAUSTED,
                detail=(
                    f"attempt_count={retry.attempt_count} >= "
                    f"max_attempts={cfg.max_attempts}: "
                    f"{retry.deferred_reason}"
                ),
                attempts=int(retry.attempt_count),
            )
            writes.append(_classified_write(
                event, freeze, record, attempt_count=int(retry.attempt_count)))
            retry_terminal_keys.append(event.source_event_key)
            pending_updates.append(PendingRetryUpdate(retry.ledger_identity))
            pending_map.pop(retry.ledger_identity, None)
            continue
        produced = classifier(event, prior_deferrals=int(retry.attempt_count))
        record = produced.record if hasattr(produced, "record") else produced
        attempt_count = int(retry.attempt_count) + 1
        writes.append(_classified_write(
            event, freeze, record, attempt_count=attempt_count))
        retried_keys.append(event.source_event_key)
        if record.status == STATUS_DEFERRED:
            pending_updates.append(PendingRetryUpdate(
                retry.ledger_identity,
                PendingRetry(
                    ledger_identity=retry.ledger_identity,
                    source_event_key=event.source_event_key,
                    attempt_count=attempt_count,
                    deferred_reason=_deferral_reason(record),
                    next_attempt_us=(
                        (int(now_us) if now_us is not None else 0)
                        + int(cfg.retry_backoff_us)
                    ),
                    preserved_hash=event.preserved_hash,
                ),
            ))
        else:
            pending_updates.append(PendingRetryUpdate(retry.ledger_identity))
            pending_map.pop(retry.ledger_identity, None)

    # ── 2. source rows after the cursor (capture order) ────────────────
    rows = list(reader.read_after(
        cursor, limit=int(cfg.batch_limit), ceiling_captured_us=ceiling_captured_us))

    previous_cursor = cursor.as_tuple()
    for row in rows:
        # stable capture order is a READER contract — a violation is a
        # fail-closed error, never a silent reordering.
        row_key = row.get("source_event_key") if isinstance(row, Mapping) else None
        try:
            row_captured_us = parse_utc_us(
                row.get("captured_at"), field_name="captured_at")
        except ValueError:
            row_captured_us = 0
        current_cursor = (
            row_captured_us, row_key if isinstance(row_key, str) else "")
        if current_cursor < previous_cursor:
            raise ValueError(
                "H1E_READER_ORDER_VIOLATION: rows must arrive in "
                "(captured_at, source_event_key) order"
            )
        previous_cursor = current_cursor

        decision: AdmissionDecision = admit_source_row(
            row, freeze=freeze, expected_fingerprint=cfg.expected_fingerprint)
        captured_us = (
            decision.event.captured_at_us
            if decision.event is not None
            else row_captured_us
        )
        if not decision.accepted:
            rejections.append(RejectionRecord(
                source_event_key=(
                    row_key if isinstance(row_key, str) else ""),
                reason=decision.reason,
                detail=decision.detail,
                captured_at_us=captured_us,
            ))
            writes.append(LedgerWrite(
                ledger_identity=(
                    f"reject:{row_key if isinstance(row_key, str) else ''}"
                    f":{captured_us}:{decision.reason}"
                ),
                source_event_key=(
                    row_key if isinstance(row_key, str) else ""),
                protocol_fingerprint=freeze.protocol_fingerprint,
                event_kind=EVENT_KIND_REJECTED,
                preserved_hash="",
                payload={"reason": decision.reason, "detail": decision.detail},
                classification=None,
                attempt_count=1,
                captured_at_us=captured_us,
            ))
            continue

        event: H1ESourceEvent = decision.event  # type: ignore[assignment]
        identity = ledger_identity(event.source_event_key, freeze.protocol_fingerprint)
        existing = store.lookup(identity)
        if existing is not None:
            existing_hash = getattr(existing, "preserved_hash", "")
            if existing_hash != event.preserved_hash:
                conflicts.append(ConflictRecord(
                    source_event_key=event.source_event_key,
                    existing_preserved_hash=existing_hash,
                    delivered_preserved_hash=event.preserved_hash,
                    detail="redelivery with different preserved identity facts",
                ))
                # B2: the conflict journal is written INSIDE the batch
                # transaction — never before it, never outside it.
                conflict_payloads.append({
                    "source_event_key": event.source_event_key,
                    "ledger_identity": identity,
                    "existing_preserved_hash": existing_hash,
                    "delivered_preserved_hash": event.preserved_hash,
                    "delivered_payload": event.to_dict(),
                })
                continue
            if getattr(existing, "current_status", "") != STATUS_DEFERRED:
                # committed identity, same preserved facts — idempotent
                # no-op (exactly-once effect); first capture kept verbatim.
                duplicates += 1
                continue
            # a queued DEFERRED identity is re-attempted ONLY via the
            # durable pending queue (B1) — redelivery is idempotent.
            if identity not in pending_map:
                # self-heal: the durable queue lost an entry (never the
                # event) — re-queue it atomically with this batch.
                pending_updates.append(PendingRetryUpdate(
                    identity,
                    PendingRetry(
                        ledger_identity=identity,
                        source_event_key=event.source_event_key,
                        attempt_count=int(getattr(existing, "attempt_count", 1)),
                        deferred_reason="H1E_PENDING_SELF_HEAL",
                        next_attempt_us=(
                            (int(now_us) if now_us is not None else 0)
                            + int(cfg.retry_backoff_us)
                        ),
                        preserved_hash=event.preserved_hash,
                    ),
                ))
                pending_map[identity] = pending_updates[-1].retry  # type: ignore[arg-type]
                admitted += 1
                continue
            duplicates += 1
            continue

        produced = classifier(event, prior_deferrals=0)
        record: ClassificationRecord = (
            produced.record if hasattr(produced, "record") else produced
        )
        writes.append(_classified_write(event, freeze, record, attempt_count=1))
        admitted += 1
        if record.status == STATUS_DEFERRED:
            deferred_keys.append(event.source_event_key)
            pending_updates.append(PendingRetryUpdate(
                identity,
                PendingRetry(
                    ledger_identity=identity,
                    source_event_key=event.source_event_key,
                    attempt_count=1,
                    deferred_reason=_deferral_reason(record),
                    next_attempt_us=(
                        (int(now_us) if now_us is not None else 0)
                        + int(cfg.retry_backoff_us)
                    ),
                    preserved_hash=event.preserved_hash,
                ),
            ))
            pending_map[identity] = pending_updates[-1].retry  # type: ignore[arg-type]

    checkpoint = CaptureCursor.after_rows(rows) if rows else cursor
    try:
        store.commit_batch(
            writes, checkpoint,
            conflicts=conflict_payloads, pending=pending_updates)
    except Exception as exc:  # noqa: BLE001 — atomic failure is a report fact
        return CycleReport(
            version=H1E_CAPTURE_VERSION,
            cursor_before=cursor,
            cursor_after=cursor,
            read_count=len(rows),
            admitted_count=admitted,
            rejections=tuple(rejections),
            duplicates=duplicates,
            conflicts=tuple(conflicts),
            deferred_keys=tuple(deferred_keys),
            committed_count=0,
            committed=False,
            commit_error=str(exc),
            retried_keys=tuple(retried_keys),
            retry_terminal_keys=tuple(retry_terminal_keys),
            retry_skipped_keys=tuple(retry_skipped_keys),
        )

    return CycleReport(
        version=H1E_CAPTURE_VERSION,
        cursor_before=cursor,
        cursor_after=checkpoint,
        read_count=len(rows),
        admitted_count=admitted,
        rejections=tuple(rejections),
        duplicates=duplicates,
        conflicts=tuple(conflicts),
        deferred_keys=tuple(deferred_keys),
        committed_count=len(writes),
        committed=True,
        retried_keys=tuple(retried_keys),
        retry_terminal_keys=tuple(retry_terminal_keys),
        retry_skipped_keys=tuple(retry_skipped_keys),
    )


def _deferral_reason(record: ClassificationRecord) -> str:
    codes = tuple(record.non_eligible_codes or ())
    return codes[0] if codes else str(record.eligibility_reason)


def _classified_write(
    event: H1ESourceEvent,
    freeze: H1EFreeze,
    record: ClassificationRecord,
    *,
    attempt_count: int,
) -> LedgerWrite:
    return LedgerWrite(
        ledger_identity=ledger_identity(
            event.source_event_key, freeze.protocol_fingerprint),
        source_event_key=event.source_event_key,
        protocol_fingerprint=freeze.protocol_fingerprint,
        event_kind=(
            EVENT_KIND_DEFERRED
            if record.status == STATUS_DEFERRED
            else EVENT_KIND_CLASSIFIED
        ),
        preserved_hash=event.preserved_hash,
        payload=event.to_dict(),
        classification=record.to_dict(),
        attempt_count=attempt_count,
        captured_at_us=event.captured_at_us,
        activation_id=EXPERIMENT_ID,
        h1e_freeze_ts_us=int(freeze.freeze_ts_us),
    )
