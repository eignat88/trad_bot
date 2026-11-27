"""Issue #324 / H1-E — review fix B3: the H1-E prospective freeze is
enforced as a SEPARATE activation, independently of the #305 source freeze.

Covers the acceptance matrix: two different freezes (#305 vs H1-E),
``signal_time == h1e_freeze_ts`` rejected, ``signal_time > h1e_freeze_ts``
admitted with a valid activation, missing approval / fingerprint mismatch
fail-closed, a frozen freeze can never be changed, and concurrent /
repeated writes stay idempotent and integral.

Offline: zero network, zero production DB, NO DDL execution.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CaptureCursor,
    LedgerWrite,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    REASON_PRE_FREEZE,
    H1EFreeze,
    ledger_identity,
)
from app.research.me_entry_quality_h1e_v1.ledger import (
    ActivationFingerprintMismatch,
    FreezeTsMismatch,
    InMemoryH1EStore,
    NoApprovedActivation,
    PreFreezeWrite,
)
from tests.h1e_v1_fixtures import (
    FIXTURE_FREEZE_US,
    FakeReader,
    make_classification_record,
    make_source_row,
)

FREEZE_A = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
FREEZE_B = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US + 1_000_000_000)
CFG = CaptureConfig(allow_fixture_freeze=True)

#: the #305 source freeze (independent of every H1-E freeze)
SOURCE305_FREEZE_US = 1_795_527_113_365_952


def _classifier(event, prior_deferrals=0):
    return make_classification_record(event)


def _write_for(freeze: H1EFreeze, *, key: str = "k1",
               signal_time_us: int | None = None,
               fingerprint: str | None = None,
               h1e_freeze_ts_us: int | None = None) -> LedgerWrite:
    signal = int(signal_time_us if signal_time_us is not None
                 else freeze.freeze_ts_us + 10)
    return LedgerWrite(
        ledger_identity=ledger_identity(key, freeze.protocol_fingerprint),
        source_event_key=key,
        protocol_fingerprint=(
            freeze.protocol_fingerprint if fingerprint is None else fingerprint),
        event_kind="CLASSIFIED",
        preserved_hash="h1",
        payload={"source_event_key": key, "signal_time_us": signal},
        classification={"eligible": True},
        attempt_count=1,
        captured_at_us=signal + 1,
        activation_id="ME_ENTRY_QUALITY_H1E_PROSPECTIVE_OBSERVER_V1",
        h1e_freeze_ts_us=(
            freeze.freeze_ts_us if h1e_freeze_ts_us is None
            else int(h1e_freeze_ts_us)),
    )


# ─── two different freezes (#305 vs H1-E) ──────────────────────────────────


def test_event_between_source305_and_h1e_freeze_is_rejected():
    """After the #305 freeze but BEFORE the H1-E freeze → explicit reject."""
    row = make_source_row(
        signal_time_us=FREEZE_A.freeze_ts_us,  # == H1-E freeze
        captured_us=FREEZE_A.freeze_ts_us + 5_000_000,
        candle_open_ms=1_700_000_000_000,
        source_freeze_us=SOURCE305_FREEZE_US,
    )
    assert SOURCE305_FREEZE_US < FREEZE_A.freeze_ts_us  # two distinct freezes
    store = InMemoryH1EStore(activation=FREEZE_A)
    report = run_capture_cycle(
        reader=FakeReader([row]), store=store, classifier=_classifier,
        freeze=FREEZE_A, cursor=CaptureCursor(), config=CFG)
    assert report.committed
    assert [r.reason for r in report.rejections] == [REASON_PRE_FREEZE]
    assert store.entries() == ()

    # a row strictly BETWEEN the two freezes is also refused
    between = make_source_row(
        signal_time_us=SOURCE305_FREEZE_US + 1_000_000,
        captured_us=FREEZE_A.freeze_ts_us + 5_000_000,
        candle_open_ms=1_700_000_000_000,
        source_freeze_us=SOURCE305_FREEZE_US,
    )
    report2 = run_capture_cycle(
        reader=FakeReader([between]), store=store, classifier=_classifier,
        freeze=FREEZE_A, cursor=CaptureCursor(), config=CFG)
    assert [r.reason for r in report2.rejections] == [REASON_PRE_FREEZE]


def test_store_checks_against_the_bound_h1e_freeze_not_the_source_freeze():
    """The DB-level guard is mirrored: a write valid under freeze A but
    evaluated against the LATER freeze B is refused."""
    store_b = InMemoryH1EStore(activation=FREEZE_B)
    # event after freeze A but at/before freeze B
    write = _write_for(FREEZE_B, signal_time_us=FREEZE_A.freeze_ts_us + 100)
    with pytest.raises(PreFreezeWrite, match="signal_time_us"):
        store_b.commit_batch([write], CaptureCursor(1, "k1"))
    # the same event IS admitted by the store bound to freeze A
    store_a = InMemoryH1EStore(activation=FREEZE_A)
    write_a = _write_for(FREEZE_A, signal_time_us=FREEZE_A.freeze_ts_us + 100)
    store_a.commit_batch([write_a], CaptureCursor(1, "k1"))
    assert len(store_a.entries()) == 1


