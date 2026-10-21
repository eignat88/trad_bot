"""#176 Phase-2 readiness blockers: lifecycle and crash-injection regressions."""
import json
import pytest
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes
from tools.research.htf_baseline_short_checkpoint_writer_lock_v1 import writer_lock
from tools.research.htf_baseline_short_atomic_checkpoint_v1 import (
    checkpoint_envelope, read_checkpoint, write_checkpoint,
)

STEP = 300_000


def test_eligibility_loss_is_non_terminal_and_pending_survives():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    episode_id = state.pending["L"]
    state.advance(
        open_ms=STEP,
        level_contacts={"L": (100.0, 0, False)},
        eligibility_loss={"L"},
    )
    assert state.pending["L"] == episode_id
    assert state.episodes[episode_id]["state"] == "CONTACT"
    assert state.episodes[episode_id]["terminal_reason"] is None
    assert state.advance(
        open_ms=2 * STEP,
        level_contacts={"L": (100.0, 0, False)},
        eligibility_loss={"L"},
        confirmations=["L"],
    )[0]["episode_id"] == episode_id


def test_reentry_after_eligibility_loss_preserves_pending_identity():
    state = MultiLevelEpisodes("ETHUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    episode_id = state.pending["L"]
    state.advance(
        open_ms=STEP,
        level_contacts={"L": (100.0, 0, True)},
        eligibility_loss={"L"},
    )
    assert state.pending["L"] == episode_id
    assert len(state.episodes) == 1


def test_unresolved_terminal_reason_is_documented_and_not_retired():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    episode_id = state.pending["L"]
    state.advance(
        open_ms=STEP,
        level_contacts={},
        unavailable=["L"],
        eligibility_loss={"L"},
    )
    assert state.episodes[episode_id]["state"] == "UNRESOLVED"
    assert state.episodes[episode_id]["terminal_reason"] == "SOURCE_WINDOW_EVICTED"
    assert not state.events


def test_unknown_eligibility_loss_fails_closed():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    with pytest.raises(ValueError, match="without known level"):
        state.advance(open_ms=STEP, level_contacts={"L": (100.0, 0, False)},
                      eligibility_loss={"UNKNOWN"})


def test_writer_lock_rejects_concurrent_process_and_releases_cleanly(tmp_path):
    path=tmp_path/"state.json"
    with writer_lock(path):
        with pytest.raises(RuntimeError,match="OBSERVER_WRITER_ALREADY_ACTIVE"):
            with writer_lock(path):
                pass
    with writer_lock(path):
        write_checkpoint(path,{"cursor":1})
    assert not (tmp_path/"state.json.writer.lock").exists()
    assert read_checkpoint(path)=={"cursor":1}


def test_checkpoint_rejects_unknown_fault():
    with pytest.raises(ValueError, match="unknown checkpoint fault"):
        write_checkpoint("unused.json", {"cursor": 1}, fault="replace-directory")


def test_checkpoint_round_trip_restores_known_level_ids_as_set():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, False)})
    restored = MultiLevelEpisodes.restore(json.loads(json.dumps(state.checkpoint())))
    assert restored.known_level_ids == {"L"}
    assert restored.active == {}
    restored.advance(open_ms=STEP, level_contacts={"L": (100.0, 0, True)},
                     eligibility_loss={"L"})
    assert restored.pending["L"] == next(iter(restored.episodes))


def test_provenance_recovery_replays_verified_payload(tmp_path):
    path = tmp_path/"state.json"
    write_checkpoint(path,{"cursor":1})
    provenance=checkpoint_envelope(path)
    path.write_text('{"schema":"HTF_176_COMBINED_CHECKPOINT_V2"}')
    recovery=tmp_path/"recovery.json"
    write_checkpoint(recovery,provenance["payload"],provenance_envelope=provenance)
    assert read_checkpoint(recovery)==provenance["payload"]
    assert checkpoint_envelope(recovery)["sha256"]==provenance["sha256"]


