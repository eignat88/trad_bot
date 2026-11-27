"""Issue #324 / H1-E — protocol identity, freeze gate and preserved #305 facts.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1e_v1 import protocol as h1e_protocol
from app.research.me_entry_quality_h1e_v1.contracts import (
    REASON_EXCLUDED_WINDOW,
    REASON_MISSING_FINGERPRINT,
    REASON_PRE_FREEZE,
    H1EFreeze,
    admit_source_row,
)
from tests.h1e_v1_fixtures import (
    FIXTURE_FREEZE_US,
    FIXTURE_SYMBOL,
    make_source_row,
    us_to_datetime,
)


# ─── protocol identity ──────────────────────────────────────────────────────


def test_protocol_fingerprint_is_deterministic_sha256():
    fp = h1e_protocol.protocol_fingerprint()
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    assert fp == h1e_protocol.protocol_fingerprint()


def test_protocol_descriptor_is_frozen_text():
    assert h1e_protocol.H1E_PROTOCOL_VERSION == "1.0.0"
    assert "prospective_only=signal_time>h1e_freeze_ts" in (
        h1e_protocol.PROTOCOL_DESCRIPTOR)
    assert "no_backfill" in h1e_protocol.PROTOCOL_DESCRIPTOR
    assert h1e_protocol.IDENTITY_NAMESPACE == "ME_ENTRY_QUALITY_H1E_V1"
    assert h1e_protocol.IDENTITY_NAMESPACE != "ME_UPSTREAM_SOURCE_V1"


# ─── preserved #305 facts ───────────────────────────────────────────────────


def test_source305_freeze_constant_preserved_verbatim():
    assert (
        h1e_protocol.SOURCE305_FREEZE_TS_TEXT
        == "2026-11-24T13:31:53.365952+00:00"
    )
    # exact microsecond epoch of the frozen literal
    # (= 1795527113365 ms from the #305 H1-D evidence, at µs resolution)
    assert h1e_protocol.SOURCE305_FREEZE_TS_US == 1_795_527_113_365_952
    assert (
        h1e_protocol.SOURCE305_UNVERIFIED_WINDOW_TEXT[1]
        == "2026-11-25T07:14:41.230516+00:00"
    )


def test_unverified_window_is_excluded():
    start, end = h1e_protocol.SOURCE305_UNVERIFIED_WINDOW_US
    assert h1e_protocol.excluded_window_reason(start) == "H1E_REJECT_UNVERIFIED_WINDOW"
    assert h1e_protocol.excluded_window_reason(end) == "H1E_REJECT_UNVERIFIED_WINDOW"
    assert h1e_protocol.excluded_window_reason(start - 1) == ""
    assert h1e_protocol.excluded_window_reason(end + 1) == ""


def test_soxsusdt_qa_example_is_excluded():
    qa_us = h1e_protocol._parse_utc_us("2026-11-25T12:00:00+00:00")
    assert (
        h1e_protocol.excluded_window_reason(qa_us, "SOXSUSDT")
        == "H1E_REJECT_SOXSUSDT_QA_EXAMPLE"
    )
    # same symbol on another day is not the QA example
    other = h1e_protocol._parse_utc_us("2026-11-26T12:00:00+00:00")
    assert h1e_protocol.excluded_window_reason(other, "SOXSUSDT") == ""


# ─── H1-E freeze gate ───────────────────────────────────────────────────────


def test_phase_a_never_mints_a_real_freeze():
    assert h1e_protocol.H1E_FREEZE_NOT_MINTED == "H1E_FREEZE_NOT_MINTED"
    with pytest.raises(ValueError):
        H1EFreeze.from_owner_approval(
            freeze_ts_us=1, approval_ref="", approved_by="owner")
    with pytest.raises(ValueError):
        H1EFreeze.from_owner_approval(
            freeze_ts_us=1, approval_ref="PRV-1", approved_by="")


def test_fixture_freeze_is_labelled_synthetic():
    freeze = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
    assert freeze.is_fixture
    assert freeze.fixture_label == "SYNTHETIC_TEST_FIXTURE"
    assert "NOT_AN_APPROVAL" in freeze.approval_ref
    real = H1EFreeze.from_owner_approval(
        freeze_ts_us=FIXTURE_FREEZE_US,
        approval_ref="GO-1",
        approved_by="owner",
    )
    assert not real.is_fixture
    assert real.fixture_label == "REAL_BYBIT_FETCH"


def test_pre_freeze_events_rejected_strictly():
    freeze = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
    at_freeze = make_source_row(signal_time_us=FIXTURE_FREEZE_US)
    assert admit_source_row(at_freeze, freeze=freeze).reason == REASON_PRE_FREEZE
    before = make_source_row(signal_time_us=FIXTURE_FREEZE_US - 1)
    assert admit_source_row(before, freeze=freeze).reason == REASON_PRE_FREEZE
    after = make_source_row(signal_time_us=FIXTURE_FREEZE_US + 1)
    decision = admit_source_row(after, freeze=freeze)
    assert decision.accepted
    assert decision.event.signal_time_us == FIXTURE_FREEZE_US + 1


def test_excluded_window_event_rejected_with_explicit_reason():
    # inside the #305 UNVERIFIED window (pre-freeze too; freeze is later,
    # so use a freeze inside the past to isolate the exclusion check)
    window_us = h1e_protocol.SOURCE305_UNVERIFIED_WINDOW_US
    row = make_source_row(
        signal_time_us=window_us[0] + 1_000_000,
        source_freeze_us=window_us[0] - 1_000_000,
    )
    freeze = H1EFreeze.synthetic_fixture(window_us[0] - 2_000_000)
    decision = admit_source_row(row, freeze=freeze)
    assert not decision.accepted
    assert decision.reason == REASON_EXCLUDED_WINDOW
    assert "H1E_REJECT_UNVERIFIED_WINDOW" in decision.detail


def test_soxsusdt_qa_example_never_admitted():
    qa_us = h1e_protocol._parse_utc_us("2026-11-25T12:00:00+00:00")
    row = make_source_row(
        symbol="SOXSUSDT",
        signal_time_us=qa_us,
        source_freeze_us=h1e_protocol.SOURCE305_UNVERIFIED_WINDOW_US[0] - 1,
    )
    freeze = H1EFreeze.synthetic_fixture(
        h1e_protocol.SOURCE305_UNVERIFIED_WINDOW_US[0] - 2_000_000)
    decision = admit_source_row(row, freeze=freeze)
    assert not decision.accepted
    assert decision.reason == REASON_EXCLUDED_WINDOW
    assert "H1E_REJECT_SOXSUSDT_QA_EXAMPLE" in decision.detail


def test_fingerprint_mismatch_rejected():
    freeze = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
    row = make_source_row()
    decision = admit_source_row(
        row, freeze=freeze, expected_fingerprint="a" * 64)
    assert not decision.accepted
    assert decision.reason == REASON_MISSING_FINGERPRINT
