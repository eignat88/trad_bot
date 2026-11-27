"""Issue #314 / H1-D — read-only CLI runner tests (offline, guarded fetch).

Covers: default offline replay mode (ZERO HTTP, byte-identical output),
fetch refusal without BOTH authorizations (explicit flag AND sentinel
file), guarded fetch happy path against a stub client (snapshot written,
``fixture_label=REAL_BYBIT_FETCH``), fail-closed behavior on limiter /
breaker denial (no snapshot, non-zero exit), and ``get_klines_range`` never
being called.

Deterministic and offline: every client is a stub; NO real Bybit requests.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.exchange.bybit_client import BybitClient, BybitRateLimitError
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h1d_v1.provenance import (
    FIXTURE_LABEL_REAL,
    FIXTURE_LABEL_SYNTHETIC,
    build_snapshot,
    canonical_output_bytes,
    load_snapshot,
    replay_classification,
    save_snapshot,
)
from app.research.me_entry_quality_h1d_v1.runner import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    main,
)

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


def _path(i):
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


def _fixture_snapshot():
    record = adapt_source_events([EVENT_ROW]).records[0]
    asof_us = record.signal_time_us
    latest_4h = ((asof_us // 1000) - TF) // TF * TF
    latest_1d = ((asof_us // 1000) - DAY) // DAY * DAY
    rows_4h = _zigzag_rows(latest_4h - 59 * TF, 60, TF)
    rows_1d = _zigzag_rows(latest_1d - 59 * DAY, 60, DAY)
    return build_snapshot(
        record=record, rows_4h=rows_4h, rows_1d=rows_1d,
        source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
        source_label="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:htf",
        source_label_4h="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:4H",
        source_label_1d="SYNTHETIC_TEST_FIXTURE:SOXSUSDT:1D",
        fixture_label=FIXTURE_LABEL_SYNTHETIC)


class StubKlineApi:
    """Bybit V5 ``/v5/market/kline`` double (bounds-aware, newest-first)."""

    def __init__(self, *, rows_4h, rows_1d, fail_with=None):
        self.rows = {"240": list(rows_4h), "D": list(rows_1d)}
        self.calls = []
        self.fail_with = fail_with

    def _public_get(self, endpoint, **params):
        self.calls.append((endpoint, dict(params)))
        if self.fail_with is not None:
            raise self.fail_with
        start, end = params.get("start"), params.get("end")
        data = []
        for row in self.rows.get(params.get("interval"), []):
            if start is not None and row["open_ms"] < start:
                continue
            if end is not None and row["open_ms"] > end:
                continue
            data.append(row)
        data.sort(key=lambda row: row["open_ms"], reverse=True)
        data = data[:params["limit"]]
        return {"retCode": 0, "retMsg": "OK", "result": {"list": list(data)}}

    def get_klines_range(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("get_klines_range must NEVER be called")


def _stub_data():
    record = adapt_source_events([EVENT_ROW]).records[0]
    asof_us = record.signal_time_us
    latest_4h = ((asof_us // 1000) - TF) // TF * TF
    latest_1d = ((asof_us // 1000) - DAY) // DAY * DAY
    return (_zigzag_rows(latest_4h - 65 * TF, 70, TF),
            _zigzag_rows(latest_1d - 65 * DAY, 70, DAY))


def _event_file(tmp_path):
    path = tmp_path / "event.json"
    path.write_bytes(json.dumps(EVENT_ROW).encode("utf-8"))
    return str(path)


# ─── default offline replay mode ───────────────────────────────────────────


class TestReplayMode:
    def test_replay_is_default_and_offline(self, tmp_path, monkeypatch):
        snapshot = _fixture_snapshot()
        snap_path = tmp_path / "snap.json"
        save_snapshot(snapshot, snap_path)
        out_path = tmp_path / "out.json"

        def _boom():
            raise AssertionError("client constructed during replay")

        monkeypatch.setattr(BybitClient, "_public_get",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("HTTP during replay"))))
        code = main(["--replay", str(snap_path), "--out", str(out_path)],
                    client_factory=_boom)
        assert code == EXIT_OK
        expected = canonical_output_bytes(replay_classification(snapshot))
        assert out_path.read_bytes() == expected

    def test_replay_output_is_byte_identical_across_runs(self, tmp_path):
        snapshot = _fixture_snapshot()
        snap_path = tmp_path / "snap.json"
        save_snapshot(snapshot, snap_path)
        outs = []
        for i in range(2):
            out_path = tmp_path / f"out{i}.json"
            assert main(["--replay", str(snap_path),
                         "--out", str(out_path)]) == EXIT_OK
            outs.append(out_path.read_bytes())
        assert outs[0] == outs[1]

    def test_no_mode_selected_fails(self):
        assert main([]) == EXIT_FAILURE

    def test_replay_of_missing_snapshot_fails(self):
        assert main(["--replay", "does-not-exist.json"]) == EXIT_FAILURE

    def test_replay_of_tampered_snapshot_fails(self, tmp_path):
        snapshot = _fixture_snapshot()
        snap_path = tmp_path / "snap.json"
        save_snapshot(snapshot, snap_path)
        document = json.loads(snap_path.read_bytes())
        document["payload_4h"]["rows"][0]["close"] = 1.0
        snap_path.write_bytes(json.dumps(document).encode("utf-8"))
        assert main(["--replay", str(snap_path)]) == EXIT_FAILURE


# ─── guarded fetch mode: refuses without BOTH authorizations ───────────────


class TestFetchRefusal:
    def test_fetch_without_flag_is_refused(self, tmp_path, capsys):
        code = main(["--fetch",
                     "--sentinel", str(tmp_path / "sentinel"),
                     "--source-event", _event_file(tmp_path),
                     "--out-snapshot", str(tmp_path / "snap.json")])
        assert code == EXIT_REFUSED
        err = capsys.readouterr().err
        assert "REFUSED" in err
        assert "SEPARATE OWNER APPROVAL" in err
        assert not (tmp_path / "snap.json").exists()

    def test_fetch_without_sentinel_is_refused(self, tmp_path, capsys):
        code = main(["--fetch", "--fetch-authorized",
                     "--sentinel", str(tmp_path / "missing.sentinel"),
                     "--source-event", _event_file(tmp_path),
                     "--out-snapshot", str(tmp_path / "snap.json")])
        assert code == EXIT_REFUSED
        assert "sentinel" in capsys.readouterr().err
        assert not (tmp_path / "snap.json").exists()

    def test_default_sentinel_is_required(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no H1D_REAL_FETCH_APPROVED.sentinel here
        code = main(["--fetch", "--fetch-authorized",
                     "--source-event", _event_file(tmp_path),
                     "--out-snapshot", str(tmp_path / "snap.json")])
        assert code == EXIT_REFUSED
        assert not (tmp_path / "snap.json").exists()


# ─── guarded fetch mode: happy path (stub client, zero real HTTP) ─────────


class TestFetchGuardedHappyPath:
    def _run(self, tmp_path, stub):
        rows_4h, rows_1d = _stub_data()
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("H1D_REAL_BYBIT_FETCH_APPROVED", encoding="utf-8")
        snap_path = tmp_path / "snap.json"
        out_path = tmp_path / "out.json"
        code = main(
            ["--fetch", "--fetch-authorized",
             "--sentinel", str(sentinel),
             "--source-event", _event_file(tmp_path),
             "--out-snapshot", str(snap_path),
             "--out", str(out_path)],
            client_factory=lambda: stub)
        return code, snap_path, out_path, rows_4h, rows_1d

    def test_authorized_fetch_writes_real_labeled_snapshot(self, tmp_path):
        rows_4h, rows_1d = _stub_data()
        stub = StubKlineApi(rows_4h=rows_4h, rows_1d=rows_1d)
        code, snap_path, out_path, _, _ = self._run(tmp_path, stub)
        assert code == EXIT_OK
        snapshot = load_snapshot(snap_path)
        assert snapshot.fixture_label == FIXTURE_LABEL_REAL
        assert snapshot.payload_4h.interval == "240"
        assert snapshot.payload_1d.interval == "D"
        assert len(snapshot.payload_4h.rows) == 60
        assert len(snapshot.payload_1d.rows) == 60
        # classification output written and replayable offline
        expected = canonical_output_bytes(replay_classification(snapshot))
        assert out_path.read_bytes() == expected

    def test_fetch_uses_only_public_get_with_bounded_params(self, tmp_path):
        rows_4h, rows_1d = _stub_data()
        stub = StubKlineApi(rows_4h=rows_4h, rows_1d=rows_1d)
        code, *_ = self._run(tmp_path, stub)
        assert code == EXIT_OK
        intervals = {params["interval"] for _, params in stub.calls}
        assert intervals == {"240", "D"}
        for _, params in stub.calls:
            assert params["category"] == "linear"
            assert "deadline_s" in params

    def test_fetch_missing_source_event_fails(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("ok", encoding="utf-8")
        code = main(["--fetch", "--fetch-authorized",
                     "--sentinel", str(sentinel),
                     "--source-event", str(tmp_path / "missing.json"),
                     "--out-snapshot", str(tmp_path / "snap.json")])
        assert code == EXIT_FAILURE


# ─── guarded fetch mode: fail closed on limiter / breaker / errors ─────────


class TestFetchFailClosed:
    def _attempt(self, tmp_path, stub):
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("ok", encoding="utf-8")
        snap_path = tmp_path / "snap.json"
        code = main(
            ["--fetch", "--fetch-authorized",
             "--sentinel", str(sentinel),
             "--source-event", _event_file(tmp_path),
             "--out-snapshot", str(snap_path)],
            client_factory=lambda: stub)
        return code, snap_path

    def test_breaker_denial_fails_closed_without_snapshot(self, tmp_path):
        stub = StubKlineApi(rows_4h=[], rows_1d=[], fail_with=(
            BybitRateLimitError("breaker open", limiter_result="BREAKER_OPEN")))
        code, snap_path = self._attempt(tmp_path, stub)
        assert code == EXIT_FAILURE
        assert not snap_path.exists()

    def test_limiter_denial_fails_closed_without_snapshot(self, tmp_path):
        stub = StubKlineApi(rows_4h=[], rows_1d=[], fail_with=(
            BybitRateLimitError("denied", limiter_result="SKIPPED")))
        code, snap_path = self._attempt(tmp_path, stub)
        assert code == EXIT_FAILURE
        assert not snap_path.exists()

    def test_empty_fetch_fails_closed_without_snapshot(self, tmp_path):
        stub = StubKlineApi(rows_4h=[], rows_1d=[])
        code, snap_path = self._attempt(tmp_path, stub)
        assert code == EXIT_FAILURE
        assert not snap_path.exists()
