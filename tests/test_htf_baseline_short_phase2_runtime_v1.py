"""Issue #176 Phase-2 mandatory test matrix (9): runtime start-stop, heartbeat,
kill-recovery, fail-closed startup, resume replay with duplicate suppression."""
from __future__ import annotations

import json

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_episode_v1 import Phase2Episodes
from app.research.htf_baseline_short_phase2_freeze_v1 import Phase2FreezeBoundary
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    EXIT_NOT_RUNNABLE,
    EXIT_OK,
    EXIT_STATE_ERROR,
    CheckpointBoundaryError,
    FileCandleSource,
    InMemoryWriter,
    NotRunnableError,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
    main,
)
from app.research.htf_baseline_short_phase2_state_store_v1 import (
    LEASE_SUFFIX,
    WriterLease,
    read_checkpoint,
    write_checkpoint,
)
import app.research.htf_keylevel_point_b as point_b

BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
FREEZE = "2025-01-01T00:00:00Z"  # before the synthetic data -> events post-freeze


def make_csv_candles() -> list[Candle]:
    """32 hours of 5m bars whose aggregated 1H highs contain one 4/4 swing (h=20)
    with two prior touches and post-availability contact bars."""
    highs = [100.0 + (i % 3) * 0.1 for i in range(32)]
    for i in (6, 12):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    highs[20] = 110.0  # swing, confirmed at hour 24
    for i in (25, 26, 27, 28):
        highs[i] = 110.0  # post-availability contact bars
    candles: list[Candle] = []
    for hour_index, high in enumerate(highs):
        for bar_index in range(12):
            ts = HOUR0 + hour_index * HOUR + bar_index * BAR
            touch_hour = high >= 109.5
            price_high = high
            candles.append(
                Candle(ts, 100.0, price_high, 99.0, 100.0 if not touch_hour else 109.5, 1.0)
            )
    return candles


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    candles = make_csv_candles()
    with (data_dir / "BTCUSDT_5m.csv").open("w", encoding="utf-8", newline="") as handle:
        handle.write("timestamp,open,high,low,close,volume\n")
        for c in candles:
            handle.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
    # frozen detector stub: always confirm at the latest closed bar
    monkeypatch.setattr(
        point_b, "_find_reaction_end",
        lambda **kwargs: (len(kwargs["closed"]) - 1, 0),
    )
    boundary = build_boundary(
        experiment_id="HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint_hex="0" * 64,
    )
    config = RunnerConfig(
        symbols=("BTCUSDT",),
        checkpoint_path=str(tmp_path / "state.json"),
        warmup_bars=300,
    )
    writer = InMemoryWriter()
    source = FileCandleSource(data_dir)
    return tmp_path, config, boundary, source, writer, candles


def test_runner_start_stop_processes_and_checkpoints(env):
    tmp_path, config, boundary, source, writer, candles = env
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert runner.run_once() == EXIT_OK
    assert runner.metrics.bars_processed == len(candles)
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / f"state.json{LEASE_SUFFIX}").exists()  # lease released
    assert runner.metrics.checkpoint_writes == 1


def test_warmup_origin_events_never_eligible(env):
    tmp_path, config, boundary, source, writer, candles = env
    config.warmup_bars = 10_000  # everything is warmup
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    runner.run_once()
    assert writer.rows == []  # nothing eligible
    state = read_checkpoint(tmp_path / "state.json")
    episodes = state["symbols"]["BTCUSDT"]["episodes"]
    assert episodes, "expected warmup episodes to exist and be flagged"
    assert all(record["warmup_origin"] for record in episodes.values())


def test_restart_replay_suppresses_duplicates_at_least_once_idempotent(env, monkeypatch):
    tmp_path, config, boundary, source, writer, candles = env
    # simulate a crash AFTER emission but BEFORE the checkpoint was persisted
    monkeypatch.setattr(Phase2ObserverRunner, "save_checkpoint", lambda self: None)
    crashed = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert crashed.run_once() == EXIT_OK
    rows_after_crash = len(writer.rows)
    assert rows_after_crash >= 1
    assert not (tmp_path / "state.json").exists()  # nothing was persisted
    monkeypatch.undo()
    # restart replays the full series from the durable checkpoint (= none)
    resumed = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert resumed.run_once() == EXIT_OK
    assert len(writer.rows) == rows_after_crash  # idempotent: no duplicate rows
    assert resumed.metrics.duplicate_suppressions >= 1


