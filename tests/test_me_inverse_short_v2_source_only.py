"""Regression checks for isolated ME inverse SHORT V2 Scanner source mode.

The production V2 delivery call site remains unchanged: preflight's
negative-control tests must detect its removal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scanner_runner as runner


DETECTOR = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"


def test_source_only_delivers_one_event_per_source_without_legacy_writes(monkeypatch):
    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    seen = []
    legacy = []

    class Repository:
        def save_run_stat(self, *args, **kwargs):
            assert args[1] == DETECTOR
        def save_event(self, *args, **kwargs):
            raise AssertionError("ordinary event write in source-only mode")
        def save_setup(self, *args, **kwargs):
            raise AssertionError("ordinary setup write in source-only mode")
        def save_error(self, *args, **kwargs):
            raise AssertionError("unexpected error in source-only mode")

    class Orchestrator:
        scanners = {DETECTOR: object()}
        def scan_all_with_stats(self, *args, **kwargs):
            return [object()], {
                DETECTOR: {
                    "candidates_found": 1, "setups_saved": 0,
                    "errors_count": 0, "duration_ms": 1.0,
                },
                "_me_inverse_short_source_events": ["source-event"],
                "_research_candidates": [object()],
                "_expectancy_rejected_candidates": [object()],
                "_regime_rejected_candidates": [object()],
                "_observe_candidates": [object()],
            }

    class Report:
        expected_symbols = 0
        event_persisted = False
        is_complete = True
        def record_processed(self, symbol):
            pass
        def record_outcome(self, *args):
            raise AssertionError("unexpected cycle error")

    monkeypatch.setattr(runner, "build_market_context", lambda *a, **kw: object())
    monkeypatch.setattr(
        runner.ScannerDirectionGatePolicy, "load_for_cycle",
        lambda *a, **kw: object(),
    )
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_v2_source",
        lambda _repo, event: seen.append(event),
    )
    # V1 hook must stay on the default production path for V1 byte/route
    # identity, but with V1 activation OFF it must never persist data.
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_source",
        lambda _repo, event: legacy.append(event),
    )
    monkeypatch.setattr(
        runner, "_capture_me_short_a_source",
        lambda *a, **kw: pytest.fail("ME SHORT A capture invoked"),
    )
    monkeypatch.setattr(
        runner, "_observe_srr_long_research",
        lambda *a, **kw: pytest.fail("SRR capture invoked"),
    )
    monkeypatch.setattr(runner, "_research_observer", None)

    settings = SimpleNamespace(
        expectancy_filter_enabled=False,
        scanner_workers=1,
        blocked_scanner_directions=frozenset(),
        scanner_regime_whitelist={},
        regime_filter_enabled=False,
        trading_mode="paper",
    )
    counts = runner.run_scan_cycle(
        client=object(),
        orchestrator=Orchestrator(),
        repository=Repository(),
        symbols=["BTCUSDT"],
        run_id=1,
        settings=settings,
        report=Report(),
    )

    assert counts == (0, 1, 0)  # Zero normal tradeable setups, one scanned symbol
    assert seen == ["source-event"]  # Exactly one V2 delivery


@pytest.mark.parametrize(
    ("mode", "live_enabled", "v1", "error"),
    [
        ("live", "false", "0", "paper trading mode"),
        ("paper", "true", "0", "live trading OFF"),
        ("paper", "false", "1", "V1 capture OFF"),
    ],
)
def test_source_only_rejects_unsafe_startup_before_network(
    monkeypatch, mode, live_enabled, v1, error
):
    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", live_enabled)
    monkeypatch.setenv("ME_INVERSE_SHORT_V1_ENABLED", v1)
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner.signal, "signal", lambda *a: None)
    monkeypatch.setattr(
        runner, "_load_runner_settings",
        lambda: SimpleNamespace(trading_mode=mode),
    )
    monkeypatch.setattr(
        runner, "BybitClient",
        lambda *_: pytest.fail("network initialized before safety check"),
    )
    with pytest.raises(RuntimeError, match=error):
        runner.main()


def test_source_only_has_one_v2_delivery_call_site():
    from pathlib import Path

    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert source.count(
        "_capture_me_inverse_short_v2_source(repository, inv_candidate)"
    ) == 1



def test_source_only_skips_shared_postscan_database_effects(monkeypatch):
    """Clean V2-only deployments do not have legacy funnel/setup marts."""
    def forbidden(*args, **kwargs):
        pytest.fail("legacy postscan database write attempted in SOURCE_ONLY")

    class Repository:
        expire_setups = forbidden
        aggregate_signals = forbidden

    monkeypatch.setattr(runner, "flush_and_persist_all", forbidden)

    assert runner._postscan_housekeeping(
        Repository(), run_id=123, source_only=True
    ) == 0


def test_default_scanner_preserves_postscan_operations(monkeypatch):
    """SOURCE_ONLY must not alter the legacy scanner's postscan behavior."""
    called = []

    class Repository:
        def expire_setups(self):
            called.append("expire")
        def aggregate_signals(self, run_id):
            called.append(("aggregate", run_id))
            return 7

    monkeypatch.setattr(
        runner, "flush_and_persist_all",
        lambda repository: called.append("funnel"),
    )

    assert runner._postscan_housekeeping(
        Repository(), run_id=456, source_only=False
    ) == 7
    assert called == ["funnel", "expire", ("aggregate", 456)]