def test_stale_writer_lock_requires_operator_recovery(tmp_path):
    path = tmp_path/"state.json"
    (tmp_path/"state.json.writer.lock").write_text("stale")
    with pytest.raises(RuntimeError,match="OBSERVER_WRITER_ALREADY_ACTIVE"):
        with writer_lock(path):
            pass
    assert (tmp_path/"state.json.writer.lock").read_text()=="stale"


def test_checkpoint_rejects_unknown_fault_before_tempfile(tmp_path):
    with pytest.raises(ValueError, match="unknown checkpoint fault"):
        write_checkpoint(tmp_path/"unused.json", {"cursor": 1}, fault="replace-directory")
    assert list(tmp_path.glob("unused.json.*.tmp")) == []


def test_pre_replace_fault_preserves_last_valid_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    old = {"cursor": 1}
    write_checkpoint(path, old)
    with pytest.raises(RuntimeError, match="SIMULATED_BEFORE_ATOMIC_REPLACE"):
        write_checkpoint(path, {"cursor": 2}, simulate_interrupt=True)
    assert read_checkpoint(path) == old


def test_interrupted_temp_write_is_cleaned_and_last_valid_survives(tmp_path):
    path = tmp_path / "state.json"
    old = {"cursor": 1}
    write_checkpoint(path, old)
    with pytest.raises(RuntimeError, match="SIMULATED_BEFORE_ATOMIC_REPLACE"):
        write_checkpoint(path, {"cursor": 2}, fault="write")
    assert read_checkpoint(path) == old
    assert list(tmp_path.glob("state.json.*.tmp")) == []


def test_fault_after_fsync_before_replace_preserves_last_valid(tmp_path):
    path = tmp_path / "state.json"
    old = {"cursor": 1}
    write_checkpoint(path, old)
    with pytest.raises(RuntimeError, match="SIMULATED_AFTER_TEMP_FSYNC_BEFORE_REPLACE"):
        write_checkpoint(path, {"cursor": 2}, fault="replace")
    assert read_checkpoint(path) == old
    assert list(tmp_path.glob("state.json.*.tmp")) == []


def test_corrupt_committed_checkpoint_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, {"cursor": 1})
    envelope = json.loads(path.read_text())
    envelope["payload"]["cursor"] = 2
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="integrity mismatch"):
        read_checkpoint(path)


def test_provenance_replay_commit_does_not_accept_corrupt_current_file(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, {"cursor": 1})
    provenance = checkpoint_envelope(path)
    path.write_text('{"schema":"HTF_176_COMBINED_CHECKPOINT_V2"}')
    with pytest.raises(ValueError):
        read_checkpoint(path)
    recovery = tmp_path / "recovery.json"
    write_checkpoint(recovery, provenance["payload"], provenance_envelope=provenance)
    assert read_checkpoint(recovery) == {"cursor": 1}
    assert checkpoint_envelope(recovery)["sha256"] == provenance["sha256"]


def test_checkpoint_rejects_unknown_schema(tmp_path):
    path = tmp_path / "state.json"
    write_checkpoint(path, {"cursor": 1})
    envelope = json.loads(path.read_text())
    envelope["schema"] = "UNKNOWN"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="schema mismatch"):
        read_checkpoint(path)



def test_advance_failure_preserves_entire_checkpoint():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(
        open_ms=0,
        level_contacts={"L": (100.0, 0, True)},
    )

    before = json.dumps(state.checkpoint(), sort_keys=True)

    with pytest.raises(ValueError, match="not available"):
        state.advance(
            open_ms=STEP,
            level_contacts={
                "L": (100.0, 0, False),
                "INVALID": (101.0, 2 * STEP, True),
            },
            eligibility_loss={"L"},
        )

    after = json.dumps(state.checkpoint(), sort_keys=True)
    assert after == before

    with pytest.raises(ValueError, match="confirmation without pending"):
        state.advance(
            open_ms=STEP,
            level_contacts={"L": (100.0, 0, False)},
            confirmations=["UNKNOWN"],
        )

    assert json.dumps(state.checkpoint(), sort_keys=True) == before

    state.advance(
        open_ms=STEP,
        level_contacts={"L": (100.0, 0, False)},
    )
    assert state.last_close_ms == 2 * STEP
