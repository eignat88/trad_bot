"""Issue #324 / H1-E.4 — append-only ledger semantics: unique identity,
conflicting-payload isolation, atomic checkpoint, derived status, and the
DO-NOT-APPLY DDL proposal contract.

Offline: zero network, zero production DB, NO DDL execution.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureCursor,
    LedgerWrite,
)
from app.research.me_entry_quality_h1e_v1.contracts import ledger_identity
from app.research.me_entry_quality_h1e_v1.ledger import (
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
    CheckpointRegression,
    InMemoryH1EStore,
    LedgerIdentityConflict,
)

DDL_PATH = (
    Path(__file__).resolve().parent.parent
    / "sql" / "proposals" / "324_h1e_prospective_ledger_v1_proposal.sql"
)


def _write(key: str, *, kind: str = STATUS_CLASSIFIED, preserved_hash: str = "h1",
           classification: dict | None = None, captured_us: int = 10) -> LedgerWrite:
    return LedgerWrite(
        ledger_identity=ledger_identity(key),
        source_event_key=key,
        protocol_fingerprint="fp",
        event_kind=kind,
        preserved_hash=preserved_hash,
        payload={"source_event_key": key, "lineage_hash": preserved_hash},
        classification=classification if classification is not None
        else {"eligible": True},
        attempt_count=1,
        captured_at_us=captured_us,
    )


# ─── identity, idempotency, conflicts ───────────────────────────────────────


def test_unique_identity_per_source_event_key_and_fingerprint():
    store = InMemoryH1EStore()
    store.commit_batch([_write("k1")], CaptureCursor(10, "k1"))
    # identical redelivery — idempotent no-op
    store.commit_batch([_write("k1")], CaptureCursor(10, "k1"))
    assert len(store.entries()) == 1
    entry = store.lookup(ledger_identity("k1"))
    assert entry.classifications == ({"eligible": True},)
    assert entry.current_status == STATUS_CLASSIFIED


def test_conflicting_payload_is_isolated_never_overwrites():
    store = InMemoryH1EStore()
    store.commit_batch([_write("k1", preserved_hash="aaa")], CaptureCursor(10, "k1"))
    with pytest.raises(LedgerIdentityConflict):
        store.commit_batch(
            [_write("k1", preserved_hash="bbb", classification={"eligible": False})],
            CaptureCursor(11, "k1"),
        )
    # NOTHING of the conflicting batch applied — original evidence intact
    entry = store.lookup(ledger_identity("k1"))
    assert entry.preserved_hash == "aaa"
    assert entry.classifications == ({"eligible": True},)
    assert store.checkpoint == CaptureCursor(10, "k1")


def test_checkpoint_is_atomic_with_the_ledger_insert():
    store = InMemoryH1EStore()
    items = [_write("k1"), _write("k2", captured_us=20)]
    store.commit_batch(items, CaptureCursor(20, "k2"))
    assert store.checkpoint == CaptureCursor(20, "k2")
    assert len(store.entries()) == 2
    # a batch that fails validation applies NEITHER entries NOR checkpoint
    with pytest.raises(LedgerIdentityConflict):
        store.commit_batch(
            [_write("k2", preserved_hash="zzz"), _write("k3", captured_us=30)],
            CaptureCursor(30, "k3"),
        )
    assert len(store.entries()) == 2
    assert store.checkpoint == CaptureCursor(20, "k2")


def test_checkpoint_regression_is_rejected():
    store = InMemoryH1EStore()
    store.commit_batch([_write("k1")], CaptureCursor(10, "k1"))
    with pytest.raises(CheckpointRegression):
        store.commit_batch([], CaptureCursor(9, "zz"))
    # same captured_us but smaller key is also a regression
    with pytest.raises(CheckpointRegression):
        store.commit_batch([], CaptureCursor(10, "a"))


def test_status_transitions_are_append_only_journal_derived():
    store = InMemoryH1EStore()
    store.commit_batch(
        [_write("k1", kind=STATUS_DEFERRED,
                classification={"status": "DEFERRED"})],
        CaptureCursor(10, "k1"))
    assert store.current_status(ledger_identity("k1")) == STATUS_DEFERRED
    store.commit_batch(
        [_write("k1", kind=STATUS_CLASSIFIED,
                classification={"status": "CLASSIFIED"})],
        CaptureCursor(11, "k1"))
    entry = store.lookup(ledger_identity("k1"))
    # immutable evidence preserved; status DERIVED from the journal
    assert entry.current_status == STATUS_CLASSIFIED
    assert len(entry.journal) == 2
    assert entry.classifications == (
        {"status": "DEFERRED"}, {"status": "CLASSIFIED"})
    assert [e["event_kind"] for e in store.status_journal(ledger_identity("k1"))] \
        == [STATUS_DEFERRED, STATUS_CLASSIFIED]


# ─── DDL proposal contract (PROPOSAL ONLY — never applied) ─────────────────


@pytest.fixture(scope="module")
def ddl_text() -> str:
    assert DDL_PATH.is_file(), f"DDL proposal missing: {DDL_PATH}"
    return DDL_PATH.read_text(encoding="utf-8")


def test_ddl_proposal_is_marked_do_not_apply(ddl_text):
    assert "*** PROPOSAL ONLY — DO NOT APPLY ***" in ddl_text
    assert "NO_DDL_APPLY" in ddl_text
    assert "NEVER executed by any" in ddl_text


def test_ddl_proposal_is_additive_only_and_isolated(ddl_text):
    statements = re.findall(
        r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE)\b",
        ddl_text, flags=re.MULTILINE | re.IGNORECASE)
    kinds = {s.upper() for s in statements}
    assert kinds <= {"CREATE"}, f"non-additive DDL found: {kinds}"
    # nothing outside the dedicated H1-E schema is touched
    referenced = set(re.findall(
        r"\b(?:TABLE|SCHEMA|ON|FROM|INTO|REFERENCES)\s+([a-z_]+\.)", ddl_text))
    assert referenced <= {"research_me_entry_quality_h1e."}
    stripped = (
        ddl_text
        .replace("research_me_upstream_coverage.*", "")
        .replace("dds.*", "")
        .replace("market.*", "")
    )
    for forbidden in (
        "research_me_upstream_coverage.",
        "dds.",
        "market.",
        "ALTER TABLE",
        "DROP TABLE",
    ):
        assert forbidden not in stripped


def test_ddl_proposal_tables_match_the_ledger_contract(ddl_text):
    for table in (
        "h1e_activation",
        "h1e_event",
        "h1e_classification",
        "h1e_status_event",
        "h1e_reject_event",
        "h1e_conflict_event",
        "h1e_provenance_snapshot",
        "h1e_checkpoint",
        "h1e_coverage_audit",
    ):
        assert f"research_me_entry_quality_h1e.{table}" in ddl_text
    assert "uq_h1e_event_identity" in ddl_text
    assert "(source_event_key, h1e_protocol_fingerprint)" in ddl_text
    assert "H1E_LEDGER_IMMUTABLE" in ddl_text
    assert "H1E_CHECKPOINT_REGRESSION" in ddl_text
    assert "H1E_IMMUTABLE_FREEZE_TS" in ddl_text
