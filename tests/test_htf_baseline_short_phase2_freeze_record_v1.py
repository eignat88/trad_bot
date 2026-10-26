"""Issue #197 / PR #200 FIX-1+FIX-2 — freeze-record tool unit tests (no DB).

Covers, with a fully stubbed database boundary (real SQL is exercised in
``test_htf_baseline_short_phase2_freeze_record_postgres_v1.py``):

* FIX-1 delayed activation repair: ``--repair-activation-json`` keeps working
  after the ``now()-60s .. now()+5min`` acceptance window expires, but ONLY
  against an already-committed, exactly-matching, registry-evidenced claim;
* FIX-4 claim-receipt record identity: delayed repair binds the presented
  record to the claim receipt sealed at the original claim (``record_id`` AND
  ``record_sha256`` must match exactly); a second record minted in the same
  second with a fully matching DB boundary is refused, and a missing claim
  receipt fails closed (the record self-hash is never proof of provenance);
* the initial activation path keeps the strict window in full and seals the
  claim receipt BEFORE the activation transaction commits;
* re-activation with a NEW timestamp is impossible (fail-closed, no JSON);
* any mismatch (record vs DB, tampered record, missing/mismatched
  freeze_registry evidence) fails closed BEFORE the activation JSON is
  written;
* FIX-2 ``_atomic_write_json`` hardening: symlink target/parent refused,
  temp-file squatting refused, no overwrite without the repair flag, mint
  never overwrites an existing freeze record.
"""
from __future__ import annotations

import ast
import inspect
import json
import multiprocessing
import os
import sys
import textwrap
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
        "modes": [],
    }

    def fake_load_state(conn, experiment_id):
        if box["activated"] is not None:
            _exp, ts, fp = box["activated"]
            return running_state(ts, fp)
        return box["state"]

    def fake_connect(mode):
        # issue #205: every admin session is opened for an explicit mode
        # (read_only = mint/verify/repair, activate = initial claim only)
        box["modes"].append(mode)
        return _FakeConn(box)

    monkeypatch.setattr(tool, "_connect", fake_connect)
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

    # FIX-4: the initial claim seals the claim receipt BEFORE activation; it
    # binds exactly this record identity for any later delayed repair
    record = load_freeze_record(record_path)
    receipt = json.loads(
        tool._claim_receipt_path(activation_path).read_text(encoding="utf-8")
    )
    assert receipt["record_id"] == record["record_id"]
    assert receipt["record_sha256"] == record["record_sha256"]
    assert receipt["freeze_ts"] == record["freeze_ts"]


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


def _seal(record: dict, activation_path: Path) -> Path:
    """Simulate the initial claim's one-time identity seal (FIX-4).

    The real seal is written by the initial claim BEFORE the activation
    transaction commits (exercised in the PostgreSQL IT suite); the repair
    fixtures below only need its presence and content.
    """
    return tool._seal_claim_receipt(activation_path, record)


