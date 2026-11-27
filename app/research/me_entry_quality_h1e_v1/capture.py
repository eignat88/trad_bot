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
  snapshot kept verbatim);
* a redelivery with DIFFERENT preserved facts is a real integrity conflict
  (``H1E_EVENT_KEY_LINEAGE_CONFLICT``): isolated in the conflict ledger,
  NEVER an overwrite, fail-closed for that identity;
* ledger inserts and the checkpoint advance in ONE atomic transaction
  (:meth:`H1EStore.commit_batch`); an interrupted transaction commits
  NOTHING and the next cycle re-reads from the previous checkpoint
  (at-least-once redelivery → idempotent effect);
* the H1-E freeze gate is fail-closed: ``signal_time > freeze_ts`` STRICTLY,
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
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    AdmissionDecision,
    H1EFreeze,
    H1ESourceEvent,
    admit_source_row,
    ledger_identity,
)

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


class H1EStore(Protocol):
    """Ledger store with ATOMIC batch commit (insert + checkpoint)."""

    def lookup(self, ledger_identity: str) -> Any | None: ...

    def commit_batch(
        self, items: Sequence[LedgerWrite], checkpoint: CaptureCursor,
    ) -> None: ...

    def record_conflict(self, payload: Mapping[str, Any]) -> None: ...


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


def run_capture_cycle(
    *,
    reader: H1ESourceReader,
    store: H1EStore,
    classifier: Classifier,
    freeze: H1EFreeze,
    cursor: CaptureCursor,
    ceiling_captured_us: int | None = None,
    config: CaptureConfig | None = None,
) -> CycleReport:
    """Run ONE deterministic capture cycle (bounded work, atomic commit).

    The cycle reads at most ``config.batch_limit`` rows after ``cursor``,
    admits them through the fail-closed H1-E checks, classifies each new
    identity through the injected classifier and commits every effect PLUS
    the advanced checkpoint in ONE atomic transaction.
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

    rows = list(reader.read_after(
        cursor, limit=int(cfg.batch_limit), ceiling_captured_us=ceiling_captured_us))

    rejections: list[RejectionRecord] = []
    conflicts: list[ConflictRecord] = []
    writes: list[LedgerWrite] = []
    deferred_keys: list[str] = []
    admitted = 0
    duplicates = 0

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
        prior_deferrals = 0
        if existing is not None:
            existing_hash = getattr(existing, "preserved_hash", "")
            if existing_hash != event.preserved_hash:
                conflicts.append(ConflictRecord(
                    source_event_key=event.source_event_key,
                    existing_preserved_hash=existing_hash,
                    delivered_preserved_hash=event.preserved_hash,
                    detail="redelivery with different preserved identity facts",
                ))
                store.record_conflict({
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
            # a DEFERRED classification is re-attempted on redelivery
            # (at-least-once delivery, bounded deferral budget).
            prior_deferrals = int(getattr(existing, "attempt_count", 1))

        produced = classifier(event, prior_deferrals=prior_deferrals)
        record: ClassificationRecord = (
            produced.record if hasattr(produced, "record") else produced
        )
        attempt_count = prior_deferrals + 1
        writes.append(LedgerWrite(
            ledger_identity=identity,
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
        ))
        admitted += 1
        if record.status == STATUS_DEFERRED:
            deferred_keys.append(event.source_event_key)

    checkpoint = CaptureCursor.after_rows(rows) if rows else cursor
    try:
        store.commit_batch(writes, checkpoint)
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
    )
