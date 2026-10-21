"""Issue #176 Phase-2 mandatory test matrix (7+8): checkpoint atomicity,
corruption fail-closedness, writer lease stale-lock recovery, resume replay
and duplicate suppression (at-least-once + idempotent)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.research.htf_baseline_short_phase2_state_store_v1 import (
    LEASE_SUFFIX,
    STATE_SCHEMA,
    LegacyCheckpointError,
    StateStoreError,
    WriterLease,
    WriterLeaseError,
    checkpoint_exists,
    checkpoint_exists as _ce,  # noqa: F401
    read_checkpoint,
    write_checkpoint,
)


def payload(generation: int = 1) -> dict:
    return {
        "experiment_id": "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
        "boundary_freeze_ts_ms": 1_760_000_000_000,
        "emitted": ["e1", "e2"],
        "generation": generation,
    }


# ------------------------------------------------------------- atomic writes
def test_write_read_roundtrip_with_checksummed_envelope(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, payload())
    assert checkpoint_exists(path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["schema"] == STATE_SCHEMA
    assert len(envelope["sha256"]) == 64
    assert read_checkpoint(path) == payload()


def test_interrupted_replace_never_clobbers_last_valid_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, payload(1))
    for fault in ("after-tempfile", "after-fsync-before-replace", "before-tempfile"):
        with pytest.raises(StateStoreError):
            write_checkpoint(path, payload(2), fault=fault)
        # last valid checkpoint survives untouched
        assert read_checkpoint(path) == payload(1)
    # no stray temp files left behind
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_unknown_fault_injection_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_checkpoint(tmp_path / "state.json", payload(), fault="replace-directory")


# ---------------------------------------------------------- fail-closed reads
def test_corrupt_json_rejected(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, payload())
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StateStoreError):
        read_checkpoint(path)


def test_tampered_payload_rejected_by_integrity_check(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, payload())
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["emitted"].append("forged")
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(StateStoreError) as excinfo:
        read_checkpoint(path)
    assert "integrity" in str(excinfo.value)


def test_legacy_and_unknown_schemas_rejected_never_reinterpreted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": "HTF_176_COMBINED_CHECKPOINT_V2", "payload": {}}), encoding="utf-8")
    with pytest.raises(LegacyCheckpointError):
        read_checkpoint(path)
    path.write_text(json.dumps({"payload": {}}), encoding="utf-8")
    with pytest.raises(LegacyCheckpointError):
        read_checkpoint(path)
    path.write_text(json.dumps({"plain": True}), encoding="utf-8")
    with pytest.raises(LegacyCheckpointError):
        read_checkpoint(path)


# ------------------------------------------------------------- writer lease
def test_second_live_writer_is_refused(tmp_path):
    path = tmp_path / "state.json"
    first = WriterLease(path, instance_id="run-1")
    first.acquire()
    second = WriterLease(path, instance_id="run-2")
    with pytest.raises(WriterLeaseError) as excinfo:
        second.acquire()
    assert "OBSERVER_WRITER_ALREADY_ACTIVE" in str(excinfo.value)
    first.release()
    second.acquire()  # after release, fine
    second.release()


def test_release_removes_only_own_lock(tmp_path):
    path = tmp_path / "state.json"
    lease = WriterLease(path, instance_id="run-1")
    lease.acquire()
    lock = json.loads((tmp_path / f"state.json{LEASE_SUFFIX}").read_text(encoding="utf-8"))
    lock["instance_id"] = "someone-else"
    (tmp_path / f"state.json{LEASE_SUFFIX}").write_text(json.dumps(lock), encoding="utf-8")
    lease.release()  # ownership lost -> must NOT unlink
    assert (tmp_path / f"state.json{LEASE_SUFFIX}").exists()


def test_stale_lock_not_taken_over_without_explicit_flag(tmp_path):
    lock_path = tmp_path / f"state.json{LEASE_SUFFIX}"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,  # dead/nonexistent
                "instance_id": "crashed",
                "acquired_at_ms": 0,
                "heartbeat_at_ms": 0,
            }
        ),
        encoding="utf-8",
    )
    lease = WriterLease(tmp_path / "state.json", instance_id="run-2", stale_after_s=1.0)
    with pytest.raises(WriterLeaseError):
        lease.acquire(allow_stale_takeover=False)
    assert lock_path.exists()  # unchanged


def test_stale_lock_recovered_only_with_explicit_takeover(tmp_path):
    lock_path = tmp_path / f"state.json{LEASE_SUFFIX}"
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "instance_id": "crashed", "heartbeat_at_ms": 0}),
        encoding="utf-8",
    )
    lease = WriterLease(tmp_path / "state.json", instance_id="run-2", stale_after_s=1.0)
    metadata = lease.acquire(allow_stale_takeover=True)
    assert metadata["instance_id"] == "run-2"
    lease.release()


def test_fresh_heartbeat_blocks_even_with_takeover_flag(tmp_path):
    lease1 = WriterLease(tmp_path / "state.json", instance_id="live-1", stale_after_s=600)
    lease1.acquire()
    lease1.heartbeat()
    lease2 = WriterLease(tmp_path / "state.json", instance_id="live-2", stale_after_s=600)
    with pytest.raises(WriterLeaseError):
        lease2.acquire(allow_stale_takeover=True)  # pid alive + fresh heartbeat
    lease1.release()


def test_heartbeat_updates_metadata(tmp_path):
    lease = WriterLease(tmp_path / "state.json", instance_id="run-1")
    lease.acquire()
    before = json.loads((tmp_path / f"state.json{LEASE_SUFFIX}").read_text(encoding="utf-8"))
    time.sleep(0.01)
    lease.heartbeat()
    after = json.loads((tmp_path / f"state.json{LEASE_SUFFIX}").read_text(encoding="utf-8"))
    assert after["heartbeat_at_ms"] >= before["heartbeat_at_ms"]
    lease.release()


def test_heartbeat_without_ownership_rejected(tmp_path):
    lease = WriterLease(tmp_path / "state.json", instance_id="run-1")
    with pytest.raises(WriterLeaseError):
        lease.heartbeat()


# ------------------------------------------- remediation CP5/CP6 lease safety


def test_live_owner_never_stolen_even_with_stale_heartbeat(tmp_path):
    lease1 = WriterLease(tmp_path / "state.json", instance_id="slow-writer", stale_after_s=0.001)
    lease1.acquire()
    time.sleep(0.02)  # heartbeat is now far older than stale_after_s
    lease2 = WriterLease(tmp_path / "state.json", instance_id="thief", stale_after_s=0.001)
    with pytest.raises(WriterLeaseError):
        lease2.acquire(allow_stale_takeover=True)  # PID alive -> never stale
    lease1.heartbeat()  # original owner still in control
    lease1.release()


def test_takeover_fences_the_original_owner(tmp_path):
    lease1 = WriterLease(tmp_path / "state.json", instance_id="run-1")
    lease1.acquire()
    # simulate run-1 having died: its owner PID is gone
    lock_path = tmp_path / f"state.json{LEASE_SUFFIX}"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["pid"] = 999_999_999
    lock["heartbeat_at_ms"] = 0
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    lease2 = WriterLease(tmp_path / "state.json", instance_id="run-2")
    lease2.acquire(allow_stale_takeover=True)
    # the fenced owner detects the takeover at its next heartbeat
    with pytest.raises(WriterLeaseError) as excinfo:
        lease1.heartbeat()
    assert "LEASE_LOST" in str(excinfo.value)
    # a fenced owner must never unlink the new owner's lock
    lease1.release()
    assert lock_path.exists()
    lease2.heartbeat()  # new owner still in control
    lease2.release()


def test_real_process_kill_lease_recovery(tmp_path):
    """CP6: actual subprocess kill, not metadata simulation."""
    import subprocess
    import sys
    import textwrap

    repo_root = Path(__file__).resolve().parent.parent
    state_path = tmp_path / "state.json"
    ready_flag = tmp_path / "child-ready"
    child_code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(repo_root)!r})
        from app.research.htf_baseline_short_phase2_state_store_v1 import WriterLease
        lease = WriterLease({str(state_path)!r}, instance_id="child")
        lease.acquire()
        open({str(ready_flag)!r}, "w").write("1")
        time.sleep(120)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not ready_flag.exists():
            time.sleep(0.05)
        assert ready_flag.exists(), "child did not acquire the lease in time"
        # while the child lives, even explicit takeover must be refused
        intruder = WriterLease(state_path, instance_id="intruder", stale_after_s=0.001)
        with pytest.raises(WriterLeaseError):
            intruder.acquire(allow_stale_takeover=True)
        proc.kill()  # the real thing: process termination
        proc.wait(timeout=30)
        # dead owner -> explicit takeover recovers the lease
        recovered = WriterLease(state_path, instance_id="recovered", stale_after_s=0.001)
        metadata = recovered.acquire(allow_stale_takeover=True)
        assert metadata["instance_id"] == "recovered"
        recovered.release()
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=30)
