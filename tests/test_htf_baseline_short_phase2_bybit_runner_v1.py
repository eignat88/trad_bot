"""Issue #181 mandatory test matrix (integration): Bybit live source + Phase-2 runner.

Covers, per the issue:
  6. rolling-window eviction never changes episode identity;
  7. restart and overlap never produce a duplicate FIRST_CONFIRM;
  8. warmup and pre-freeze events never become clean OOS;
  5. API errors / rate limits create no signal and block the symbol;
  9. the Bybit source does not change frozen CSV replay results on identical data;
  plus: controlled legacy-cursor migration, checkpoint/restart equivalence and
  the causal ASOF audit (no decision ever looks past the verified ASOF).
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BybitPhase2CandleSource,
)
from app.research.htf_baseline_short_phase2_freeze_v1 import Phase2FreezeBoundary
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    CURSOR_SCHEMA_V1_INDEX,
    CURSOR_SCHEMA_V2,
    EXIT_OK,
    EXIT_STATE_ERROR,
    CheckpointCursorError,
    FileCandleSource,
    InMemoryWriter,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
    migrate_legacy_index_cursors,
    parse_timestamp_cursors,
)
from app.research.htf_baseline_short_phase2_state_store_v1 import (
    read_checkpoint,
    write_checkpoint,
)
import app.research.htf_keylevel_point_b as point_b

BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
FREEZE = "2025-01-01T00:00:00Z"  # before the synthetic data -> events post-freeze
LATE_FREEZE = "2030-01-01T00:00:00Z"  # after the synthetic data -> everything pre-freeze
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


def make_candles() -> list[Candle]:
    """68 hours of 5m bars with exactly one causal 1H resistance level.

    Flat 100.0 highs everywhere (a flat series forms NO strict 4/4 swings), so
    the only structure is deliberate:
      * hours 44, 52  -> the two prior touches required by KEY_LEVEL_MIN_TOUCHES;
      * hour 56       -> the 110.0 swing (confirmed once hour 60 closes);
      * hours 61-62,
        65-66       -> two separate post-availability contact runs (2 episodes).

    Everything sits after hour 40: any rolling-window shift of up to ~15h keeps
    the same causal level universe and therefore the same episodes.
    """
    highs = [100.0] * 68
    for i in (44, 52):
        highs[i] = 109.9  # prior touches of the 110.0 level (touch_count = 2)
    highs[56] = 110.0  # strict 4/4 swing, available from hour 61
    for i in (61, 62, 65, 66):
        highs[i] = 110.0  # two post-availability contact runs
    candles: list[Candle] = []
    for hour_index, high in enumerate(highs):
        for bar_index in range(12):
            ts = HOUR0 + hour_index * HOUR + bar_index * BAR
            touch_hour = high >= 109.5
            candles.append(
                Candle(ts, 100.0, high, 99.0, 100.0 if not touch_hour else 109.5, 1.0)
            )
    return candles


def row_of(candle: Candle) -> list[str]:
    return [
        str(int(candle.timestamp)),
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
    ]


class FixedCandleClient:
    """Read-only Bybit v5 fake serving one fixed closed-5m dataset."""

    def __init__(self, candles, *, server_time_ms=None, fail_with=None):
        self.rows = {int(c.timestamp): row_of(c) for c in candles}
        self.server_time_ms = int(
            server_time_ms if server_time_ms is not None else candles[-1].timestamp + BAR
        )
        self.fail_with = fail_with
        self.calls: list[tuple] = []

    def _public_get(self, endpoint: str, **params):
        self.calls.append((endpoint, params))
        if self.fail_with is not None:
            raise self.fail_with
        if endpoint == "/v5/market/time":
            return {"retCode": 0, "result": {}, "time": self.server_time_ms}
        start, end = int(params["start"]), int(params["end"])
        rows = [self.rows[o] for o in sorted(self.rows) if start <= o <= end]
        rows.reverse()
        return {"retCode": 0, "result": {"list": rows}, "time": self.server_time_ms}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    candles = make_candles()
    with (data_dir / "BTCUSDT_5m.csv").open("w", encoding="utf-8", newline="") as handle:
        handle.write("timestamp,open,high,low,close,volume\n")
        for c in candles:
            handle.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
    # frozen detector stub: always confirm at the latest closed bar
    monkeypatch.setattr(
        point_b, "_find_reaction_end",
        lambda **kwargs: (len(kwargs["closed"]) - 1, 0),
    )
    return tmp_path, data_dir, candles


def make_config(tmp_path, name="state.json", warmup_bars=300):
    return RunnerConfig(
        experiment_id=EXPERIMENT_ID,
        symbols=("BTCUSDT",),
        checkpoint_path=str(tmp_path / name),
        warmup_bars=warmup_bars,
    )


def make_boundary(freeze_ts=FREEZE):
    return build_boundary(
        experiment_id=EXPERIMENT_ID,
        protocol_freeze_ts=freeze_ts,
        registry_freeze_ts=freeze_ts,
        database_freeze_ts=freeze_ts,
        protocol_fingerprint_hex="0" * 64,
    )


def make_bybit_source(candles, **kwargs):
    kwargs.setdefault("evaluation_asof_ms", candles[-1].timestamp + BAR)
    kwargs.setdefault("min_history_bars", len(candles))
    kwargs.setdefault("page_limit", 200)
    kwargs.setdefault("max_pages", 5)
    kwargs.setdefault("sleep", lambda _s: None)
    return BybitPhase2CandleSource(FixedCandleClient(candles, server_time_ms=kwargs["evaluation_asof_ms"]), **kwargs)


def event_signature(row: dict) -> tuple:
    return (
        row["source_event_id"],
        row["episode_id"],
        row["level_id"],
        row["symbol"],
        row["direction"],
        row["first_touch_open_ms"],
        row["level_available_ms"],
        row["level_price_at_touch"],
        row["known_at_ms"],
        row["warmup_origin"],
    )


def strip_provenance(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "provenance"} for row in rows]


def strip_any_provenance(node):
    """Drop recorded provenance (source NAME is allowed to differ)."""
    if isinstance(node, dict):
        return {k: strip_any_provenance(v) for k, v in node.items() if k != "provenance"}
    if isinstance(node, list):
        return [strip_any_provenance(v) for v in node]
    return node


def first_diff(left, right, path: str = "$"):
    """First differing JSON path (or None) — precise diagnostics on mismatch."""
    if type(left) is not type(right):
        return f"{path}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                return f"{path}.{key}: missing on the left"
            if key not in right:
                return f"{path}.{key}: missing on the right"
            diff = first_diff(left[key], right[key], f"{path}.{key}")
            if diff:
                return diff
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            diff = first_diff(a, b, f"{path}[{index}]")
            if diff:
                return diff
        return None
    if left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


# ------------------------------------------------- 9. CSV replay parity


def test_bybit_source_does_not_change_frozen_csv_replay(env):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    csv_writer = InMemoryWriter()
    csv_runner = Phase2ObserverRunner(
        make_config(tmp_path, "csv_state.json"),
        boundary=boundary,
        source=FileCandleSource(data_dir),
        writer=csv_writer,
    )
    assert csv_runner.run_once() == EXIT_OK
    assert csv_writer.rows  # the fixture produces observations

    bybit_writer = InMemoryWriter()
    bybit_runner = Phase2ObserverRunner(
        make_config(tmp_path, "bybit_state.json"),
        boundary=boundary,
        source=make_bybit_source(candles),
        writer=bybit_writer,
    )
    assert bybit_runner.run_once() == EXIT_OK

    assert [event_signature(r) for r in bybit_writer.rows] == [
        event_signature(r) for r in csv_writer.rows
    ]
    assert strip_provenance(bybit_writer.rows) == strip_provenance(csv_writer.rows)

    csv_state = read_checkpoint(str(tmp_path / "csv_state.json"))
    bybit_state = read_checkpoint(str(tmp_path / "bybit_state.json"))
    for key in ("symbols", "emitted", "known_levels", "cursors", "warmup_done"):
        assert first_diff(
            strip_any_provenance(bybit_state[key]), strip_any_provenance(csv_state[key])
        ) is None, key
    # only the recorded provenance may differ
    assert csv_state["provenance"]["candle_source"] == "FileCandleSource"
    assert bybit_state["provenance"]["candle_source"] == "BybitPhase2CandleSource"


# --------------------------------- 6. rolling-window eviction keeps identity


def test_rolling_window_eviction_preserves_episode_identity(env):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    # reference: everything in one window
    ref_writer = InMemoryWriter()
    ref_runner = Phase2ObserverRunner(
        make_config(tmp_path, "ref_state.json"),
        boundary=boundary,
        source=FileCandleSource(data_dir),
        writer=ref_writer,
    )
    ref_runner.run_once()
    assert ref_writer.rows  # the reference really observed something

    # eviction: cycle 1 sees only the first 240 bars, cycle 2 sees the window
    # SHIFTED forward by 120 bars (the 120 leading bars are gone)
    evict_writer = InMemoryWriter()
    config = make_config(tmp_path, "evict_state.json")
    runner = Phase2ObserverRunner(
        config, boundary=boundary, source=FileCandleSource(data_dir), writer=evict_writer
    )
    runner.process_symbol_bars("BTCUSDT", candles[:240])
    runner.save_checkpoint()

    shifted_source = make_bybit_source(candles[120:])
    runner2 = Phase2ObserverRunner(
        config, boundary=boundary, source=shifted_source, writer=evict_writer
    )
    assert runner2.run_once() == EXIT_OK  # loads the checkpoint, resumes by timestamp

    assert len(shifted_source.load("BTCUSDT")) == 696  # window really did shift
    assert runner2.metrics.bars_processed == 816 - 240  # only unseen bars were visited

    ref_ids = sorted(r["source_event_id"] for r in ref_writer.rows)
    evict_ids = sorted(r["source_event_id"] for r in evict_writer.rows)
    assert evict_ids == ref_ids  # identity and content unchanged by eviction
    assert len(evict_ids) == len(set(evict_ids))  # no duplicate FIRST_CONFIRM

    ref_state = read_checkpoint(str(tmp_path / "ref_state.json"))
    evict_state = read_checkpoint(str(tmp_path / "evict_state.json"))
    assert first_diff(evict_state["symbols"], ref_state["symbols"]) is None  # same episodes, same ids
    assert first_diff(evict_state["emitted"], ref_state["emitted"]) is None
    assert first_diff(evict_state["known_levels"], ref_state["known_levels"]) is None


# --------------------------- 7. restart + overlap never duplicate FIRST_CONFIRM


def test_restart_and_overlap_never_duplicate_first_confirm(env, monkeypatch):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    writer = InMemoryWriter()
    config = make_config(tmp_path, "state.json", warmup_bars=0)
    first = Phase2ObserverRunner(
        config,
        boundary=boundary,
        source=make_bybit_source(candles),
        writer=writer,
    )
    monkeypatch.setattr(first, "save_checkpoint", lambda: None)  # crash before persist
    assert first.run_once() == EXIT_OK
    assert writer.rows  # events were observed before the crash

    # restart with an OVERLAPPING window: bars 60.. are delivered again
    resumed = Phase2ObserverRunner(
        config,
        boundary=boundary,
        source=make_bybit_source(candles[60:]),
        writer=writer,
    )
    assert resumed.run_once() == EXIT_OK

    ids = [r["source_event_id"] for r in writer.rows]
    assert len(ids) == len(set(ids))  # no duplicate FIRST_CONFIRM after restart+overlap
    assert resumed.metrics.duplicate_suppressions >= 1  # at-least-once, idempotent writes

    clean_writer = InMemoryWriter()
    clean = Phase2ObserverRunner(
        make_config(tmp_path, "clean.json", warmup_bars=0),
        boundary=boundary,
        source=make_bybit_source(candles),
        writer=clean_writer,
    )
    assert clean.run_once() == EXIT_OK
    assert sorted(ids) == sorted(r["source_event_id"] for r in clean_writer.rows)

    # and with a persisted checkpoint the overlap is simply not reprocessed
    writer2 = InMemoryWriter()
    config2 = make_config(tmp_path, "state2.json")
    clean2 = Phase2ObserverRunner(
        config2, boundary=boundary, source=make_bybit_source(candles), writer=writer2
    )
    assert clean2.run_once() == EXIT_OK
    clean_events = len(writer2.rows)

    overlap_writer = InMemoryWriter()
    overlap = Phase2ObserverRunner(
        config2,
        boundary=boundary,
        source=make_bybit_source(candles[60:]),
        writer=overlap_writer,
    )
    assert overlap.run_once() == EXIT_OK
    assert overlap.metrics.bars_processed == 0
    assert overlap_writer.rows == []
    assert clean_events > 0


# --------------------------- 8. warmup / pre-freeze are never clean OOS


def test_warmup_and_pre_freeze_events_never_become_clean_oos(env):
    tmp_path, data_dir, candles = env

    # warmup: every event is warmup-origin -> rejected, nothing reaches the writer
    warm_writer = InMemoryWriter()
    warm = Phase2ObserverRunner(
        make_config(tmp_path, "warm_state.json", warmup_bars=10_000),
        boundary=make_boundary(),
        source=make_bybit_source(candles),
        writer=warm_writer,
    )
    assert warm.run_once() == EXIT_OK
    assert warm_writer.rows == []
    assert warm.metrics.emissions_rejected >= 1

    # pre-freeze: nothing at or before the freeze is ever eligible
    freeze_writer = InMemoryWriter()
    frozen = Phase2ObserverRunner(
        make_config(tmp_path, "freeze_state.json"),
        boundary=make_boundary(LATE_FREEZE),
        source=make_bybit_source(candles),
        writer=freeze_writer,
    )
    assert frozen.run_once() == EXIT_OK
    assert freeze_writer.rows == []
    assert frozen.metrics.emissions_rejected >= 1


# ------------------------------------- 5. API errors / rate limits -> no signal


def test_api_error_rate_limit_blocks_symbol_and_creates_no_signal(env):
    tmp_path, data_dir, candles = env

    writer = InMemoryWriter()
    runner = Phase2ObserverRunner(
        make_config(tmp_path, "state.json"),
        boundary=make_boundary(),
        source=BybitPhase2CandleSource(
            FixedCandleClient(candles, fail_with=RuntimeError("Too many visits")),
            evaluation_asof_ms=candles[-1].timestamp + BAR,
            min_history_bars=len(candles),
            sleep=lambda _s: None,
        ),
        writer=writer,
    )
    code = runner.run_once()

    assert code == EXIT_OK  # one blocked symbol must not take the loop down
    assert runner.metrics.source_errors == 1
    assert runner.metrics.symbols_blocked == 1
    assert writer.rows == []  # no signal is ever created
    state = read_checkpoint(str(tmp_path / "state.json"))
    assert state["cursors"] == {}  # nothing was consumed
    assert any(
        report.get("action") == "SOURCE_BLOCKED" for report in state["coverage_reports"]
    )


# ------------------------------- controlled legacy index-cursor migration


def test_legacy_index_cursor_migrates_to_timestamp_cursor(env):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    writer = InMemoryWriter()
    config = make_config(tmp_path, "state.json")
    first = Phase2ObserverRunner(
        config, boundary=boundary, source=make_bybit_source(candles), writer=writer
    )
    assert first.run_once() == EXIT_OK

    # rewrite the checkpoint as a LEGACY V1 checkpoint (index cursors)
    payload = read_checkpoint(str(tmp_path / "state.json"))
    legacy_index = 240
    payload.pop("cursor_schema", None)
    payload["cursor_migrations"] = []
    payload["cursors"] = {"BTCUSDT": legacy_index}
    write_checkpoint(str(tmp_path / "state.json"), payload)

    resumed_writer = InMemoryWriter()
    resumed = Phase2ObserverRunner(
        make_config(tmp_path, "state.json"),
        boundary=boundary,
        source=make_bybit_source(candles[60:]),
        writer=resumed_writer,
    )
    assert resumed.run_once() == EXIT_OK

    # controlled migration: the index cursor became a stable timestamp cursor
    assert resumed.metrics.cursor_migrations == 1
    migrated = read_checkpoint(str(tmp_path / "state.json"))
    assert migrated["cursor_schema"] == CURSOR_SCHEMA_V2
    assert migrated["cursors"]["BTCUSDT"]["last_processed_open_ms"] == (
        payload["symbols"]["BTCUSDT"]["last_close_ms"] - BAR
    )
    audit = migrated["cursor_migrations"]
    assert audit and audit[-1]["from_schema"] == CURSOR_SCHEMA_V1_INDEX
    assert audit[-1]["legacy_index"] == legacy_index
    # restart equivalence: the overlap is not reprocessed and nothing re-emits
    assert resumed.metrics.bars_processed == 0
    assert resumed_writer.rows == []


def test_unmappable_legacy_cursor_is_rejected_fail_closed():
    with pytest.raises(CheckpointCursorError):
        migrate_legacy_index_cursors({"BTCUSDT": 12}, {"BTCUSDT": {"last_close_ms": None}})
    with pytest.raises(CheckpointCursorError):
        migrate_legacy_index_cursors({"BTCUSDT": -1}, {})
    with pytest.raises(CheckpointCursorError):
        parse_timestamp_cursors({"BTCUSDT": {"last_processed_open_ms": 1_000}})  # unaligned
    with pytest.raises(CheckpointCursorError):
        parse_timestamp_cursors({"BTCUSDT": 5})  # V1 shape under V2 schema


def test_checkpoint_with_unknown_cursor_schema_is_rejected(env):
    tmp_path, data_dir, candles = env
    payload = {
        "experiment_id": EXPERIMENT_ID,
        "boundary_freeze_ts_ms": Phase2FreezeBoundary.create(
            experiment_id=EXPERIMENT_ID,
            protocol_freeze_ts=FREEZE,
            registry_freeze_ts=FREEZE,
            database_freeze_ts=FREEZE,
            protocol_fingerprint="0" * 64,
        ).freeze_ts_ms,
        "protocol_fingerprint": "0" * 64,
        "cursor_schema": "HTF_P2_CURSOR_V99_UNKNOWN",
        "cursors": {},
        "symbols": {},
    }
    write_checkpoint(str(tmp_path / "state.json"), payload)
    runner = Phase2ObserverRunner(
        make_config(tmp_path, "state.json"),
        boundary=make_boundary(),
        source=make_bybit_source(candles),
        writer=InMemoryWriter(),
    )
    assert runner.run_once() == EXIT_STATE_ERROR


# ----------------------------------- checkpoint/restart equivalence + ASOF audit


def test_checkpoint_restart_equivalence(env):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    clean_writer = InMemoryWriter()
    clean = Phase2ObserverRunner(
        make_config(tmp_path, "clean_state.json"),
        boundary=boundary,
        source=FileCandleSource(data_dir),
        writer=clean_writer,
    )
    assert clean.run_once() == EXIT_OK
    assert clean_writer.rows

    # interrupted cycle: process half, persist the checkpoint, then restart
    crash_writer = InMemoryWriter()
    config = make_config(tmp_path, "crash_state.json")
    crashed = Phase2ObserverRunner(
        config, boundary=boundary, source=FileCandleSource(data_dir), writer=crash_writer
    )
    crashed.process_symbol_bars("BTCUSDT", candles[:240])
    crashed.save_checkpoint()

    restarted = Phase2ObserverRunner(
        config,
        boundary=boundary,
        source=make_bybit_source(candles[60:]),  # different source, overlapping window
        writer=crash_writer,
    )
    assert restarted.run_once() == EXIT_OK

    clean_ids = sorted(r["source_event_id"] for r in clean_writer.rows)
    crash_ids = sorted(r["source_event_id"] for r in crash_writer.rows)
    assert crash_ids == clean_ids
    assert len(crash_ids) == len(set(crash_ids))

    clean_state = read_checkpoint(str(tmp_path / "clean_state.json"))
    crash_state = read_checkpoint(str(tmp_path / "crash_state.json"))
    for key in ("symbols", "emitted", "known_levels"):
        assert first_diff(
            strip_any_provenance(crash_state[key]), strip_any_provenance(clean_state[key])
        ) is None, key


def test_causal_asof_audit_no_decision_past_the_verified_asof(env):
    tmp_path, data_dir, candles = env
    boundary = make_boundary()
    evaluation_asof_ms = candles[-1].timestamp + BAR

    writer = InMemoryWriter()
    source = make_bybit_source(candles, evaluation_asof_ms=evaluation_asof_ms)
    runner = Phase2ObserverRunner(
        make_config(tmp_path, "state.json"),
        boundary=boundary,
        source=source,
        writer=writer,
    )
    assert runner.run_once() == EXIT_OK

    assert source.last_evaluation_asof_ms == evaluation_asof_ms
    loaded = source.load("BTCUSDT")
    assert all(int(c.timestamp) + BAR <= evaluation_asof_ms for c in loaded)
    assert runner.metrics.unclosed_candles_excluded == 0  # nothing forming was consumed

    assert writer.rows  # the audit needs at least one observed event
    for row in writer.rows:
        decision_asof = row["known_at_ms"]
        assert decision_asof % BAR == 0
        assert decision_asof == row["observed_at_ms"]  # decided at the confirming close
        assert decision_asof <= evaluation_asof_ms  # never looks past the ASOF
        assert row["first_touch_open_ms"] < decision_asof
        assert row["level_available_ms"] <= row["first_touch_open_ms"]
        assert row["first_touch_open_ms"] % BAR == 0
        assert row["provenance"]["candle_source"] == "BybitPhase2CandleSource"


def test_block_symbol_without_touching_other_symbols(env):
    """One broken symbol must not block, contaminate or advance another one."""
    tmp_path, data_dir, candles = env
    boundary = make_boundary()

    class MixedClient(FixedCandleClient):
        def _public_get(self, endpoint, **params):
            if params.get("symbol") == "BADUSDT":
                raise RuntimeError("connection reset")
            return super()._public_get(endpoint, **params)

    client = MixedClient(candles, server_time_ms=candles[-1].timestamp + BAR)
    source = BybitPhase2CandleSource(
        client,
        evaluation_asof_ms=candles[-1].timestamp + BAR,
        min_history_bars=len(candles),
        page_limit=200,
        max_pages=5,
        sleep=lambda _s: None,
    )
    config = RunnerConfig(
        experiment_id=EXPERIMENT_ID,
        symbols=("BADUSDT", "BTCUSDT"),
        checkpoint_path=str(tmp_path / "state.json"),
        warmup_bars=300,
    )
    writer = InMemoryWriter()
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert runner.run_once() == EXIT_OK

    state = read_checkpoint(str(tmp_path / "state.json"))
    assert runner.metrics.symbols_blocked == 1
    assert writer.rows  # BTCUSDT still produced its observations
    assert all(row["symbol"] == "BTCUSDT" for row in writer.rows)
    assert "BTCUSDT" in state["cursors"]
    assert "BADUSDT" not in state["cursors"]


def test_csv_source_errors_also_block_the_symbol(env):
    tmp_path, data_dir, candles = env
    writer = InMemoryWriter()
    runner = Phase2ObserverRunner(
        make_config(tmp_path, "state.json"),
        boundary=make_boundary(),
        source=FileCandleSource(tmp_path / "does-not-exist"),
        writer=writer,
    )
    assert runner.run_once() == EXIT_OK
    assert runner.metrics.symbols_blocked == 1
    assert writer.rows == []