def test_write_carrying_a_different_freeze_instant_is_refused():
    store = InMemoryH1EStore(activation=FREEZE_B)
    write = _write_for(FREEZE_B, h1e_freeze_ts_us=FREEZE_A.freeze_ts_us)
    with pytest.raises(FreezeTsMismatch, match="H1E|h1e_freeze"):
        store.commit_batch([write], CaptureCursor(1, "k1"))
    assert store.entries() == ()


# ─── equality is refused; strict-after is admitted ─────────────────────────


def test_signal_time_equal_to_h1e_freeze_is_rejected():
    store = InMemoryH1EStore(activation=FREEZE_A)
    write = _write_for(FREEZE_A, signal_time_us=FREEZE_A.freeze_ts_us)
    with pytest.raises(PreFreezeWrite):
        store.commit_batch([write], CaptureCursor(1, "k1"))
    assert store.entries() == ()


def test_signal_time_strictly_after_h1e_freeze_is_admitted():
    store = InMemoryH1EStore(activation=FREEZE_A)
    write = _write_for(FREEZE_A, signal_time_us=FREEZE_A.freeze_ts_us + 1)
    store.commit_batch([write], CaptureCursor(1, "k1"))
    assert len(store.entries()) == 1


# ─── fail-closed: approval / fingerprint / freeze integrity ────────────────


def test_missing_approval_is_fail_closed():
    with pytest.raises(ValueError, match="H1E_REJECT_FREEZE_NOT_APPROVED"):
        H1EFreeze.from_owner_approval(
            freeze_ts_us=FREEZE_A.freeze_ts_us,
            approval_ref="", approved_by="owner")
    with pytest.raises(ValueError, match="H1E_REJECT_FREEZE_NOT_APPROVED"):
        H1EFreeze.from_owner_approval(
            freeze_ts_us=FREEZE_A.freeze_ts_us,
            approval_ref="   ", approved_by="owner")
    with pytest.raises(ValueError, match="H1E_REJECT_FREEZE_NOT_APPROVED"):
        H1EFreeze.from_owner_approval(
            freeze_ts_us=FREEZE_A.freeze_ts_us,
            approval_ref="PR#325", approved_by="  ")


def test_fingerprint_mismatch_is_fail_closed():
    store = InMemoryH1EStore(activation=FREEZE_A)
    write = _write_for(FREEZE_A, fingerprint="OTHER_FINGERPRINT")
    with pytest.raises(ActivationFingerprintMismatch, match="fingerprint"):
        store.commit_batch([write], CaptureCursor(1, "k1"))
    assert store.entries() == ()


def test_store_without_activation_is_fail_closed():
    store = InMemoryH1EStore()  # no approved activation bound
    write = _write_for(FREEZE_A)
    with pytest.raises(NoApprovedActivation, match="activation"):
        store.commit_batch([write], CaptureCursor(1, "k1"))
    assert store.entries() == ()


def test_frozen_h1e_freeze_cannot_be_changed():
    freeze = H1EFreeze.from_owner_approval(
        freeze_ts_us=FREEZE_A.freeze_ts_us,
        approval_ref="PR#325-GO-2027-01-01",
        approved_by="owner")
    for field_name, value in (
        ("freeze_ts_us", 1),
        ("approval_ref", "MUTATED"),
        ("approved_by", "MUTATED"),
        ("protocol_fingerprint", "MUTATED"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(freeze, field_name, value)


# ─── concurrency / repeated commit ─────────────────────────────────────────


def test_concurrent_records_and_repeated_commits_stay_idempotent():
    """Interleaved writes for several identities + exact re-commits keep
    every identity integral and exactly-once."""
    store = InMemoryH1EStore(activation=FREEZE_A)
    keys = [f"k{i}" for i in range(5)]
    writes = [
        _write_for(FREEZE_A, key=key, signal_time_us=FREEZE_A.freeze_ts_us + i + 1)
        for i, key in enumerate(keys)
    ]
    # commit the same "concurrent" batches repeatedly (at-least-once)
    for _ in range(3):
        store.commit_batch(writes, CaptureCursor(5, keys[-1]))
    assert len(store.entries()) == 5
    for key in keys:
        entry = store.lookup(ledger_identity(key, FREEZE_A.protocol_fingerprint))
        assert len(entry.journal) == 1  # exactly-once
        assert entry.payload["signal_time_us"] > FREEZE_A.freeze_ts_us

    # a later interleaved batch adds only NEW identities
    late = _write_for(
        FREEZE_A, key="late", signal_time_us=FREEZE_A.freeze_ts_us + 99)
    store.commit_batch([late], CaptureCursor(6, "late"))
    assert len(store.entries()) == 6
