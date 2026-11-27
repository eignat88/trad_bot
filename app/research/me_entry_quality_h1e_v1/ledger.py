"""Issue #324 / H1-E.4 — ``H1E_PROSPECTIVE_LEDGER_V1``.

Append-only, RESEARCH-ONLY prospective ledger semantics.  This module is
the executable REFERENCE of the DDL proposal
(``sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`` — **DO NOT
APPLY**; no DDL is ever executed by this code):

* unique ledger identity per ``(source_event_key, H1-E protocol
  fingerprint)`` — one immutable first-capture record per identity;
* conflicting payload isolation: a redelivery with different preserved
  facts is appended to the CONFLICT journal (idempotent — recorded EXACTLY
  ONCE per conflict key) and never overwrites the original record
  (``H1E_EVENT_KEY_LINEAGE_CONFLICT``);
* ONE atomic transaction per batch: :meth:`InMemoryH1EStore.commit_batch`
  applies ledger writes, reject/conflict journals, durable pending-retry
  state AND the checkpoint TOGETHER — an interrupted commit applies
  NOTHING (no partial records, the checkpoint never advances past
  uncommitted work) and there is NO other durable mutation path outside
  this transaction (review fix B2);
* durable pending-retry queue (review fix B1): deferred events live in an
  append-only-classified queue that is INDEPENDENT of the delivery cursor,
  survives restart (:meth:`to_dict` / :meth:`from_dict` model durable
  persistence), and is updated atomically with the classification journal
  and the checkpoint;
* status transitions are APPEND-ONLY journal events; the current status is
  always DERIVED from the journal — immutable evidence (payload,
  classification, snapshot hashes) is never rewritten; terminal events and
  conflicted identities are never re-executed;
* H1-E activation gate (review fix B3): a store bound to an owner-approved
  activation enforces ``signal_time_us > h1e_freeze_ts_us`` and the
  activation's protocol fingerprint fail-closed — mirroring the
  PostgreSQL trigger ``h1e_event_prospective_guard`` (a direct INSERT
  cannot bypass it).  The #305 source freeze stays an INDEPENDENT
  boundary.

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
    PendingRetry,
    PendingRetryUpdate,
)
from app.research.me_entry_quality_h1e_v1.contracts import H1EFreeze

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


class NoApprovedActivation(RuntimeError):
    """A write arrived without an approved H1-E activation (fail-closed)."""


class ActivationFingerprintMismatch(RuntimeError):
    """A write's protocol fingerprint differs from the approved activation."""


class FreezeTsMismatch(RuntimeError):
    """A write carries a different H1-E freeze than the approved activation."""


class PreFreezeWrite(RuntimeError):
    """A write tried to record an event at/before the H1-E freeze."""


# ─── durable pending-retry queue (B1) ───────────────────────────────────────
# ``PendingRetry`` / ``PendingRetryUpdate`` live in ``capture.py`` (the store
# interface contract) and are re-exported here for the ledger vocabulary.