def test_default_scanner_funnel_exception_stays_fail_open(monkeypatch):
    """Keep original exception policy for the ordinary Scanner."""
    called = []

    def broken_funnel(repository):
        called.append("funnel")
        raise RuntimeError("test funnel failure")

    class Repository:
        def expire_setups(self):
            called.append("expire")
        def aggregate_signals(self, run_id):
            called.append("aggregate")
            return 0

    monkeypatch.setattr(runner, "flush_and_persist_all", broken_funnel)
    assert runner._postscan_housekeeping(
        Repository(), run_id=1, source_only=False
    ) == 0
    assert called == ["funnel", "expire", "aggregate"]


# ---------------------------------------------------------------------------
# Issue #305 regression: SOURCE_ONLY routing must preserve BOTH research
# source streams.  Before the fix the ``source_only`` branch rebuilt
# ``symbol_stats`` with only ``_me_inverse_short_source_events`` and silently
# dropped ``_me_upstream_source_events`` — the #305 detector-level upstream
# capture then always saw an empty list (upstream events = 0 on the VPS).
# ---------------------------------------------------------------------------

SIGNAL_AT = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
FREEZE_AT = SIGNAL_AT - timedelta(hours=2)


def _up_candidate(**overrides):
    from app.scanners.models import SetupCandidate, SetupState

    values = dict(
        scanner_name=DETECTOR,
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        detected_at=SIGNAL_AT,
        setup_started_at=SIGNAL_AT,
        signal_candle_open_time=int(SIGNAL_AT.timestamp() // 300) * 300_000,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        score=0.0,
        reasons=(),
        market_regime="TREND_UP",
        state=SetupState.DETECTED,
        features={
            "rsi_14": 55.0,
            "rsi_delta_3": 1.25,
            "entry_confirmation_rsi_rising": True,
        },
    )
    values.update(overrides)
    return SetupCandidate(**values)


def _settings(**overrides):
    values = dict(
        expectancy_filter_enabled=False,
        scanner_workers=1,
        blocked_scanner_directions=frozenset(),
        scanner_regime_whitelist={},
        regime_filter_enabled=False,
        trading_mode="paper",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _StrictRepository:
    """Any ordinary write is a test failure (SOURCE_ONLY contract)."""

    def save_run_stat(self, *args, **kwargs):
        pass

    def save_event(self, *args, **kwargs):
        raise AssertionError("ordinary event write in source-only mode")

    def save_setup(self, *args, **kwargs):
        raise AssertionError("ordinary setup write in source-only mode")

    def save_error(self, *args, **kwargs):
        raise AssertionError("unexpected error in source-only mode")


class _RecRepository:
    """Recording repository for the normal (non SOURCE_ONLY) path."""

    def __init__(self):
        self.setups = []
        self.events = []
        self.errors = []

    def save_run_stat(self, *args, **kwargs):
        pass

    def save_setup(self, c, run_id=None):
        self.setups.append(c)

    def save_event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def save_error(self, **kwargs):
        self.errors.append(kwargs)


class _Report:
    expected_symbols = 0
    event_persisted = False
    is_complete = True

    def record_processed(self, symbol):
        pass

    def record_outcome(self, *args):
        raise AssertionError("unexpected cycle error")


class _Orch:
    """Fake orchestrator: fixed candidates + fresh stats per call."""

    def __init__(self, candidates, stats):
        self.scanners = {DETECTOR: object()}
        self._candidates = list(candidates)
        self._stats = stats

    def scan_all_with_stats(self, *args, **kwargs):
        return list(self._candidates), dict(self._stats)


def _patch_runner(monkeypatch):
    monkeypatch.setattr(
        runner, "build_market_context", lambda *a, **kw: object(),
    )
    monkeypatch.setattr(
        runner.ScannerDirectionGatePolicy, "load_for_cycle",
        lambda *a, **kw: object(),
    )
    monkeypatch.setattr(runner, "_research_observer", None)


def _stats(**overrides):
    stats = {
        DETECTOR: {
            "candidates_found": 1, "setups_saved": 0,
            "errors_count": 0, "duration_ms": 1.0,
        },
        "_me_inverse_short_source_events": ["source-event"],
        "_me_upstream_source_events": [],
    }
    stats.update(overrides)
    return stats


def _deliver(monkeypatch):
    """Record every research delivery hook; fail on unrelated captures."""
    seen = {"v1": [], "v2": [], "upstream": []}
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_source",
        lambda _r, e: seen["v1"].append(e),
    )
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_v2_source",
        lambda _r, e: seen["v2"].append(e),
    )
    monkeypatch.setattr(
        runner, "_capture_me_upstream_source_v1",
        lambda _r, e: seen["upstream"].append(e),
    )
    monkeypatch.setattr(
        runner, "_capture_me_short_a_source",
        lambda *a, **kw: pytest.fail("ME SHORT A capture invoked"),
    )
    monkeypatch.setattr(
        runner, "_observe_srr_long_research",
        lambda *a, **kw: pytest.fail("SRR capture invoked"),
    )
    return seen


