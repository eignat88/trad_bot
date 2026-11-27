"""Issue #324 / H1-E.4 — ``H1E_PROSPECTIVE_LEDGER_V1``.

Append-only, RESEARCH-ONLY prospective ledger semantics.  This module is
the executable REFERENCE of the DDL proposal
(``sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`` — **DO NOT
APPLY**; no DDL is ever executed by this code):

* unique ledger identity per ``(source_event_key, H1-E protocol
  fingerprint)`` — one immutable first-capture record per identity;
* conflicting payload isolation: a redelivery with different preserved
  facts is appended to the CONFLICT journal and never overwrites the
  original record (``H1E_EVENT_KEY_LINEAGE_CONFLICT``);
* atomic checkpoint + ledger insert: :meth:`InMemoryH1EStore.commit_batch`
  applies ALL writes and the checkpoint in ONE transaction — an
  interrupted commit applies NOTHING and the checkpoint never advances
  past uncommitted work;
* status transitions are APPEND-ONLY journal events; the current status is
  always DERIVED from the journal — immutable evidence (payload,
  classification, snapshot hashes) is never rewritten;
* deterministic recovery: replaying the same deliveries after a crash
  yields the identical ledger state (idempotent commit, monotonic
  checkpoint).

Namespace separation: the proposed schema
``research_me_entry_quality_h1e`` is disjoint from #305
(``research_me_upstream_coverage``), from production (``dds``/``market``)
and from every existing OOS namespace.

Retention / recovery (documented proposal): classification payloads and
provenance snapshot hashes are retained for the lifetime of the experiment
(they ARE the evidence); reject/conflict journals are retained forever
(append-only audit); the checkpoint is a single row updated in the same
transaction as every batch.  Deterministic recovery = restart from the
committed checkpoint and re-deliver (at-least-once) — exactly-once effect
comes from identity idempotency, never from deleting or rewriting rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureCursor,
    LedgerWrite,
)

H1E_LEDGER_VERSION = "H1E_PROSPECTIVE_LEDGER_V1"

#: proposed schema (DDL proposal only — never applied by this code)
LEDGER_SCHEMA = "research_me_entry_quality_h1e"

# ─── status vocabulary (append-only journal; status is DERIVED) ─────────────

STATUS_ACCEPTED = "ACCEPTED"
STATUS_CLASSIFIED = "CLASSIFIED"
STATUS_DEFERRED = "DEFERRED"
STATUS_REJECTED = "REJECTED"
STATUS_CONFLICT = "CONFLICT"

TERMINAL_STATUSES = frozenset({STATUS_CLASSIFIED, STATUS_REJECTED, STATUS_CONFLICT})


class LedgerIdentityConflict(RuntimeError):
    """A committed identity received a DIFFERENT preserved payload."""


class CheckpointRegression(RuntimeError):
    """A commit tried to move the checkpoint backwards."""


class SimulatedCrash(RuntimeError):
    """Offline fault-injection commit failure (tests ONLY — never production)."""


@dataclass(frozen=True)
class LedgerEntry:
    """Immutable first-capture record + derived state of ONE identity."""

    ledger_identity: str
    source_event_key: str
    protocol_fingerprint: str
    preserved_hash: str
    payload: Mapping[str, Any]
    journal: tuple[Mapping[str, Any], ...] = ()

    @property
    def classifications(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            entry["classification"]
            for entry in self.journal
            if entry.get("event_kind") in (STATUS_CLASSIFIED, STATUS_DEFERRED)
            and entry.get("classification") is not None
        )

    @property
    def attempt_count(self) -> int:
        return sum(
            1
            for entry in self.journal
            if entry.get("event_kind") in (STATUS_CLASSIFIED, STATUS_DEFERRED)
        )

    @property
    def current_status(self) -> str:
        """DERIVED from the append-only journal (last write wins on status)."""
        for entry in reversed(self.journal):
            kind = entry.get("event_kind")
            if kind in (STATUS_CLASSIFIED, STATUS_DEFERRED):
                return str(kind)
        return STATUS_ACCEPTED

    @property
    def final_classification(self) -> Mapping[str, Any] | None:
        for entry in reversed(self.journal):
            if entry.get("event_kind") == STATUS_CLASSIFIED:
                return entry.get("classification")
        return None


@dataclass
class InMemoryH1EStore:
    """Reference implementation of the proposed append-only ledger.

    Models EXACTLY the semantics of the DDL proposal (single-transaction
    batch + checkpoint, append-only journals, derived status).  Offline
    tests use it as the executable contract; the Phase B runtime will bind
    the same interface to PostgreSQL.
    """

    version: str = H1E_LEDGER_VERSION
    _entries: dict[str, LedgerEntry] = field(default_factory=dict, repr=False)
    _rejects: list[Mapping[str, Any]] = field(default_factory=list, repr=False)
    _conflicts: list[Mapping[str, Any]] = field(default_factory=list, repr=False)
    _checkpoint: CaptureCursor = field(default_factory=CaptureCursor, repr=False)

    # ── read side ────────────────────────────────────────────────────────

    def lookup(self, ledger_identity: str) -> LedgerEntry | None:
        return self._entries.get(ledger_identity)

    @property
    def checkpoint(self) -> CaptureCursor:
        return self._checkpoint

    @property
    def reject_events(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._rejects)

    @property
    def conflict_events(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._conflicts)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def current_status(self, ledger_identity: str) -> str:
        entry = self.lookup(ledger_identity)
        return "" if entry is None else entry.current_status

    def status_journal(self, ledger_identity: str) -> tuple[Mapping[str, Any], ...]:
        entry = self.lookup(ledger_identity)
        return () if entry is None else entry.journal

    # ── write side (atomic) ──────────────────────────────────────────────

    def record_conflict(self, payload: Mapping[str, Any]) -> None:
        """Append an isolated conflict record (never overwrites evidence)."""
        self._conflicts.append(dict(payload))

    def commit_batch(
        self, items: Sequence[LedgerWrite], checkpoint: CaptureCursor,
    ) -> None:
        """Apply ALL items + the checkpoint in ONE atomic transaction.

        All-or-nothing: any conflict / regression / injected crash leaves
        the store byte-identical to its previous state.  Identical
        redeliveries are idempotent no-ops (exactly-once effect).
        """
        if not isinstance(checkpoint, CaptureCursor):
            raise ValueError("checkpoint must be a CaptureCursor")
        if checkpoint.as_tuple() < self._checkpoint.as_tuple():
            raise CheckpointRegression(
                f"checkpoint {checkpoint.as_tuple()} < "
                f"{self._checkpoint.as_tuple()}")

        # ── validation phase (nothing applied yet) ──────────────────────
        for item in items:
            if item.event_kind in (STATUS_CLASSIFIED, STATUS_DEFERRED):
                existing = self._entries.get(item.ledger_identity)
                if (
                    existing is not None
                    and existing.preserved_hash != item.preserved_hash
                ):
                    raise LedgerIdentityConflict(
                        f"{item.ledger_identity}: committed preserved_hash "
                        f"{existing.preserved_hash} != delivered "
                        f"{item.preserved_hash}"
                    )

        # ── apply phase (single transaction) ────────────────────────────
        applied: list[tuple[str, LedgerEntry]] = []
        new_entries: dict[str, LedgerEntry] = dict(self._entries)
        new_rejects = list(self._rejects)
        for item in items:
            if item.event_kind == STATUS_REJECTED:
                new_rejects.append({
                    **dict(item.payload),
                    "source_event_key": item.source_event_key,
                    "protocol_fingerprint": item.protocol_fingerprint,
                    "captured_at_us": item.captured_at_us,
                })
                continue
            existing = new_entries.get(item.ledger_identity)
            if existing is None:
                entry = LedgerEntry(
                    ledger_identity=item.ledger_identity,
                    source_event_key=item.source_event_key,
                    protocol_fingerprint=item.protocol_fingerprint,
                    preserved_hash=item.preserved_hash,
                    payload=dict(item.payload),
                    journal=({
                        "event_kind": item.event_kind,
                        "classification": (
                            None if item.classification is None
                            else dict(item.classification)
                        ),
                        "attempt_count": item.attempt_count,
                        "captured_at_us": item.captured_at_us,
                    },),
                )
                new_entries[item.ledger_identity] = entry
                applied.append((item.ledger_identity, entry))
                continue
            # idempotent redelivery of the SAME preserved facts
            journal_entry = {
                "event_kind": item.event_kind,
                "classification": (
                    None if item.classification is None
                    else dict(item.classification)
                ),
                "attempt_count": item.attempt_count,
                "captured_at_us": item.captured_at_us,
            }
            if item.event_kind in (STATUS_CLASSIFIED, STATUS_DEFERRED):
                if existing.classifications and item.classification is not None:
                    if existing.classifications[-1] == dict(item.classification):
                        # exact redelivery — idempotent no-op
                        continue
            entry = LedgerEntry(
                ledger_identity=existing.ledger_identity,
                source_event_key=existing.source_event_key,
                protocol_fingerprint=existing.protocol_fingerprint,
                preserved_hash=existing.preserved_hash,
                payload=existing.payload,
                journal=existing.journal + (journal_entry,),
            )
            new_entries[item.ledger_identity] = entry
            applied.append((item.ledger_identity, entry))

        # crash AFTER the writes were staged but BEFORE commit — the whole
        # transaction rolls back (tests only, via subclass hooks).
        self._before_commit_hook(len(applied))

        self._entries = new_entries
        self._rejects = new_rejects
        self._checkpoint = checkpoint

    def _before_commit_hook(self, staged_effects: int) -> None:
        """Overridden by fault-injection harnesses (tests ONLY)."""


class FaultInjectingStore(InMemoryH1EStore):
    """Offline fault-injection wrapper (tests ONLY).

    ``fail_next_commit`` simulates a crash of the commit transaction: the
    batch and the checkpoint are rolled back together, exactly like a real
    single-transaction failure.
    """

    fail_next_commit: bool = False

    def _before_commit_hook(self, staged_effects: int) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise SimulatedCrash("injected commit crash (tests only)")
