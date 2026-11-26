"""Issue #314 / H1-D — immutable provenance snapshot + offline replay tests
(``H1D_PROVENANCE_REPLAY_V1``).

Covers: snapshot field capture (source_event_key / source_identity via
``build_source_identity()`` / lineage hash / ASOF / timeframe / source
label / fetched_at_ms / row count / canonical SHA-256 / raw rows),
deterministic canonical serialization, save/load round-trip, TAMPER
DETECTION (rows, lineage, hashes, counts), and deterministic replay with
byte-identical output and ZERO HTTP (the transport must never be touched).

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.exchange.bybit_client import BybitClient
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
    build_source_identity,
)
from app.research.me_entry_quality_h1d_v1.provenance import (
    FIXTURE_LABEL_SYNTHETIC,
    H1D_PROVENANCE_VERSION,
    ProvenanceError,
    ProvenanceTamperError,
    SNAPSHOT_SCHEMA,
    Snapshot,
    build_snapshot,
    canonical_json_bytes,
    canonical_output_bytes,
    canonical_rows_bytes,
    compute_snapshot_hash,
    load_snapshot,
    payload_sha256,
    replay_classification,
    save_snapshot,
    snapshot_from_dict,
)

# ─── canonical fixture: the #305 SOXSUSDT event (synthetic payload rows) ────

EVENT_ROW = {
    "source_event_key":
        "d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829",
    "source_identity":
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000",
    "symbol": "SOXSUSDT",
    "lineage_hash": hashlib.sha256(
        b"SYNTHETIC_TEST_FIXTURE_LINEAGE").hexdigest(),
    "signal_time": "2026-11-25T13:11:00.804844+00:00",
    "captured_at": "2026-11-25T13:11:01.500000+00:00",
    "activation_freeze_ts": "2026-11-24T13:31:53.365952+00:00",
}

TF = 14_400_000
DAY = 86_400_000


def _row(open_ms, *, o=100.0, h=101.0, low=99.0, c=100.5, v=1.0):
    return {"open_ms": open_ms, "open": float(o), "high": float(h),
            "low": float(low), "close": float(c), "volume": float(v)}


def _path(i):
    """Deterministic upward zigzag (triangle wave + drift)."""
    phase = i % 8
    tri = phase if phase <= 4 else 8 - phase
    return 100.0 + 0.3 * i + 5.0 * tri


def _zigzag_rows(open0, n, tf):
    rows = []
    for i in range(n):
        o, c = _path(i - 1), _path(i)
        rows.append({
            "open_ms": open0 + i * tf,
            "open": o, "close": c,
            "high": max(o, c) + 1.0, "low": min(o, c) - 1.0,
            "volume": 1.0,
        })
    return rows


def _event_record():
    batch = adapt_source_events([EVENT_ROW])
    assert not batch.rejections
    return batch.records[0]


def _fixture_payloads(n_4h=60, n_1d=60):
    record = _event_record()
    asof_us = record.signal_time_us
    latest_4h = ((asof_us // 1000) - TF) // TF * TF
    latest_1d = ((asof_us // 1000) - DAY) // DAY * DAY
    rows_4h = _zigzag_rows(latest_4h - (n_4h - 1) * TF, n_4h, TF)
    rows_1d = _zigzag_rows(latest_1d - (n_1d - 1) * DAY, n_1d, DAY)
    return record, rows_4h, rows_1d


def _snapshot(n_4h=60, n_1d=60):
    record, rows_4h, rows_1d = _fixture_payloads(n_4h, n_1d)
    return build_snapshot(
        record=record,
        rows_4h=rows_4h,
        rows_1d=rows_1d,
        source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
        source_label="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf",
        fixture_label=FIXTURE_LABEL_SYNTHETIC,
        fetched_at_ms=1795612260804,
    )


# ─── snapshot construction ─────────────────────────────────────────────────


class TestSnapshotBuild:
    def test_captures_full_provenance(self):
        snapshot = _snapshot()
        record = _event_record()
        assert snapshot.schema == SNAPSHOT_SCHEMA
        assert snapshot.provenance_version == H1D_PROVENANCE_VERSION
        assert snapshot.fixture_label == FIXTURE_LABEL_SYNTHETIC
        assert snapshot.source_event_key == record.source_event_key
        assert snapshot.source_identity == record.source_identity
        assert snapshot.lineage_hash == record.lineage_hash
        assert snapshot.signal_time_us == 1795612260804844  # µs preserved
        assert snapshot.signal_time_ms == 1795612260804      # floor policy
        assert snapshot.asof_us == snapshot.signal_time_us
        assert snapshot.symbol == "SOXSUSDT"
        assert snapshot.payload_4h.timeframe == "4H"
        assert snapshot.payload_4h.interval == "240"
        assert snapshot.payload_1d.timeframe == "1D"
        assert snapshot.payload_1d.interval == "D"
        for payload in (snapshot.payload_4h, snapshot.payload_1d):
            assert payload.source_label == "SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf"
            assert payload.fetched_at_ms == 1795612260804
            assert payload.row_count == 60 == len(payload.rows)
            assert len(payload.payload_sha256) == 64

    def test_source_identity_built_via_h1c_builder(self):
        snapshot = _snapshot()
        record, rows_4h, _ = _fixture_payloads()
        expected = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT,
            "SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf",
            rows_4h, fetched_at_ms=1795612260804).to_dict()
        assert snapshot.payload_4h.source_identity == expected

    def test_lineage_payload_is_verbatim(self):
        snapshot = _snapshot()
        record = _event_record()
        assert snapshot.lineage_payload() == record.lineage_payload()

    def test_raw_rows_stored(self):
        _, rows_4h, rows_1d = _fixture_payloads()
        snapshot = _snapshot()
        assert [dict(row) for row in snapshot.payload_4h.rows] == rows_4h
        assert [dict(row) for row in snapshot.payload_1d.rows] == rows_1d

    def test_fixture_label_must_be_honest(self):
        record, rows_4h, rows_1d = _fixture_payloads()
        with pytest.raises(ValueError):
            build_snapshot(
                record=record, rows_4h=rows_4h, rows_1d=rows_1d,
                source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
                source_label="x", fixture_label="REAL_COVERALPHA")


# ─── canonical hashing ─────────────────────────────────────────────────────


class TestCanonicalHash:
    def test_payload_hash_is_deterministic(self):
        _, rows_4h, _ = _fixture_payloads()
        assert payload_sha256(rows_4h) == payload_sha256(list(rows_4h))
        assert payload_sha256(rows_4h) == hashlib.sha256(
            canonical_rows_bytes(rows_4h)).hexdigest()

    def test_payload_hash_changes_with_content(self):
        _, rows_4h, _ = _fixture_payloads()
        tampered = [dict(row) for row in rows_4h]
        tampered[10]["close"] = 999.0
        assert payload_sha256(tampered) != payload_sha256(rows_4h)

    def test_snapshot_hash_covers_everything_but_itself(self):
        snapshot = _snapshot()
        document = snapshot.to_dict()
        assert compute_snapshot_hash(document) == snapshot.snapshot_sha256
        body = {k: v for k, v in document.items() if k != "snapshot_sha256"}
        assert compute_snapshot_hash(body) == snapshot.snapshot_sha256

    def test_canonical_json_is_sorted_and_stable(self):
        assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
        assert canonical_json_bytes({"b": 1, "a": 2}) == canonical_json_bytes(
            {"a": 2, "b": 1})


# ─── save / load round-trip ────────────────────────────────────────────────


class TestSaveLoad:
    def test_round_trip_is_lossless(self, tmp_path):
        snapshot = _snapshot()
        path = tmp_path / "snap.json"
        save_snapshot(snapshot, path)
        loaded = load_snapshot(path)
        assert loaded == snapshot
        # re-saving the loaded snapshot reproduces identical bytes
        path2 = tmp_path / "snap2.json"
        save_snapshot(loaded, path2)
        assert path.read_bytes() == path2.read_bytes()

    def test_load_rejects_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_bytes(b"{not json")
        with pytest.raises(ProvenanceError):
            load_snapshot(path)


# ─── immutable lineage: tamper detection ───────────────────────────────────


class TestTamperDetection:
    def _document(self):
        return json.loads(json.dumps(_snapshot().to_dict()))

    def test_row_value_tamper_detected(self):
        document = self._document()
        document["payload_4h"]["rows"][10]["close"] = 12345.0
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_row_deleted_detected(self):
        document = self._document()
        del document["payload_1d"]["rows"][3]
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_lineage_tamper_detected(self):
        document = self._document()
        document["lineage_hash"] = "0" * 64
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_source_event_key_tamper_detected(self):
        document = self._document()
        document["source_event_key"] = "f" * 64
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_asof_tamper_detected(self):
        document = self._document()
        document["signal_time_us"] += 1
        document["asof_us"] += 1
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_snapshot_hash_field_tamper_detected(self):
        document = self._document()
        document["snapshot_sha256"] = "0" * 64
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_row_count_tamper_detected(self):
        document = self._document()
        document["payload_4h"]["row_count"] = 61
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_source_identity_tamper_detected(self):
        document = self._document()
        document["payload_4h"]["source_identity"]["payload_hash"] = "0" * 64
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_fixture_label_tamper_detected(self):
        document = self._document()
        document["fixture_label"] = "REAL_BYBIT_FETCH"
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)

    def test_payload_hash_field_tamper_detected(self):
        document = self._document()
        document["payload_1d"]["payload_sha256"] = "0" * 64
        with pytest.raises(ProvenanceTamperError):
            snapshot_from_dict(document)


# ─── deterministic replay with ZERO HTTP ───────────────────────────────────


class TestReplay:
    def test_replay_is_byte_identical(self):
        snapshot = _snapshot()
        first = canonical_output_bytes(replay_classification(snapshot))
        second = canonical_output_bytes(replay_classification(snapshot))
        assert first == second

    def test_replay_never_touches_the_transport(self, monkeypatch):
        calls = []

        def _boom(*args, **kwargs):
            calls.append(1)
            raise AssertionError("HTTP transport touched during replay")

        monkeypatch.setattr(BybitClient, "_public_get", _boom)
        snapshot = _snapshot()
        out_bytes = canonical_output_bytes(replay_classification(snapshot))
        assert calls == []
        assert len(out_bytes) > 0

    def test_replay_from_disk_matches_in_memory(self, tmp_path):
        snapshot = _snapshot()
        path = tmp_path / "snap.json"
        save_snapshot(snapshot, path)
        from_disk = canonical_output_bytes(
            replay_classification(load_snapshot(path)))
        in_memory = canonical_output_bytes(replay_classification(snapshot))
        assert from_disk == in_memory

    def test_replay_classifies_offline(self):
        snapshot = _snapshot()
        classification = replay_classification(snapshot)
        assert classification.record.source_event_key == EVENT_ROW[
            "source_event_key"]
        assert classification.asof_us == 1795612260804844
        assert classification.coverage_4h.status == "COMPLETE_COVERAGE"
        assert classification.coverage_1d.status == "COMPLETE_COVERAGE"
        assert classification.eligible is True

    def test_snapshot_type_is_immutable(self):
        snapshot = _snapshot()
        with pytest.raises(Exception):
            snapshot.symbol = "OTHER"  # frozen dataclass