# --- Scenario A: SOURCE_ONLY with an upstream event -----------------------

def test_scenario_a_source_only_preserves_both_research_streams(monkeypatch):
    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    _patch_runner(monkeypatch)
    seen = _deliver(monkeypatch)
    up_event = _up_candidate()

    counts = runner.run_scan_cycle(
        client=object(),
        orchestrator=_Orch([object()], _stats(
            _me_upstream_source_events=[up_event],
            _research_candidates=[object()],
            _expectancy_rejected_candidates=[object()],
        )),
        repository=_StrictRepository(),
        symbols=["BTCUSDT"],
        run_id=1,
        settings=_settings(),
        report=_Report(),
    )

    assert counts == (0, 1, 0)  # zero normal setups, one scanned symbol
    assert seen["v2"] == ["source-event"]   # V2/V3 source metadata preserved
    assert seen["upstream"] == [up_event]   # ORIGINAL pre-filter candidate


# --- Scenario B: SOURCE_ONLY without any events ---------------------------

def test_scenario_b_source_only_without_events_is_quiet(monkeypatch):
    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    _patch_runner(monkeypatch)
    seen = _deliver(monkeypatch)

    counts = runner.run_scan_cycle(
        client=object(),
        orchestrator=_Orch([], _stats(
            _me_inverse_short_source_events=[],
            _me_upstream_source_events=[],
        )),
        repository=_StrictRepository(),
        symbols=["BTCUSDT"],
        run_id=1,
        settings=_settings(),
        report=_Report(),
    )

    assert counts == (0, 1, 0)
    assert seen["v2"] == [] and seen["upstream"] == []  # no false deliveries


# --- Scenario C: SOURCE_ONLY disabled — pre-fix behavior unchanged --------