def test_repair_delayed_after_window_succeeds(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    record = make_record()
    write_json(record_path, record)
    _committed(box)
    _seal(record, activation_path)  # original claim bound this record identity

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
    record = make_record()
    write_json(record_path, record)
    _committed(box)
    _seal(record, activation_path)
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
    record = make_record()
    write_json(record_path, record)
    _committed(box)
    _seal(record, activation_path)  # identity OK -> refusal must be the registry
    box["registry"] = None  # claim not evidenced -> partial failure state

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()


def test_repair_refuses_registry_mismatch(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    record = make_record()
    write_json(record_path, record)
    _committed(box)
    _seal(record, activation_path)  # identity OK -> refusal must be the registry
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


# ======================================= FIX-4: claim-receipt record identity
def test_repair_refuses_same_boundary_twin_record_identity(box, tmp_path):
    # Deterministic twin of the independent VPS defect: two ``mint`` runs in
    # the SAME second produce records with an identical DB boundary
    # (freeze_ts + protocol fingerprint) but DIFFERENT record_id /
    # record_sha256. Delayed repair accepts ONLY the record bound to the
    # original claim by the sealed claim receipt — never the twin, no matter
    # how completely the DB boundary matches.
    record_path = tmp_path / "freeze_record.json"
    twin_path = tmp_path / "freeze_record_twin.json"
    activation_path = tmp_path / "activation.json"
    record = make_record()
    twin = make_record()  # same freeze_ts + claimed_at + fingerprint, new UUID
    write_json(record_path, record)
    write_json(twin_path, twin)
    assert twin["freeze_ts"] == record["freeze_ts"]
    assert twin["protocol_fingerprint"] == record["protocol_fingerprint"]
    assert twin["record_id"] != record["record_id"]
    assert twin["record_sha256"] != record["record_sha256"]
    _committed(box)
    _seal(record, activation_path)  # the original claim sealed record's identity

    # the twin matches the DB boundary completely but is NOT the claimed record
    assert (
        tool.main(_claim_argv(twin_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])

    # the ORIGINAL record still repairs successfully
    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_OK
    )
    assert activation_path.exists()


def test_repair_fails_closed_without_claim_receipt(box, tmp_path):
    # FIX-4: when the original identity cannot be established (no claim
    # receipt), the repair fails closed — the record self-hash is tamper
    # evidence ONLY and is never accepted as proof that the record belongs to
    # the original GO
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    write_json(record_path, make_record())
    _committed(box)

    assert (
        tool.main(_claim_argv(record_path, activation_path, repair=True))
        == tool.EXIT_REFUSED
    )
    assert not activation_path.exists()
    assert not _any_activation_write(box["executed"])


def test_claim_receipt_seal_is_idempotent_for_the_same_record_only(tmp_path):
    activation_path = tmp_path / "activation.json"
    record = make_record()
    path = _seal(record, activation_path)
    first = path.read_text(encoding="utf-8")

    _seal(record, activation_path)  # byte-identical retry of the SAME record OK
    assert path.read_text(encoding="utf-8") == first

    twin = make_record()  # same boundary, different record identity
    with pytest.raises(tool.ToolRefused):
        tool._seal_claim_receipt(activation_path, twin)  # never replaced
    assert path.read_text(encoding="utf-8") == first


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


# ==========================================================================
# FIX-6: atomic no-clobber immutable publication (concurrent overwrite race)
# ==========================================================================


def _mp_worker_publish(result_path: str, target: str, text: str, barrier) -> None:
    """Module-level (spawn-picklable) worker: one competing publication."""
    barrier.wait()
    try:
        tool._atomic_write_bytes(target, text, overwrite=False)
        outcome = "OK"
    except tool.ToolRefused:
        outcome = "REFUSED"
    except Exception as exc:  # pragma: no cover - must never happen
        outcome = f"ERROR:{type(exc).__name__}:{exc}"
    Path(result_path).write_text(outcome, encoding="utf-8")


def _mp_worker_seal(result_path: str, activation_out: str, record: dict, barrier) -> None:
    """Module-level worker: one competing claim-receipt identity seal."""
    barrier.wait()
    try:
        tool._seal_claim_receipt(activation_out, record)
        outcome = "OK"
    except tool.ToolRefused:
        outcome = "REFUSED"
    except Exception as exc:  # pragma: no cover - must never happen
        outcome = f"ERROR:{type(exc).__name__}:{exc}"
    Path(result_path).write_text(outcome, encoding="utf-8")


def _run_concurrently(worker, arg_sets, tmp_path) -> list[str]:
    """Run ``worker`` in spawn processes released simultaneously (a real
    multiprocessing race, not threads)."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(arg_sets))
    procs = [
        ctx.Process(target=worker, args=(*args, barrier)) for args in arg_sets
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0, f"worker crashed: exitcode={proc.exitcode}"
    return [
        (tmp_path / f"result_{i}.txt").read_text(encoding="utf-8")
        for i in range(len(arg_sets))
    ]


def test_concurrent_immutable_publications_exactly_one_wins(tmp_path):
    """FIX-6 req. 1/6: competing writers publish DIFFERENT payloads with
    overwrite=False — exactly one publication succeeds and the final file
    contents belong to the winner. A concurrent writer must never overwrite
    a successfully published immutable artifact."""
    target = tmp_path / "freeze_record.json"
    n = 4
    payloads = [f"payload-{i}\n" for i in range(n)]
    arg_sets = [
        (str(tmp_path / f"result_{i}.txt"), str(target), payloads[i])
        for i in range(n)
    ]
    outcomes = _run_concurrently(_mp_worker_publish, arg_sets, tmp_path)
    assert outcomes.count("OK") == 1, outcomes
    assert outcomes.count("REFUSED") == n - 1, outcomes
    winner = outcomes.index("OK")
    assert target.read_text(encoding="utf-8") == payloads[winner]
    # req. 5: no temp name survives a publication conflict
    assert list(tmp_path.glob(".freeze_record.json.tmp.*")) == []


def test_concurrent_claim_receipt_seals_bind_one_identity(tmp_path):
    """FIX-6 req. 3/7: two DIFFERENT records claimed concurrently — the
    claim-receipt identity is sealed for exactly one of them and can never
    change afterwards."""
    activation_out = tmp_path / "htf_phase2_activation.json"
    record_a = make_record(experiment_id="EXPERIMENT_A")
    record_b = make_record(experiment_id="EXPERIMENT_B")
    arg_sets = [
        (str(tmp_path / "result_0.txt"), str(activation_out), record_a),
        (str(tmp_path / "result_1.txt"), str(activation_out), record_b),
    ]
    outcomes = _run_concurrently(_mp_worker_seal, arg_sets, tmp_path)
    assert outcomes.count("OK") == 1, outcomes
    assert outcomes.count("REFUSED") == 1, outcomes
    winner_record, loser_record = (
        (record_a, record_b) if outcomes[0] == "OK" else (record_b, record_a)
    )
    receipt = tool.load_claim_receipt(
        str(tool._claim_receipt_path(str(activation_out)))
    )
    tool.verify_claim_identity(receipt, winner_record)  # winner binds
    with pytest.raises(tool.FreezeRecordError):
        tool.verify_claim_identity(receipt, loser_record)  # loser can never bind
    # the sealed identity cannot change, not even by a later seal attempt
    with pytest.raises(tool.ToolRefused):
        tool._seal_claim_receipt(str(activation_out), loser_record)
    assert list(tmp_path.glob(".htf_phase2_activation.json.tmp.*")) == []


def test_publication_conflict_cleans_temp_and_preserves_winner(monkeypatch, tmp_path):
    """FIX-6 req. 4/5: when a competitor publishes between the existence
    check and our publication, the conflict fails closed, the winner's bytes
    are preserved, and the temp file is cleaned up."""
    target = tmp_path / "artifact.json"
    real_link = os.link

    def _racing_link(src, dst):
        # simulate the concurrent winner that published first
        Path(dst).write_text("winner\n", encoding="utf-8")
        real_link(src, dst)  # must now fail with FileExistsError

    monkeypatch.setattr(tool.os, "link", _racing_link)
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_bytes(target, "late-comer\n", overwrite=False)
    assert target.read_text(encoding="utf-8") == "winner\n"
    assert list(tmp_path.glob(".artifact.json.tmp.*")) == []


def test_immutable_publication_never_calls_os_replace(monkeypatch, tmp_path):
    """FIX-6 req. 9: regression guard — immutable publication must NEVER go
    through os.replace() again; the authorized repair path (overwrite=True)
    remains the only os.replace() user."""
    calls: list[tuple[str, str]] = []

    def _bomb(src, dst):
        calls.append((str(src), str(dst)))
        raise AssertionError("os.replace() must not run in the immutable path")

    monkeypatch.setattr(os, "replace", _bomb)

    # freeze-record / claim-receipt / initial-activation style publication
    path = tmp_path / "immutable_artifact.json"
    tool._atomic_write_bytes(path, "first\n", overwrite=False)
    assert path.read_text(encoding="utf-8") == "first\n"
    assert calls == []

    # the claim-receipt identity seal equally never uses os.replace()
    receipt_path = tool._seal_claim_receipt(
        tmp_path / "activation.json", make_record()
    )
    assert receipt_path.exists()
    assert calls == []

    # the authorized activation-JSON repair path is the ONLY os.replace user
    with pytest.raises(AssertionError):
        tool._atomic_write_bytes(path, "repaired\n", overwrite=True)
    assert len(calls) == 1
    assert path.read_text(encoding="utf-8") == "first\n"
    assert list(tmp_path.glob(".*.tmp.*")) == []


def test_immutable_publication_source_has_single_replace_on_repair_path():
    """FIX-6 req. 9: static AST guard — immutable publication must use
    os.link(), and at most ONE os.replace() call may exist inside
    _atomic_write_bytes: the authorized repair one, inside ``if overwrite``."""
    source = inspect.getsource(tool._atomic_write_bytes)
    tree = ast.parse(textwrap.dedent(source))
    os_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ]
    link_calls = [n for n in os_calls if n.func.attr == "link"]
    replace_calls = [n for n in os_calls if n.func.attr == "replace"]
    assert link_calls, "immutable publication must use os.link()"
    assert len(replace_calls) == 1, (
        "os.replace() is allowed exactly once — the authorized repair path"
    )
    overwrite_bodies = [
        stmt
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "overwrite"
        for stmt in node.body
    ]
    assert any(
        replace_calls[0] is node
        for stmt in overwrite_bodies
        for node in ast.walk(stmt)
    ), "the only os.replace() call must sit inside the overwrite=True branch"


def test_os_link_refuses_existing_target_on_this_platform(tmp_path):
    """FIX-6 req. 8: PROVE, don't assume — os.link() must be no-clobber on
    the platform under test (Windows: CreateHardLinkW fails with
    ERROR_ALREADY_EXISTS; Linux: link(2) fails with EEXIST). Immutable
    publication relies on exactly this, never on rename semantics (which
    differ across platforms)."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a\n", encoding="utf-8")
    b.write_text("b\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        os.link(str(a), str(b))
    assert b.read_text(encoding="utf-8") == "b\n"
    # a free destination still publishes
    c = tmp_path / "c.txt"
    os.link(str(a), str(c))
    assert c.read_text(encoding="utf-8") == "a\n"


def test_temp_file_cleaned_up_on_precheck_refusal(tmp_path):
    """FIX-6 req. 5: fail-closed error paths never leave a temp file."""
    path = tmp_path / "artifact.json"
    path.write_text("published\n", encoding="utf-8")
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_bytes(path, "late\n", overwrite=False)
    assert list(tmp_path.glob(".artifact.json.tmp.*")) == []


def test_temp_file_cleaned_up_on_symlink_refusal(tmp_path):
    """FIX-6 req. 5: the symlink fail-closed path also leaves no temp file."""
    path = tmp_path / "artifact.json"
    path.write_text("published\n", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(path)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(tool.ToolRefused):
        tool._atomic_write_bytes(link, "x\n", overwrite=False)
    assert list(tmp_path.glob(".link.json.tmp.*")) == []


# ======================================================================
# Issue #205: admin connection routing — per-operation session modes
# ======================================================================


def _mint_argv(record_path: Path) -> list:
    return [
        "mint",
        "--operator-approval-ref",
        "GO-UNIT-205",
        "--issued-by",
        "unit-operator",
        "--record-id",
        str(uuid.uuid4()),
        "--out",
        str(record_path),
    ]


def test_mint_uses_read_only_admin_session_and_writes_nothing(box, tmp_path):
    """FIX-2: mint must never hold write capability (issue #205)."""
    record_path = tmp_path / "freeze_record.json"
    assert tool.main(_mint_argv(record_path)) == tool.EXIT_OK
    assert record_path.exists()
    assert box["modes"] == ["read_only"]
    assert box["activated"] is None  # no activation, no DB write, ever
    assert not _any_activation_write(box["executed"])


def test_initial_claim_opens_read_probe_then_activate_session(box, tmp_path):
    """The activation privilege is held ONLY by the initial claim (issue #205)."""
    record_path = tmp_path / "freeze_record.json"
    assert tool.main(_mint_argv(record_path)) == tool.EXIT_OK
    activation = tmp_path / "activation.json"
    assert tool.main(_claim_argv(record_path, activation)) == tool.EXIT_OK
    assert box["modes"] == ["read_only", "read_only", "activate"]
    assert box["activated"] is not None


def test_repair_never_opens_an_activate_session(box, tmp_path):
    """--repair-activation-json stays read-only end to end (issue #205)."""
    record_path = tmp_path / "freeze_record.json"
    assert tool.main(_mint_argv(record_path)) == tool.EXIT_OK
    activation = tmp_path / "activation.json"
    assert tool.main(_claim_argv(record_path, activation)) == tool.EXIT_OK
    activation.unlink()
    box["registry"] = {"experiment_id": EXPERIMENT_ID, "protocol_fingerprint": FP}
    assert tool.main(_claim_argv(record_path, activation, repair=True)) == tool.EXIT_OK
    assert box["modes"] == ["read_only", "read_only", "activate", "read_only"]
    assert activation.exists()


def test_verify_is_read_only_and_requires_registry_evidence(box, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    assert tool.main(_mint_argv(record_path)) == tool.EXIT_OK
    activation = tmp_path / "activation.json"
    assert tool.main(_claim_argv(record_path, activation)) == tool.EXIT_OK
    box["registry"] = {"experiment_id": EXPERIMENT_ID, "protocol_fingerprint": FP}
    assert (
        tool.main(["verify", "--record", str(record_path), "--activation", str(activation)])
        == tool.EXIT_OK
    )
    assert box["modes"][-1] == "read_only"


def test_verify_refuses_missing_registry_evidence(box, tmp_path):
    """FIX-4: without freeze_registry evidence the agreement cannot be proven."""
    record_path = tmp_path / "freeze_record.json"
    assert tool.main(_mint_argv(record_path)) == tool.EXIT_OK
    activation = tmp_path / "activation.json"
    assert tool.main(_claim_argv(record_path, activation)) == tool.EXIT_OK
    box["registry"] = None
    assert (
        tool.main(["verify", "--record", str(record_path), "--activation", str(activation)])
        == tool.EXIT_REFUSED
    )
