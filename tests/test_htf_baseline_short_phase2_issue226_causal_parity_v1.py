"""Issue #226 — causal parity: plain BybitPhase2CandleSource vs cached source.

Both sources feed the REAL Phase-2 observer pipeline (``Phase2ObserverRunner``
with ``Phase2Episodes``, ``build_resistance_universe``, ``aggregate_closed_htf``,
``InMemoryWriter``) over an identical deterministic dataset at identical
evaluation ASOFs and identical observer state. Compared at every phase:

* closed 5m OHLCV windows (baseline window == cache window suffix);
* formed 1H bars + coverage;
* ``known_levels`` and their level ids;
* ``source_event_id`` / ``episode_id`` per event;
* event count and sequence (warmup/restart/recovery boundaries included);
* restart (checkpoint restore) and bounded recovery (>200 missed 5m bars).

Known non-blocking difference (documented, not bent into the test):
``event['provenance']['candle_source']`` names the adapter CLASS and therefore
differs by design (``BybitPhase2CandleSource`` vs
``IncrementalPhase2CandleSource``); every causal content field must match.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BAR_MS,
    BybitPhase2CandleSource,
)
from app.research.htf_baseline_short_phase2_contracts_v1 import (
    aggregate_closed_htf,
)
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    InMemoryWriter,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
)
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    IncrementalPhase2CandleSource,
)

BAR = BAR_MS
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
FREEZE = "2025-01-01T00:00:00Z"        # before the synthetic data
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
N_HOURS = 92

# Deterministic price structure (same semantics as the Issue #181 suite):
# hours 44, 52 -> the two prior touches required by KEY_LEVEL_MIN_TOUCHES;
# hour 56      -> the 110.0 strict swing, level available from hour 61;
# hours 61-62, 65-66, 70-71 -> three post-availability contact runs.
TOUCH_HOURS = {44: 109.9, 52: 109.9, 56: 110.0,
               61: 110.0, 62: 110.0, 65: 110.0, 66: 110.0, 70: 110.0, 71: 110.0}


def row_of(ts: int) -> list[str]:
    hour_index = (ts - HOUR0) // HOUR
    high = TOUCH_HOURS.get(hour_index, 100.0)
    touch = high >= 109.5
    return [str(ts), "100.0", str(high), "99.0",
            "109.5" if touch else "100.0", "1.0"]


class ClockClient:
    """Read-only Bybit v5 fake with a fully controlled exchange clock."""

    def __init__(self):
        self.server_time_ms = 0
        self.calls: list[tuple] = []

    def set_clock(self, now_ms: int) -> None:
        self.server_time_ms = int(now_ms)

    def _public_get(self, endpoint: str, **params):
        self.calls.append((endpoint, params))
        if endpoint == "/v5/market/time":
            return {"retCode": 0, "result": {}, "time": self.server_time_ms}
        start, end = int(params["start"]), int(params["end"])
        rows = [row_of(o) for o in range(start, end + BAR, BAR)]
        rows.reverse()
        return {"retCode": 0, "result": {"list": rows}, "time": self.server_time_ms}


def ts(bar_index: int) -> int:
    return HOUR0 + bar_index * BAR


def make_runner(source, checkpoint_path):
    config = RunnerConfig(
        experiment_id=EXPERIMENT_ID,
        symbols=("BTCUSDT",),
        checkpoint_path=str(checkpoint_path),
        warmup_bars=300,
    )
    boundary = build_boundary(
        experiment_id=EXPERIMENT_ID,
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint_hex="0" * 64,
    )
    return Phase2ObserverRunner(config, boundary=boundary,
                                source=source, writer=InMemoryWriter())


EVENT_FIELDS = ("symbol", "episode_id", "source_event_id", "level_id",
                "first_touch_open_ms", "known_at_ms", "warmup_origin")


def event_view(rows: list[dict]) -> list[tuple]:
    return [tuple(row[f] for f in EVENT_FIELDS) for row in rows]


def drive(tmp_path, monkeypatch, *, stub: bool, recovery_bar: int,
          b_range=range(732, 805), suffix_every: int = 12,
          cache_max_bars: int = 800):
    import app.research.htf_keylevel_point_b as point_b

    if stub:
        monkeypatch.setattr(
            point_b, "_find_reaction_end",
            lambda **kwargs: (len(kwargs["closed"]) - 1, 0),
        )

    client = ClockClient()
    # Pre-create the cache dir with default ACLs: the module intentionally
    # creates it with mode 0700, which the constrained local sandbox cannot
    # write into (see docs/research/...RECOVERY_RUNBOOK sandbox section).
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    base_upstream = BybitPhase2CandleSource(
        client, page_limit=200, max_pages=5, min_history_bars=513,
        retry_backoff_s=0, sleep=lambda _s: None,
    )
    cached_upstream = BybitPhase2CandleSource(
        client, page_limit=200, max_pages=5, min_history_bars=513,
        retry_backoff_s=0, sleep=lambda _s: None,
    )
    cache = IncrementalPhase2CandleSource(
        cached_upstream, tmp_path / "cache",
        max_incremental_bars=200, max_cache_bars=cache_max_bars,
    )

    base_writer, cache_writer = InMemoryWriter(), InMemoryWriter()
    base_runner = make_runner(base_upstream, tmp_path / "base_state.json")
    cache_runner = make_runner(cache, tmp_path / "cache_state.json")
    base_runner.writer, cache_runner.writer = base_writer, cache_writer

    def cycle(runners, bar_index):
        client.set_clock(ts(bar_index) + BAR)
        for runner in runners:
            assert runner.run_once() == 0

    runners = (base_runner, cache_runner)
    cycle(runners, 731)                                   # A: cold load

    for step, bar_index in enumerate(b_range):            # B: incremental bars
        cycle(runners, bar_index)
        if step % suffix_every == 0 or bar_index == b_range[-1]:
            suffix_parity(base_upstream, cache, client)   # window parity

    # C: restart — fresh runners resume from the durable checkpoints
    base_runner2 = make_runner(base_upstream, tmp_path / "base_state.json")
    cache_runner2 = make_runner(cache, tmp_path / "cache_state.json")
    base_runner2.writer, cache_runner2.writer = InMemoryWriter(), InMemoryWriter()
    runners = (base_runner2, cache_runner2)
    for bar_index in range(805, 815):
        cycle(runners, bar_index)

    # D: bounded recovery — 240 missed closed 5m bars at once (> one page)
    cycle(runners, recovery_bar)
    assert cache.last_load_metrics["mode"] == "RECOVERY"
    assert cache.last_load_metrics["pages"] == 2

    client.set_clock(ts(recovery_bar) + BAR)
    base_window = base_upstream.load("BTCUSDT")
    cache_window = cache.load("BTCUSDT")
    return {
        "base_rows": base_writer.rows + base_runner2.writer.rows,
        "cache_rows": cache_writer.rows + cache_runner2.writer.rows,
        "base_writer_old": base_writer,
        "base_known_levels": base_runner2._known_levels,
        "cache_known_levels": cache_runner2._known_levels,
        "base_state": base_runner2._state["BTCUSDT"].checkpoint(),
        "cache_state": cache_runner2._state["BTCUSDT"].checkpoint(),
        "base_cursors": base_runner2._cursors,
        "cache_cursors": cache_runner2._cursors,
        "base_window": base_window,
        "cache_window": cache_window,
        "asof": ts(recovery_bar) + BAR,
        "base_metrics": dict(vars(base_runner2.metrics)),
        "cache_metrics": dict(vars(cache_runner2.metrics)),
    }


def suffix_parity(base_upstream, cache, client):
    base_window = base_upstream.load("BTCUSDT")
    cache_window = cache.load("BTCUSDT")
    assert cache_window[-len(base_window):] == base_window, (
        "WINDOW_PARITY_FAIL: cached suffix differs from baseline window"
    )


def normalize_state(state: dict) -> dict:
    """Strip event provenance — the ONLY tolerated cross-source difference.

    ``provenance.candle_source`` names the adapter class and therefore differs
    by design; everything else in the durable state must match exactly.
    """
    import copy

    state = copy.deepcopy(state)
    for event in state.get("events", []):
        prov = event.pop("provenance", None)
        if prov is not None:
            prov = dict(prov)
            assert prov.pop("candle_source") in (
                "BybitPhase2CandleSource", "IncrementalPhase2CandleSource",
            )
            assert prov == {"htf_source": "AGGREGATED_5M", "coverage_clean": True}
    return state


def assert_parity(result, *, expect_events: bool):
    base_rows, cache_rows = result["base_rows"], result["cache_rows"]

    # 1) closed 5m OHLCV — baseline window is the cache window's suffix
    base_window, cache_window = result["base_window"], result["cache_window"]
    assert cache_window[-len(base_window):] == base_window
    assert len(cache_window) >= 513

    # 2) formed 1H bars + coverage (leading history-start edge may extend
    #    further back in the cache; complete/partial/missing must match)
    asof = result["asof"]
    base_htf, base_cov = aggregate_closed_htf(base_window, asof)
    cache_htf, cache_cov = aggregate_closed_htf(cache_window, asof)
    assert cache_htf[-len(base_htf):] == base_htf
    assert base_cov.partial_hours == cache_cov.partial_hours
    assert base_cov.missing_hours == cache_cov.missing_hours

    # 3) known_levels and their identifiers
    assert result["base_known_levels"] == result["cache_known_levels"]
    assert result["base_state"]["known_level_ids"] == result["cache_state"]["known_level_ids"]

    # 4/5) source_event_id + episode_id: count and sequence
    if expect_events:
        assert len(base_rows) > 0, "scenario must produce events to compare"
    assert event_view(base_rows) == event_view(cache_rows)
    assert len({r["source_event_id"] for r in base_rows}) == len(base_rows)
    assert len({r["source_event_id"] for r in cache_rows}) == len(cache_rows)

    # full event payloads identical except documented provenance class name
    for b_row, c_row in zip(base_rows, cache_rows):
        b_prov, c_prov = b_row.pop("provenance"), c_row.pop("provenance")
        assert b_row == c_row
        assert b_prov["htf_source"] == c_prov["htf_source"] == "AGGREGATED_5M"
        assert b_prov["coverage_clean"] == c_prov["coverage_clean"] is True
        assert b_prov["candle_source"] != c_prov["candle_source"]   # documented

    # 6) observer state: episodes, cursors, emitted, restart continuity
    assert normalize_state(result["base_state"]) == normalize_state(result["cache_state"])
    assert result["base_cursors"] == result["cache_cursors"]


def test_causal_parity_cold_incremental_restart_recovery(tmp_path, monkeypatch):
    result = drive(tmp_path, monkeypatch, stub=True, recovery_bar=1054)
    assert_parity(result, expect_events=True)
    # bounded recovery produced the same causal context as a full refetch
    assert [int(b.timestamp) for b in result["cache_window"][-513:]] == \
        [int(b.timestamp) for b in result["base_window"]]


def test_causal_parity_real_frozen_detector(tmp_path, monkeypatch):
    """Same parity with the real (unstubbed) frozen reaction detector."""
    result = drive(tmp_path, monkeypatch, stub=False, recovery_bar=1054,
                   b_range=range(732, 805, 4), suffix_every=4)
    assert_parity(result, expect_events=False)


def test_cache_window_length_does_not_change_causal_outputs(tmp_path, monkeypatch):
    """A longer cache window must not change identity, dedup or durable state.

    ``max_cache_bars=800`` trims leading history during bounded recovery while
    ``max_cache_bars=2000`` keeps the entire window — the causal outputs must
    be identical: source_event_id / episode_id sequences, known_levels and
    level ids, duplicate suppression, cursors and durable checkpoint state.
    """
    tight = drive(tmp_path / "tight", monkeypatch, stub=True, recovery_bar=1054,
                  cache_max_bars=800)
    wide = drive(tmp_path / "wide", monkeypatch, stub=True, recovery_bar=1054,
                 cache_max_bars=2000)

    # the scenario really exercised different window lengths
    assert len(wide["cache_window"]) > len(tight["cache_window"])

    # event identity + sequence + dedup
    assert event_view(tight["cache_rows"]) == event_view(wide["cache_rows"])
    ids = [r["source_event_id"] for r in tight["cache_rows"]]
    assert ids == [r["source_event_id"] for r in wide["cache_rows"]]
    assert len(set(ids)) == len(ids)
    assert [r["episode_id"] for r in tight["cache_rows"]] == \
        [r["episode_id"] for r in wide["cache_rows"]]

    # known_levels + level ids, cursors, durable state
    assert tight["cache_known_levels"] == wide["cache_known_levels"]
    assert tight["cache_cursors"] == wide["cache_cursors"]
    assert normalize_state(tight["cache_state"]) == normalize_state(wide["cache_state"])

    # duplicate suppression independent of window length and of the source
    assert tight["cache_metrics"]["duplicate_suppressions"] == \
        wide["cache_metrics"]["duplicate_suppressions"] == \
        tight["base_metrics"]["duplicate_suppressions"]