def test_scenario_c_source_only_off_keeps_normal_routing(monkeypatch):
    monkeypatch.delenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", raising=False)
    _patch_runner(monkeypatch)
    seen = _deliver(monkeypatch)
    short_a = []
    # Normal mode legitimately runs the ME SHORT A research intake per
    # candidate (unchanged pre-fix behavior) — record instead of forbidding.
    monkeypatch.setattr(
        runner, "_capture_me_short_a_source",
        lambda *a, **kw: short_a.append(a),
    )
    ordinary = _up_candidate(symbol="SOLUSDT")
    up_event = _up_candidate()
    repository = _RecRepository()

    counts = runner.run_scan_cycle(
        client=object(),
        orchestrator=_Orch([ordinary], _stats(
            _me_upstream_source_events=[up_event],
        )),
        repository=repository,
        symbols=["BTCUSDT"],
        run_id=1,
        settings=_settings(),
        report=_Report(),
    )

    # Ordinary routing is intact: setups are saved and DETECTED events land.
    assert [c.symbol for c in repository.setups] == ["SOLUSDT"]
    assert repository.events[0][0][0] == "SETUP_DETECTED"
    assert repository.errors == []
    # The research source streams keep working in normal mode too.
    assert seen["v2"] == ["source-event"]
    assert seen["upstream"] == [up_event]
    assert counts[1] == 1 and counts[2] == 0


# --- Scenario D: repeated delivery stays idempotent -----------------------

def test_scenario_d_repeated_upstream_delivery_is_idempotent(monkeypatch):
    from app.research.me_upstream_source_v1.protocol import (
        EXPERIMENT_ID,
        protocol_fingerprint,
    )
    from app.research.me_upstream_source_v1.store import InMemoryUpstreamSourceStore

    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    monkeypatch.setenv("ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED", "1")
    _patch_runner(monkeypatch)
    monkeypatch.setattr(
        runner, "_capture_me_short_a_source", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_source", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        runner, "_capture_me_inverse_short_v2_source", lambda *a, **kw: None,
    )
    store = InMemoryUpstreamSourceStore({
        "experiment_id": EXPERIMENT_ID,
        "status": "RUNNING",
        "protocol_fingerprint": protocol_fingerprint(),
        "freeze_ts": FREEZE_AT,
    })
    monkeypatch.setattr(
        runner, "_get_me_upstream_source_store", lambda repository: store,
    )
    up_event = _up_candidate()

    for _ in range(2):  # the same event delivered twice (restart / retry)
        runner.run_scan_cycle(
            client=object(),
            orchestrator=_Orch([], _stats(
                _me_inverse_short_source_events=[],
                _me_upstream_source_events=[up_event],
            )),
            repository=_StrictRepository(),
            symbols=["BTCUSDT"],
            run_id=1,
            settings=_settings(),
            report=_Report(),
        )

    # Idempotent on source_event_key: one row, first snapshot preserved.
    assert len(store.events) == 1
    assert store.states == []  # no conflict, no BLOCKED


# --- Scenario E: trading direction blocked — upstream capture unaffected --

def test_scenario_e_upstream_delivery_survives_blocked_direction(monkeypatch):
    monkeypatch.setenv("ME_INVERSE_SHORT_V2_SOURCE_ONLY", "1")
    _patch_runner(monkeypatch)
    seen = _deliver(monkeypatch)
    up_event = _up_candidate()

    counts = runner.run_scan_cycle(
        client=object(),
        orchestrator=_Orch([], _stats(  # gate blocked every tradeable setup
            _me_upstream_source_events=[up_event],
        )),
        repository=_StrictRepository(),
        symbols=["BTCUSDT"],
        run_id=1,
        settings=_settings(
            blocked_scanner_directions=frozenset({f"{DETECTOR}:LONG"}),
        ),
        report=_Report(),
    )

    # Detector-level delivery is independent of the trading gate ...
    assert seen["upstream"] == [up_event]
    assert seen["v2"] == ["source-event"]
    # ... and NO tradeable routing / side effects happen.
    assert counts == (0, 1, 0)


# --- Static regression: the rebuild must keep BOTH source keys ------------

def test_source_only_rebuild_lists_both_source_keys():
    from pathlib import Path

    source = Path(runner.__file__).read_text(encoding="utf-8")
    rebuild = source.index(
        'symbol_stats = {\n'
        '                        "_me_inverse_short_source_events": '
        'source_events,\n'
        '                        "_me_upstream_source_events": '
        'upstream_events,\n'
        '                    }'
    )
    assert rebuild > 0
