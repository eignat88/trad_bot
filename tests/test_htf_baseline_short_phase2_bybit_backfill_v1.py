"""Issue #181 review FIX-1 regression matrix: durable cursor-aware backfill.

Covers the mandatory review scenarios (backfill / restart):

 1. downtime shorter than the source window  -> plain recovery;
 2. downtime longer than the source window   -> bounded backfill batch;
 3. downtime far beyond one batch            -> sequential multi-batch recovery;
 4. missing 5m candle inside backfill        -> fail-closed;
 5. duplicate / conflicting OHLC in backfill -> fail-closed;
 6. rate limit during backfill               -> cursor never jumps the gap;
 7. restart in the middle of backfill        -> no lost/duplicated events;
 8. overlapping historical ranges           -> no repeated FIRST_CONFIRM;
 9. rolling-window eviction                  -> episode identity preserved;
10. backfill with pre-freeze events          -> never clean OOS;
11. first-touch before freeze, confirm after -> not eligible;
12. insufficient HTF/ATR lookback            -> fail-closed.

All tests are deterministic and never call the Bybit API.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BybitPhase2CandleSource,
    CandleSourceError,
)
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    EXIT_OK,
    InMemoryWriter,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
    read_checkpoint,
)
import app.research.htf_keylevel_point_b as point_b

BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
FREEZE = "2025-01-01T00:00:00Z"  # before the synthetic data -> events post-freeze
LATE_FREEZE = "2030-01-01T00:00:00Z"  # after the synthetic data -> everything pre-freeze
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"

HOURS = 120
SPLIT = 840  # bars processed before the simulated observer downtime (hour 70)
MIN_HISTORY_BARS = 400  # steady window of the simulated source (~33h)
STREADY_START = HOURS * 12 - MIN_HISTORY_BARS  # bar 1040 (hour 86.7)


def make_candles() -> list[Candle]:
    """120 hours of 5m bars; all causal structure sits at hours 60..83.

    * hours 60, 68 -> the two prior touches required by KEY_LEVEL_MIN_TOUCHES;
    * hour 72      -> the 110.0 swing (confirmed once hour 76 closes);
    * hours 78-79,
      82-83        -> two post-availability contact runs (2 episodes), i.e. the
                      observed events land INSIDE the backfill batches.
    Flat 100.0 highs elsewhere form no strict 4/4 swings.
    """
    highs = [100.0] * HOURS
    for i in (60, 68):
        highs[i] = 109.9
    highs[72] = 110.0
    for i in (78, 79, 82, 83):
        highs[i] = 110.0
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


class FakeKlineClient:
    """Read-only Bybit v5 fake with deterministic fault injection."""

    def __init__(
        self,
        candles,
        *,
        server_time_ms=None,
        drop_opens=(),
        duplicate_opens=(),
        conflict_opens=(),
        fail_with=None,
        earliest_open=None,
    ):
        self.rows = {int(c.timestamp): row_of(c) for c in candles}
        self.server_time_ms = int(
            server_time_ms if server_time_ms is not None else candles[-1].timestamp + BAR
        )
        self.drop_opens = set(drop_opens)
        self.duplicate_opens = set(duplicate_opens)
        self.conflict_opens = set(conflict_opens)
        self.fail_with = fail_with
        self.earliest_open = earliest_open
        self.calls: list[tuple] = []

    def _public_get(self, endpoint: str, **params):
        self.calls.append((endpoint, params))
        if self.fail_with is not None:
            raise self.fail_with
        if endpoint == "/v5/market/time":
            return {"retCode": 0, "result": {}, "time": self.server_time_ms}
        start, end = int(params["start"]), int(params["end"])
        low = max(start, int(self.earliest_open)) if self.earliest_open else start
        out: list[list[str]] = []
        for open_ms in sorted(self.rows):
            if open_ms < low or open_ms > end or open_ms in self.drop_opens:
                continue
            out.append(list(self.rows[open_ms]))
            if open_ms in self.duplicate_opens:
                out.append(list(self.rows[open_ms]))
            if open_ms in self.conflict_opens:
                row = list(self.rows[open_ms])
                row[2] = str(float(row[2]) + 1.0)  # high
                row[4] = str(float(row[4]) + 1.0)  # close, still OHLC-coherent
                out.append(row)
        out.reverse()
        return {"retCode": 0, "result": {"list": out}, "time": self.server_time_ms}


@pytest.fixture()
def env(monkeypatch):
    # frozen detector stub: always confirm at the latest closed bar
    monkeypatch.setattr(
        point_b, "_find_reaction_end",
        lambda **kwargs: (len(kwargs["closed"]) - 1, 0),
    )
    return make_candles()


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


def make_source(candles, *, client=None, min_history_bars=MIN_HISTORY_BARS, max_backfill_bars=200, **kwargs):
    asof = candles[-1].timestamp + BAR
    kwargs.setdefault("evaluation_asof_ms", asof)
    kwargs.setdefault("page_limit", 200)
    kwargs.setdefault("max_pages", 8)
    kwargs.setdefault("sleep", lambda _s: None)
    if client is None:
        client = FakeKlineClient(candles, server_time_ms=asof)
    return BybitPhase2CandleSource(
        client, min_history_bars=min_history_bars, max_backfill_bars=max_backfill_bars, **kwargs
    )


def event_key(row: dict) -> tuple:
    return (
        row["source_event_id"],
        row["episode_id"],
        row["first_touch_open_ms"],
        row["known_at_ms"],
    )


def reference_events(candles, boundary):
    """Uninterrupted single-pass processing of the identical historical set."""
    writer = InMemoryWriter()
    runner = Phase2ObserverRunner(
        make_config(__import__("pathlib").Path(__file__).parent, "unused.json"),
        boundary=boundary,
        source=make_source(candles),
        writer=writer,
    )
    runner.process_symbol_bars("BTCUSDT", candles, evaluation_asof_ms=candles[-1].timestamp + BAR)
    return writer.rows


def run_recovery(
    tmp_path,
    candles,
    boundary,
    *,
    split=SPLIT,
    max_backfill_bars=200,
    state_name="state.json",
    writer=None,
    client_factory=None,
    max_cycles=10,
):
    """Simulated downtime: process ``candles[:split]``, then catch up via run_once."""
    writer = writer if writer is not None else InMemoryWriter()
    config = make_config(tmp_path, state_name)
    first_client = client_factory() if client_factory else None
    source = make_source(
        candles, client=first_client, max_backfill_bars=max_backfill_bars
    )
    runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    runner.process_symbol_bars(
        "BTCUSDT", candles[:split], evaluation_asof_ms=candles[-1].timestamp + BAR
    )
    runner.save_checkpoint()
    runners = [runner]
    for _ in range(max_cycles):
        client = client_factory() if client_factory else None
        source = make_source(candles, client=client, max_backfill_bars=max_backfill_bars)
        runner = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
        runners.append(runner)
        before = runner.metrics.bars_processed
        assert runner.run_once() == EXIT_OK
        if runner.metrics.bars_processed == before:
            break
    return writer, runners


# ------------------------------------------------------------------ scenario 1
def test_downtime_shorter_than_window_resumes_normally(env, tmp_path):
    candles = env
    boundary = make_boundary()
    split = 1100  # resume inside the steady window [1040..1439]
    writer, runners = run_recovery(tmp_path, candles, boundary, split=split)

    runner = runners[1]  # the first catch-up cycle
    assert runner.metrics.backfill_batches == 0
    assert runner.metrics.quarantine_bars == 0
    assert runner.source.last_load_metrics["backfill"] is False
    assert runner.metrics.bars_processed == len(candles) - split
    assert [event_key(r) for r in writer.rows] == [
        event_key(r) for r in reference_events(candles, boundary)
    ]


# ------------------------------------------------------------------ scenario 2
def test_downtime_longer_than_window_triggers_bounded_backfill(env, tmp_path):
    candles = env
    boundary = make_boundary()
    writer, runners = run_recovery(tmp_path, candles, boundary, split=SPLIT)

    batch_runner = runners[1]
    metrics = batch_runner.source.last_load_metrics
    assert metrics["backfill"] is True
    assert metrics["window_start_open_ms"] == candles[SPLIT - 399].timestamp  # full lookback
    assert metrics["window_end_open_ms"] == candles[SPLIT + 199].timestamp  # bounded batch
    assert batch_runner.metrics.backfill_batches == 1
    assert batch_runner.metrics.bars_processed == 200
    assert batch_runner.metrics.quarantine_bars == 0
    # the resume bar is inside the delivered window: no RESUME_GAP quarantine
    assert all(r.get("reason") != "RESUME_GAP_SOURCE_WINDOW" for r in batch_runner._coverage_reports)
    assert [event_key(r) for r in writer.rows] == [
        event_key(r) for r in reference_events(candles, boundary)
    ]


# ------------------------------------------------------------------ scenario 3
def test_downtime_far_beyond_one_batch_recovers_sequentially(env, tmp_path):
    candles = env
    boundary = make_boundary()
    writer, runners = run_recovery(
        tmp_path, candles, boundary, split=SPLIT, max_backfill_bars=100
    )

    total_backfill = sum(r.metrics.backfill_batches for r in runners)
    assert total_backfill == 2  # [840..939] then [940..1039], then steady state
    assert sum(r.metrics.bars_processed for r in runners[1:]) == len(candles) - SPLIT
    assert all(r.metrics.quarantine_bars == 0 for r in runners)
    assert [event_key(r) for r in writer.rows] == [
        event_key(r) for r in reference_events(candles, boundary)
    ]


# ------------------------------------------------------------------ scenario 4
def test_missing_candle_inside_backfill_fails_closed(env, tmp_path):
    candles = env
    boundary = make_boundary()
    hole = candles[900].timestamp

    def client_factory():
        return FakeKlineClient(candles, drop_opens={hole})

    writer, runners = run_recovery(
        tmp_path, candles, boundary, split=SPLIT, client_factory=client_factory
    )
    runner = runners[1]
    assert runner.metrics.symbols_blocked == 1
    assert runner.metrics.source_errors == 1
    assert runner.metrics.bars_processed == 0  # no partial processing
    assert writer.rows == []
    state = read_checkpoint(str(tmp_path / "state.json"))
    # the durable cursor stayed at the pre-recovery position: it never jumps the gap
    assert state["cursors"]["BTCUSDT"]["last_processed_open_ms"] == candles[SPLIT - 1].timestamp


# ------------------------------------------------------------------ scenario 5
def test_duplicate_and_conflicting_ohlc_inside_backfill_fail_closed(env, tmp_path):
    candles = env
    boundary = make_boundary()
    target = candles[900].timestamp

    for name, knob in (("duplicate_opens", {target}), ("conflict_opens", {target})):

        def client_factory(knob=knob, name=name):
            return FakeKlineClient(candles, **{name: knob})

        writer, runners = run_recovery(
            tmp_path,
            candles,
            boundary,
            split=SPLIT,
            state_name=f"state_{name}.json",
            client_factory=client_factory,
        )
        runner = runners[1]
        assert runner.metrics.symbols_blocked == 1, name
        assert runner.metrics.bars_processed == 0, name
        assert writer.rows == [], name


# ------------------------------------------------------------------ scenario 6
def test_rate_limit_during_backfill_does_not_advance_cursor(env, tmp_path):
    candles = env
    boundary = make_boundary()

    def client_factory():
        return FakeKlineClient(candles, fail_with=Exception("429 too many requests"))

    writer, runners = run_recovery(
        tmp_path, candles, boundary, split=SPLIT, client_factory=client_factory
    )
    runner = runners[1]
    assert runner.metrics.symbols_blocked == 1
    assert runner.metrics.bars_processed == 0
    assert writer.rows == []
    state = read_checkpoint(str(tmp_path / "state.json"))
    assert state["cursors"]["BTCUSDT"]["last_processed_open_ms"] == candles[SPLIT - 1].timestamp
    assert state["symbols"]["BTCUSDT"]["last_close_ms"] == candles[SPLIT].timestamp


# ------------------------------------------------------------------ scenario 7
def test_restart_mid_backfill_loses_and_duplicates_nothing(env, tmp_path):
    candles = env
    boundary = make_boundary()
    writer = InMemoryWriter()
    config = make_config(tmp_path, "state.json")

    runner = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles, max_backfill_bars=100), writer=writer
    )
    runner.process_symbol_bars(
        "BTCUSDT", candles[:SPLIT], evaluation_asof_ms=candles[-1].timestamp + BAR
    )
    runner.save_checkpoint()
    assert runner.run_once() == EXIT_OK  # backfill batch 1 (840..939)
    assert runner.metrics.backfill_batches == 1

    # crash: only the checkpoint survives
    restarted = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles, max_backfill_bars=100), writer=writer
    )
    while True:
        before = restarted.metrics.bars_processed
        assert restarted.run_once() == EXIT_OK
        if restarted.metrics.bars_processed == before:
            break

    keys = [event_key(row) for row in writer.rows]
    assert keys == [event_key(r) for r in reference_events(candles, boundary)]
    ids = [row["source_event_id"] for row in writer.rows]
    assert len(ids) == len(set(ids))  # no duplicated FIRST_CONFIRM


# ------------------------------------------------------------------ scenario 8
def test_overlapping_historical_ranges_never_duplicate_first_confirm(env, tmp_path, monkeypatch):
    candles = env
    boundary = make_boundary()
    writer = InMemoryWriter()
    config = make_config(tmp_path, "state.json")

    runner = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles, max_backfill_bars=100), writer=writer
    )
    runner.process_symbol_bars(
        "BTCUSDT", candles[:SPLIT], evaluation_asof_ms=candles[-1].timestamp + BAR
    )
    runner.save_checkpoint()
    monkeypatch.setattr(runner, "save_checkpoint", lambda: None)  # crash before persist
    assert runner.run_once() == EXIT_OK  # batch 1 emitted its FIRST_CONFIRM
    assert writer.rows

    # restart from the stale checkpoint: batch 1 overlaps the historical range
    restarted = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles, max_backfill_bars=100), writer=writer
    )
    assert restarted.run_once() == EXIT_OK
    assert restarted.metrics.duplicate_suppressions >= 1  # at-least-once + idempotent writes
    while True:
        before = restarted.metrics.bars_processed
        assert restarted.run_once() == EXIT_OK
        if restarted.metrics.bars_processed == before:
            break

    ids = [row["source_event_id"] for row in writer.rows]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == sorted(
        r["source_event_id"] for r in reference_events(candles, boundary)
    )


# ------------------------------------------------------------------ scenario 9
def test_backfill_and_window_eviction_preserve_episode_identity(env, tmp_path):
    candles = env
    boundary = make_boundary()
    ref = reference_events(candles, boundary)
    assert ref  # the fixture produces observations

    writer, _ = run_recovery(
        tmp_path, candles, boundary, split=SPLIT, max_backfill_bars=100, state_name="evict.json"
    )
    recovered = writer.rows
    assert [event_key(r) for r in recovered] == [event_key(r) for r in ref]
    assert [r["episode_id"] for r in recovered] == [r["episode_id"] for r in ref]
    assert [r["level_id"] for r in recovered] == [r["level_id"] for r in ref]
    assert len({r["episode_id"] for r in recovered}) == len(recovered)


# ----------------------------------------------------------------- scenario 10
def test_backfilled_pre_freeze_events_never_become_clean_oos(env, tmp_path):
    candles = env
    boundary = make_boundary(LATE_FREEZE)
    writer, runners = run_recovery(tmp_path, candles, boundary, split=SPLIT)
    assert writer.rows == []  # nothing pre-freeze is ever observed as clean OOS
    assert sum(r.metrics.emissions_rejected for r in runners) >= 1


# ----------------------------------------------------------------- scenario 11
def test_first_touch_before_freeze_confirm_after_freeze_is_not_eligible(env, tmp_path):
    candles = env
    # freeze lands between the first touch (bar 936 open) and its confirmation
    # (bar 937 close): the event must not become eligible prospective OOS
    freeze_ts = __import__("datetime").datetime.fromtimestamp(
        (candles[937].timestamp) / 1000, __import__("datetime").timezone.utc
    )
    boundary = make_boundary(freeze_ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    writer, runners = run_recovery(tmp_path, candles, boundary, split=SPLIT)

    # the pre-freeze first touch (bar 936) never becomes eligible; every
    # observed event is strictly post-freeze prospective OOS
    assert [r["first_touch_open_ms"] for r in writer.rows] == [
        candles[984].timestamp,
        candles[996].timestamp,
    ]
    assert all(r["first_touch_open_ms"] > candles[937].timestamp for r in writer.rows)
    assert all(r["known_at_ms"] > candles[937].timestamp for r in writer.rows)
    assert sum(r.metrics.emissions_rejected for r in runners) >= 1


# ----------------------------------------------------------------- scenario 12
def test_insufficient_htf_atr_lookback_fails_closed(env, tmp_path):
    candles = env
    boundary = make_boundary()
    writer = InMemoryWriter()
    config = make_config(tmp_path, "state.json")

    runner = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles), writer=writer
    )
    runner.process_symbol_bars(
        "BTCUSDT", candles[:SPLIT], evaluation_asof_ms=candles[-1].timestamp + BAR
    )
    runner.save_checkpoint()

    # the exchange cannot serve the causal lookback before bar 500
    limited = FakeKlineClient(candles, earliest_open=candles[500].timestamp)
    restarted = Phase2ObserverRunner(
        config, boundary=boundary, source=make_source(candles, client=limited), writer=writer
    )
    assert restarted.run_once() == EXIT_OK  # symbol blocked, run continues
    assert restarted.metrics.symbols_blocked == 1
    assert restarted.metrics.bars_processed == 0
    assert writer.rows == []  # no partial processing with a broken lookback
    state = read_checkpoint(str(tmp_path / "state.json"))
    assert state["cursors"]["BTCUSDT"]["last_processed_open_ms"] == candles[SPLIT - 1].timestamp
