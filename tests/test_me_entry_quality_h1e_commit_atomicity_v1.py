"""Issue #324 / H1-E — review fix B2: conflict journal is part of the ONE
atomic batch transaction.

A detected identity conflict is never persisted outside ``commit_batch``:
on an interrupted transaction NOTHING is applied (no partial conflict
record, checkpoint unchanged), the original immutable event record is
preserved, and one conflict is recorded EXACTLY ONCE across redeliveries.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CaptureCursor,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    ledger_identity,
)
from app.research.me_entry_quality_h1e_v1.ledger import (
    FaultInjectingStore,
    InMemoryH1EStore,
    SimulatedCrash,
)
from tests.h1e_v1_fixtures import (
    FIXTURE_FREEZE_US,
    FakeReader,
    make_classification_record,
    make_source_row,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
CFG = CaptureConfig(allow_fixture_freeze=True)


def _classifier(event, prior_deferrals=0):
    return make_classification_record(event)


def _conflicting_rows():
    row_a = make_source_row(
        signal_time_us=FIXTURE_FREEZE_US + 10_000_000,
        captured_us=FIXTURE_FREEZE_US + 100_000_000,
        candle_open_ms=1_700_000_000_000,
    )
    # same key/identity, DIFFERENT preserved facts (mutated lineage)
    row_b = make_source_row(
        signal_time_us=FIXTURE_FREEZE_US + 10_000_000,
        captured_us=FIXTURE_FREEZE_US + 100_000_000,
        candle_open_ms=1_700_000_000_000,
        lineage_overrides={"source_scanner_version": "MUTATED"},
    )
    assert row_a["source_event_key"] == row_b["source_event_key"]
    assert row_a["lineage_hash"] != row_b["lineage_hash"]
    return row_a, row_b


def test_conflict_plus_crash_applies_no_partial_records():
    row_a, row_b = _conflicting_rows()
    identity = ledger_identity(row_a["source_event_key"])
    store = FaultInjectingStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader([row_a]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    checkpoint_before = store.checkpoint

    # the batch that would record the conflict crashes before commit
    store.fail_next_commit = True
    crashed = run_capture_cycle(
        reader=FakeReader([row_b]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert not crashed.committed
    assert len(crashed.conflicts) == 1  # detected in-memory…
    # …but NOTHING reached the durable journals (all-or-nothing)
    assert store.conflict_events == ()
    assert store.entries()[0].preserved_hash != ""
    entry = store.lookup(identity)
    assert entry.payload["lineage_hash"] == row_a["lineage_hash"]
    assert len(entry.journal) == 1
    assert store.checkpoint == checkpoint_before

    # restart + redelivery: the conflict is recorded EXACTLY ONCE
    restarted = run_capture_cycle(
        reader=FakeReader([row_b]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    assert restarted.committed
    assert len(store.conflict_events) == 1
    assert store.checkpoint == restarted.cursor_after


def test_conflict_is_recorded_exactly_once_across_redeliveries():
    row_a, row_b = _conflicting_rows()
    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader([row_a]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    for _ in range(3):
        report = run_capture_cycle(
            reader=FakeReader([row_b]), store=store, classifier=_classifier,
            freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
        assert report.committed
        assert len(report.conflicts) == 1
    # the durable conflict journal holds ONE record — idempotent writes
    assert len(store.conflict_events) == 1


def test_original_record_is_never_modified_by_a_conflict():
    row_a, row_b = _conflicting_rows()
    identity = ledger_identity(row_a["source_event_key"])
    store = InMemoryH1EStore(activation=FREEZE)
    run_capture_cycle(
        reader=FakeReader([row_a]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    original = store.lookup(identity)
    run_capture_cycle(
        reader=FakeReader([row_b]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    after = store.lookup(identity)
    assert after == original  # byte-identical immutable evidence
    conflict = store.conflict_events[0]
    assert conflict["existing_preserved_hash"] == original.preserved_hash
    assert conflict["delivered_preserved_hash"] != original.preserved_hash
    assert conflict["delivered_payload"]["lineage_hash"] == row_b["lineage_hash"]


def test_partial_batch_is_impossible_on_failure():
    """Writes + rejects + conflicts + pending + checkpoint roll back TOGETHER."""
    from app.research.me_entry_quality_h1e_v1.capture import (
        LedgerWrite,
        PendingRetry,
        PendingRetryUpdate,
    )

    store = FaultInjectingStore(activation=FREEZE)
    row_a, row_b = _conflicting_rows()
    identity = ledger_identity(row_a["source_event_key"])
    run_capture_cycle(
        reader=FakeReader([row_a]), store=store, classifier=_classifier,
        freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
    snapshot_before = store.to_dict()

    event_key = row_a["source_event_key"]
    item = LedgerWrite(
        ledger_identity="reject:x",
        source_event_key=event_key,
        protocol_fingerprint=FREEZE.protocol_fingerprint,
        event_kind="REJECTED",
        preserved_hash="",
        payload={"reason": "H1E_TEST", "detail": "partial-batch guard"},
        classification=None,
        attempt_count=1,
        captured_at_us=1,
    )
    conflict = {
        "source_event_key": event_key,
        "ledger_identity": identity,
        "existing_preserved_hash": "aaa",
        "delivered_preserved_hash": "bbb",
        "delivered_payload": {},
    }
    pending = PendingRetryUpdate(identity, PendingRetry(
        ledger_identity=identity, source_event_key=event_key,
        attempt_count=1, deferred_reason="H1E_TEST",
        next_attempt_us=0, preserved_hash=store.lookup(identity).preserved_hash))

    store.fail_next_commit = True
    with pytest.raises(SimulatedCrash, match="injected commit crash"):
        store.commit_batch(
            [item], CaptureCursor(FIXTURE_FREEZE_US + 200_000_000, "zz"),
            conflicts=[conflict], pending=[pending])
    # NOTHING partial: rejects, conflicts, pending and checkpoint unchanged
    assert store.to_dict() == snapshot_before

    # the same batch retried applies everything exactly once
    store.commit_batch(
        [item], CaptureCursor(FIXTURE_FREEZE_US + 200_000_000, "zz"),
        conflicts=[conflict], pending=[pending])
    assert len(store.reject_events) == 1
    assert len(store.conflict_events) == 1
    assert [p.ledger_identity for p in store.pending_retries()] == [identity]
    # repeated commit of the same conflict/pending is idempotent
    store.commit_batch(
        [], CaptureCursor(FIXTURE_FREEZE_US + 200_000_000, "zz"),
        conflicts=[conflict], pending=[pending])
    assert len(store.conflict_events) == 1
    assert store.to_dict()["conflicts"] == snapshot_before["conflicts"] + [conflict]


def test_repeated_commit_of_identical_writes_is_idempotent():
    store = InMemoryH1EStore(activation=FREEZE)
    row_a, _ = _conflicting_rows()
    for _ in range(3):
        report = run_capture_cycle(
            reader=FakeReader([row_a]), store=store, classifier=_classifier,
            freeze=FREEZE, cursor=CaptureCursor(), config=CFG)
        assert report.committed
    entry = store.lookup(ledger_identity(row_a["source_event_key"]))
    assert len(entry.journal) == 1  # exactly-once effect
    assert len(store.entries()) == 1
