"""Issue #324 / H1-E — provenance snapshots: H1D_HTF_SNAPSHOT_2 retention,
tamper detection fail-closed, offline byte-identical replay.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import json

import pytest

from app.research.me_entry_quality_h1d_v1.provenance import (
    SNAPSHOT_SCHEMA,
    ProvenanceTamperError,
    load_snapshot,
    save_snapshot,
    snapshot_from_dict,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    classify_h1e_event,
    replay_canonical_bytes,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    admit_source_row,
)
from tests.h1e_v1_fixtures import (
    DAY_MS,
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    H4_MS,
    StubTransport,
    make_source_row,
    timeframe_payload,
    zigzag_rows,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)


def _outcome():
    decision = admit_source_row(make_source_row(), freeze=FREEZE)
    assert decision.accepted
    rows4 = zigzag_rows("BEARISH", timeframe_ms=H4_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows1 = zigzag_rows("NEUTRAL", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    transport = StubTransport(default={
        "240": timeframe_payload(rows4, interval="240"),
        "D": timeframe_payload(rows1, interval="D"),
    })
    return classify_h1e_event(decision.event, transport)


def test_snapshot_schema_is_h1d_htf_snapshot_2():
    assert SNAPSHOT_SCHEMA == "H1D_HTF_SNAPSHOT_2"
    outcome = _outcome()
    snapshot = outcome.snapshot
    assert snapshot is not None
    assert snapshot.schema == SNAPSHOT_SCHEMA
    assert snapshot.fixture_label == "SYNTHETIC_TEST_FIXTURE"
    # the ledger record retains the canonical snapshot hash
    assert outcome.record.snapshot_sha256 == snapshot.snapshot_sha256


def test_snapshot_load_rejects_tampered_row_value():
    outcome = _outcome()
    document = outcome.snapshot.to_dict()
    document["payload_4h"]["rows"][10]["close"] += 1.0  # tamper one value
    with pytest.raises(ProvenanceTamperError):
        snapshot_from_dict(document)


def test_snapshot_load_rejects_tampered_lineage():
    outcome = _outcome()
    document = outcome.snapshot.to_dict()
    document["lineage_hash"] = "0" * 64  # tamper the preserved lineage
    with pytest.raises(ProvenanceTamperError):
        snapshot_from_dict(document)


def test_snapshot_load_rejects_tampered_asof():
    outcome = _outcome()
    document = outcome.snapshot.to_dict()
    document["asof_us"] += 1  # tamper the causal ASOF
    with pytest.raises(ProvenanceTamperError):
        snapshot_from_dict(document)


def test_snapshot_load_rejects_tampered_row_count():
    outcome = _outcome()
    document = outcome.snapshot.to_dict()
    document["payload_1d"]["row_count"] += 1  # tamper the row count
    with pytest.raises(ProvenanceTamperError):
        snapshot_from_dict(document)


def test_snapshot_load_rejects_schema_downgrade():
    outcome = _outcome()
    document = outcome.snapshot.to_dict()
    document["schema"] = "H1D_HTF_SNAPSHOT_1"
    with pytest.raises(ProvenanceTamperError):
        snapshot_from_dict(document)


def test_offline_replay_is_byte_identical_across_load_cycles(tmp_path):
    outcome = _outcome()
    path = tmp_path / "snapshot.json"
    save_snapshot(outcome.snapshot, path)
    loaded = load_snapshot(path)
    assert loaded.snapshot_sha256 == outcome.snapshot.snapshot_sha256
    assert replay_canonical_bytes(loaded) == replay_canonical_bytes(outcome.snapshot)
    # deterministic across a serialized round trip
    reloaded = snapshot_from_dict(json.loads(json.dumps(loaded.to_dict())))
    assert replay_canonical_bytes(reloaded) == replay_canonical_bytes(loaded)


def test_classification_replay_matches_the_direct_run():
    outcome = _outcome()
    replay_bytes = replay_canonical_bytes(outcome.snapshot)
    replayed = json.loads(replay_bytes)
    assert replayed["eligible"] is True
    assert replayed["source_event_key"] == outcome.record.source_event_key
    assert replayed["regime_4h"] == outcome.record.regime_4h
    assert replayed["regime_1d"] == outcome.record.regime_1d