def test_normal_restart_resumes_from_cursor_without_reemission(env):
    tmp_path, config, boundary, source, writer, candles = env
    first = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    first.run_once()
    rows = len(writer.rows)
    second = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert second.run_once() == EXIT_OK
    assert len(writer.rows) == rows  # cursor resume: no reprocessing, no re-emission
    assert second.metrics.bars_processed == 0


def test_second_live_runner_refused_fail_closed(env):
    tmp_path, config, boundary, source, writer, candles = env
    holder = WriterLease(config.checkpoint_path, instance_id="other-run", stale_after_s=600)
    holder.acquire()
    try:
        runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
        assert runner.run_once() == EXIT_STATE_ERROR
        assert writer.rows == []
    finally:
        holder.release()


def test_crashed_writer_recovered_only_with_explicit_takeover(env):
    tmp_path, config, boundary, source, writer, candles = env
    (tmp_path / f"state.json{LEASE_SUFFIX}").write_text(
        json.dumps({"pid": 999_999_999, "instance_id": "crashed", "heartbeat_at_ms": 0}),
        encoding="utf-8",
    )
    strict = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert strict.run_once() == EXIT_STATE_ERROR  # fail closed by default
    config.allow_stale_takeover = True
    recovered = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert recovered.run_once() == EXIT_OK
    assert not (tmp_path / f"state.json{LEASE_SUFFIX}").exists()


def test_checkpoint_from_other_freeze_boundary_rejected(env):
    tmp_path, config, boundary, source, writer, candles = env
    write_checkpoint(
        config.checkpoint_path,
        {
            "experiment_id": config.experiment_id,
            "boundary_freeze_ts_ms": boundary.freeze_ts_ms + 86_400_000,
            "protocol_fingerprint": "0" * 64,
            "emitted": [],
            "symbols": {},
        },
    )
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert runner.run_once() == EXIT_STATE_ERROR
    assert writer.rows == []


def test_corrupt_checkpoint_rejected_no_silent_resume(env):
    tmp_path, config, boundary, source, writer, candles = env
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert runner.run_once() == EXIT_STATE_ERROR
    assert writer.rows == []


def test_sigterm_mid_cycle_stops_cleanly(env):
    tmp_path, config, boundary, source, writer, candles = env
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    runner.install_signal_handlers()
    runner.request_stop()
    assert runner.run_once() == EXIT_OK  # finishes current work, no partial state
    assert runner.metrics.bars_processed == 0  # stopped before processing
    assert not (tmp_path / f"state.json{LEASE_SUFFIX}").exists()


def test_fail_closed_boundary_construction():
    with pytest.raises(NotRunnableError):
        build_boundary(
            experiment_id="HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
            protocol_freeze_ts=FREEZE,
            registry_freeze_ts="2026-10-02T00:00:00Z",
            database_freeze_ts=FREEZE,
            protocol_fingerprint_hex="0" * 64,
        )


def test_main_without_freeze_is_fail_closed_no_observations(tmp_path, monkeypatch):
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    (data_dir / "BTCUSDT_5m.csv").write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    code = main(["--data-dir", str(data_dir), "--checkpoint", str(tmp_path / "state.json")])
    assert code == EXIT_NOT_RUNNABLE
    assert not (tmp_path / "state.json").exists()


def test_memory_writer_is_idempotent_and_freeze_gated():
    writer = InMemoryWriter()
    boundary = build_boundary(
        experiment_id="HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint_hex="0" * 64,
    )
    event = {
        "source_event_id": "e1:FIRST_CONFIRM",
        "episode_id": "e1",
        "level_id": "l1",
        "symbol": "BTCUSDT",
        "known_at_ms": 1_760_000_000_000,
        "first_touch_open_ms": 1_759_999_700_000,
        "warmup_origin": False,
    }
    result1 = writer.emit([event], boundary)
    result2 = writer.emit([event], boundary)
    assert result1.inserted == 1
    assert result2.deduplicated == 1
    pre_freeze = dict(event, source_event_id="e2:FIRST_CONFIRM", known_at_ms=1_700_000_000_000, first_touch_open_ms=1_699_999_700_000)
    result3 = writer.emit([pre_freeze], boundary)
    assert result3.rejected == 1
    assert len(writer.rows) == 1