# ─── immutable first-capture entry ──────────────────────────────────────────


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
    batch + pending-retry state + checkpoint, append-only journals, derived
    status, activation gate).  Offline tests use it as the executable
    contract; the Phase B runtime will bind the same interface to
    PostgreSQL.

    ``activation`` binds the store to ONE owner-approved H1-E activation —
    the in-code mirror of ``h1e_event_prospective_guard``: every non-reject
    write must carry the activation's protocol fingerprint and freeze
    instant, and its event ``signal_time_us`` must be STRICTLY after the
    H1-E freeze.  Missing (``None``) / mismatching / pre-freeze writes fail
    closed (never a silent drop, never a coercion).  Only REJECT-journal
    writes are ungated — they are exactly the evidence of refused rows.
    """

    version: str = H1E_LEDGER_VERSION
    activation: H1EFreeze | None = None
    _entries: dict[str, LedgerEntry] = field(default_factory=dict, repr=False)
    _rejects: list[Mapping[str, Any]] = field(default_factory=list, repr=False)
    _conflicts: list[Mapping[str, Any]] = field(default_factory=list, repr=False)
    _pending: dict[str, PendingRetry] = field(default_factory=dict, repr=False)
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

    def pending_retries(self) -> tuple[PendingRetry, ...]:
        """The durable retry queue in deterministic attempt order."""
        return tuple(
            self._pending[key]
            for key in sorted(
                self._pending,
                key=lambda k: (
                    self._pending[k].next_attempt_us,
                    self._pending[k].ledger_identity,
                ),
            )
        )

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def current_status(self, ledger_identity: str) -> str:
        entry = self.lookup(ledger_identity)
        return "" if entry is None else entry.current_status

    def status_journal(self, ledger_identity: str) -> tuple[Mapping[str, Any], ...]:
        entry = self.lookup(ledger_identity)
        return () if entry is None else entry.journal

    # ── write side (ONE atomic transaction — no other durable path) ─────

    def _check_activation(self, item: LedgerWrite) -> None:
        """Mirror of the DB ``h1e_event_prospective_guard`` (fail-closed)."""
        if self.activation is None:
            raise NoApprovedActivation(
                "no approved H1-E activation is bound to this store"
            )
        if item.protocol_fingerprint != self.activation.protocol_fingerprint:
            raise ActivationFingerprintMismatch(
                f"{item.ledger_identity}: protocol fingerprint "
                f"{item.protocol_fingerprint!r} != approved "
                f"{self.activation.protocol_fingerprint!r}"
            )
        if int(item.h1e_freeze_ts_us) != int(self.activation.freeze_ts_us):
            raise FreezeTsMismatch(
                f"{item.ledger_identity}: h1e_freeze_ts_us "
                f"{item.h1e_freeze_ts_us} != approved "
                f"{self.activation.freeze_ts_us}"
            )
        signal_time_us = int(item.payload.get("signal_time_us", 0))
        if signal_time_us <= int(self.activation.freeze_ts_us):
            raise PreFreezeWrite(
                f"{item.ledger_identity}: signal_time_us={signal_time_us} "
                f"<= h1e_freeze_ts_us={self.activation.freeze_ts_us}"
            )

    @staticmethod
    def _conflict_key(payload: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(payload.get("source_event_key", "")),
            str(payload.get("existing_preserved_hash", "")),
            str(payload.get("delivered_preserved_hash", "")),
        )

    def commit_batch(
        self,
        items: Sequence[LedgerWrite],
        checkpoint: CaptureCursor,
        *,
        conflicts: Sequence[Mapping[str, Any]] = (),
        pending: Sequence[PendingRetryUpdate] = (),
    ) -> None:
        """Apply writes + conflicts + pending-retry state + checkpoint atomically.

        All-or-nothing: any conflict / regression / activation-gate
        violation / injected crash leaves the store byte-identical to its
        previous state — there is NO partial batch.  Identical redeliveries
        are idempotent no-ops and one conflict is recorded EXACTLY ONCE
        (exactly-once effect).
        """
        if not isinstance(checkpoint, CaptureCursor):
            raise ValueError("checkpoint must be a CaptureCursor")
        if checkpoint.as_tuple() < self._checkpoint.as_tuple():
            raise CheckpointRegression(
                f"checkpoint {checkpoint.as_tuple()} < "
                f"{self._checkpoint.as_tuple()}"
            )

        # ── validation phase (nothing applied yet) ──────────────────────
        for item in items:
            if item.event_kind == STATUS_REJECTED:
                continue
            # every durable event write requires the approved activation —
            # a direct insert WITHOUT one fails closed (B3).
            self._check_activation(item)
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
        new_entries: dict[str, LedgerEntry] = dict(self._entries)
        new_rejects = list(self._rejects)
        new_conflicts = list(self._conflicts)
        seen_conflict_keys = {self._conflict_key(c) for c in self._conflicts}
        new_pending: dict[str, PendingRetry] = dict(self._pending)
        applied: list[tuple[str, LedgerEntry]] = []

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

        # conflicts — idempotent (EXACTLY ONCE per conflict key)
        for payload in conflicts:
            key = self._conflict_key(payload)
            if key in seen_conflict_keys:
                continue
            seen_conflict_keys.add(key)
            new_conflicts.append(dict(payload))

        # pending-retry state — atomic with everything else
        for update in pending:
            if update.retry is None:
                new_pending.pop(update.ledger_identity, None)
            else:
                new_pending[update.ledger_identity] = update.retry

        # crash AFTER the writes were staged but BEFORE commit — the whole
        # transaction rolls back (tests only, via subclass hooks).
        self._before_commit_hook(len(applied))

        self._entries = new_entries
        self._rejects = new_rejects
        self._conflicts = new_conflicts
        self._pending = new_pending
        self._checkpoint = checkpoint

    def _before_commit_hook(self, staged_effects: int) -> None:
        """Overridden by fault-injection harnesses (tests ONLY)."""

    # ── durable persistence model (restart recovery) ────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize the durable ledger state (restart / recovery model)."""
        return {
            "version": self.version,
            "entries": [
                {
                    "ledger_identity": entry.ledger_identity,
                    "source_event_key": entry.source_event_key,
                    "protocol_fingerprint": entry.protocol_fingerprint,
                    "preserved_hash": entry.preserved_hash,
                    "payload": dict(entry.payload),
                    "journal": [dict(row) for row in entry.journal],
                }
                for entry in self.entries()
            ],
            "rejects": [dict(row) for row in self._rejects],
            "conflicts": [dict(row) for row in self._conflicts],
            "pending": [p.to_dict() for p in self.pending_retries()],
            "checkpoint": list(self._checkpoint.as_tuple()),
        }

    @classmethod
    def from_dict(
        cls,
        document: Mapping[str, Any],
        *,
        activation: H1EFreeze | None = None,
    ) -> "InMemoryH1EStore":
        """Rebuild the store from :meth:`to_dict` output (restart model)."""
        store = cls(activation=activation)
        for row in document.get("entries", ()):
            entry = LedgerEntry(
                ledger_identity=str(row["ledger_identity"]),
                source_event_key=str(row["source_event_key"]),
                protocol_fingerprint=str(row["protocol_fingerprint"]),
                preserved_hash=str(row["preserved_hash"]),
                payload=dict(row["payload"]),
                journal=tuple(dict(j) for j in row.get("journal", ())),
            )
            store._entries[entry.ledger_identity] = entry
        store._rejects = [dict(row) for row in document.get("rejects", ())]
        store._conflicts = [dict(row) for row in document.get("conflicts", ())]
        for row in document.get("pending", ()):
            retry = PendingRetry(
                ledger_identity=str(row["ledger_identity"]),
                source_event_key=str(row["source_event_key"]),
                attempt_count=int(row["attempt_count"]),
                deferred_reason=str(row.get("deferred_reason", "")),
                next_attempt_us=int(row.get("next_attempt_us", 0)),
                preserved_hash=str(row.get("preserved_hash", "")),
            )
            store._pending[retry.ledger_identity] = retry
        captured_us, source_event_key = document.get("checkpoint", (0, ""))
        store._checkpoint = CaptureCursor(
            captured_us=int(captured_us),
            source_event_key=str(source_event_key),
        )
        return store


class FaultInjectingStore(InMemoryH1EStore):
    """Offline fault-injection wrapper (tests ONLY).

    ``fail_next_commit`` simulates a crash of the commit transaction: the
    batch, the conflict journal, the pending-retry state and the checkpoint
    are rolled back together, exactly like a real single-transaction
    failure.
    """

    fail_next_commit: bool = False

    def _before_commit_hook(self, staged_effects: int) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise SimulatedCrash("injected commit crash (tests only)")
