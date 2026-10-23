"""Issue #197 / PR #200 FIX-1+FIX-2 — freeze-record tool unit tests (no DB).

Covers, with a fully stubbed database boundary (real SQL is exercised in
``test_htf_baseline_short_phase2_freeze_record_postgres_v1.py``):

* FIX-1 delayed activation repair: ``--repair-activation-json`` keeps working
  after the ``now()-60s .. now()+5min`` acceptance window expires, but ONLY
  against an already-committed, exactly-matching, registry-evidenced claim;
* the initial activation path keeps the strict window in full;
* re-activation with a NEW timestamp is impossible (fail-closed, no JSON);
* any mismatch (record vs DB, tampered record, missing/mismatched
  freeze_registry evidence) fails closed BEFORE the activation JSON is
  written;
* FIX-2 ``_atomic_write_json`` hardening: symlink target/parent refused,
  temp-file squatting refused, no overwrite without the repair flag, mint
  never overwrites an existing freeze record.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))

import htf_baseline_short_phase2_freeze_record_v1 as tool  # noqa: E402
from app.research.htf_baseline_short_phase2_protocol_v1 import (  # noqa: E402
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.research.htf_baseline_short_phase2_timestamp_authority_v1 import (  # noqa: E402
    build_freeze_record,
    freeze_record_hash,
    load_freeze_record,
)

FP = protocol_fingerprint()
BASE_TS = "2026-10-23T12:00:00Z"  # NOT the Point-B boundary; fresh identity


def _dt(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def make_record(**over) -> dict:
    freeze_ts = over.pop("freeze_ts", BASE_TS)
    claimed_at = over.pop("claimed_at", BASE_TS)
    record = build_freeze_record(
        experiment_id=over.pop("experiment_id", EXPERIMENT_ID),
        freeze_ts=freeze_ts,
        protocol_fingerprint=over.pop("protocol_fingerprint", FP),
        claimed_at=claimed_at,
        operator_approval_ref=over.pop("operator_approval_ref", "GO-UNIT-001"),
        issued_by=over.pop("issued_by", "unit-operator"),
        record_id=over.pop("record_id", str(uuid.uuid4())),
    )
    record.update(over)
    if over:  # re-seal unless the caller explicitly wants a tampered record
        record["record_sha256"] = freeze_record_hash(record)
    return record


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class _FakeCursor:
    def __init__(self, box):
        self.box = box

    def execute(self, sql, params=None):
        self.box.setdefault("executed", []).append((sql, params))
        if "htf_baseline_short_phase2_activate" in sql:
            self.box["activated"] = params

    def fetchone(self):
        return ("ok",)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, box):
        self.box = box

    def cursor(self):
        return _FakeCursor(self.box)

    def commit(self):
        self.box["committed"] = True


def pending_state() -> SimpleNamespace:
    return SimpleNamespace(
        status="PENDING", freeze_ts=None, protocol_fingerprint=None, observer_enabled=False
    )


def running_state(freeze_ts: str, fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        status="RUNNING",
        freeze_ts=freeze_ts,
        protocol_fingerprint=fingerprint,
        observer_enabled=False,
    )


@pytest.fixture()
def box(monkeypatch):
    """Stubbed DB boundary: state + registry + clock live in one dict."""
    box = {
        "state": pending_state(),
        "registry": None,
        "db_now": _dt(BASE_TS),
        "activated": None,
        "executed": [],
    }

    def fake_load_state(conn, experiment_id):
        if box["activated"] is not None:
            _exp, ts, fp = box["activated"]
            return running_state(ts, fp)
        return box["state"]

    monkeypatch.setattr(tool, "_connect", lambda: _FakeConn(box))
    monkeypatch.setattr(tool, "_close", lambda conn: None)
    monkeypatch.setattr(tool, "_db_now", lambda conn: box["db_now"])
    monkeypatch.setattr(tool, "_load_state", fake_load_state)
    monkeypatch.setattr(tool, "_registry_row", lambda conn, freeze_ts: box["registry"])
    return box


def _claim_argv(record_path: Path, activation_path: Path, repair: bool = False):
    argv = [
        "claim",
        "--record",
        str(record_path),
        "--activation-out",
        str(activation_path),
    ]
    if repair:
        argv.append("--repair-activation-json")
    return argv


def _any_activation_write(executed) -> bool:
    return any("activate" in sql for sql, _ in executed)


# ============================================================ FIX-1: initial claim
def test_initial_claim_inside_window_activates_and_writes_json(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    box["db_now"] = _dt(BASE_TS)

    assert tool.main(_claim_argv(record_path, activation_path)) == tool.EXIT_OK
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    assert activation["freeze_ts"] == BASE_TS
    assert activation["protocol_fingerprint"] == FP
    assert activation["experiment_id"] == EXPERIMENT_ID
    assert _any_activation_write(box["executed"])


def test_initial_claim_enforces_window_after_expiry(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    from datetime import timedelta

    box["db_now"] = _dt(BASE_TS) + timedelta(seconds=tool.ACCEPTANCE_FUTURE_WINDOW_S + 60)

    assert tool.main(_claim_argv(record_path, activation_path)) == tool.EXIT_REFUSED
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


def test_initial_claim_rejects_far_future_record(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    from datetime import timedelta

    box["db_now"] = _dt(BASE_TS) - timedelta(hours=2)

    assert tool.main(_claim_argv(record_path, activation_path)) == tool.EXIT_REFUSED
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


# ================================================ FIX-1: delayed repair after claim
def _committed(box):
    box["state"] = running_state(BASE_TS, FP)
    box["registry"] = {"experiment_id": EXPERIMENT_ID, "protocol_fingerprint": FP}
    from datetime import timedelta

    box["db_now"] = _dt(BASE_TS) + timedelta(hours=3)  # window long expired


def test_repair_delayed_after_window_succeeds(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_OK
    )
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    assert activation["freeze_ts"] == BASE_TS
    assert activation["protocol_fingerprint"] == FP
    assert not _any_activation_write(box["executed"])  # repair never activates


def test_partial_recovery_missing_activation_json_restored(box, tmp_path):
    # documented recovery: claim committed, activation JSON lost -> repair
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)
    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_OK
    )
    activation_path.unlink()  # simulate the interrupted second half
    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_OK
    )
    assert activation_path.exists()


def test_repair_without_flag_still_refused(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)

    assert tool.main(_claim_argv(record_path, activation_path)) == tool.EXIT_REFUSED
    assert not activation_path.exists()


def test_repair_refuses_mismatched_freeze_ts(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)
    box["state"] = running_state("2026-10-23T13:00:00Z", FP)  # DB holds another ts

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


def test_repair_refuses_mismatched_fingerprint(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)
    box["state"] = running_state(BASE_TS, "0" * 64)

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()


def test_repair_refuses_tampered_record(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    record = make_record()
    write_json(record_path, record)
    record["issued_by"] = "someone-else"  # tamper WITHOUT re-sealing
    write_json(record_path, record)
    _committed(box)

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()


def test_repair_refuses_missing_registry_evidence(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)
    box["registry"] = None  # claim not evidenced -> partial failure state

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()


def test_repair_refuses_registry_mismatch(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)
    box["registry"] = {"experiment_id": EXPERIMENT_ID, "protocol_fingerprint": "1" * 64}

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()


def test_reactivation_with_new_timestamp_is_refused(box, tmp_path):
    # a NEW timestamp after a committed claim can never be activated
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record(freeze_ts="2026-10-23T15:00:00Z"))
    _committed(box)  # DB already holds BASE_TS

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


def test_repair_flag_is_never_combined_with_initial_activation(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    box["db_now"] = _dt(BASE_TS)  # fresh window, but DB is still PENDING

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


def test_activation_out_must_not_be_the_record_path(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    write_json(record_path, make_record())
    _committed(box)
    before = record_path.read_text(encoding="utf-8")

    assert (
        tool.main(_claim_argv(record_path, record_path, repair=True)) == tool.EXIT_REFUSED
    )
    assert record_path.read_text(encoding="utf-8") == before  # record unchanged


# ================================================== FIX-2: atomic JSON hardening
def test_atomic_write_refuses_overwrite_without_flag(tmp_path):
    target = tmp_path / "artifact.json"
    tool._atomic_write_json(target, {"a": 1}, overwrite=False)
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_json(target, {"a": 2}, overwrite=False)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_atomic_write_overwrite_flag_replaces(tmp_path):
    target = tmp_path / "artifact.json"
    tool._atomic_write_json(target, {"a": 1}, overwrite=False)
    tool._atomic_write_json(target, {"a": 2}, overwrite=True)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


def test_atomic_write_refuses_preexisting_temp_file(tmp_path):
    target = tmp_path / "artifact.json"
    squat = tmp_path / f".artifact.json.tmp.{os.getpid()}"
    squat.write_text("squatting", encoding="utf-8")
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_json(target, {"a": 1}, overwrite=False)
    assert not target.exists()
    assert squat.read_text(encoding="utf-8") == "squatting"  # not clobbered


def test_atomic_write_refuses_symlink_target(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_json(link, {"a": 1}, overwrite=True)
    assert real.read_text(encoding="utf-8") == "{}"


def test_atomic_write_refuses_symlink_parent_dir(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_json(link_dir / "artifact.json", {"a": 1}, overwrite=False)


def test_atomic_write_refuses_missing_parent_dir(tmp_path):
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_json(tmp_path / "missing" / "artifact.json", {"a": 1}, overwrite=False)


def test_mint_never_overwrites_existing_freeze_record(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    write_json(record_path, make_record())
    before = record_path.read_text(encoding="utf-8")
    argv = [
        "mint",
        "--operator-approval-ref",
        "GO-UNIT-002",
        "--issued-by",
        "unit-operator",
        "--record-id",
        str(uuid.uuid4()),
        "--out",
        str(record_path),
    ]
    assert tool.main(argv) == tool.EXIT_REFUSED
    assert record_path.read_text(encoding="utf-8") == before


def test_loaded_record_roundtrip_matches_hash(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    record = make_record()
    write_json(record_path, record)
    loaded = load_freeze_record(record_path)
    assert freeze_record_hash(loaded) == loaded["record_sha256"]
