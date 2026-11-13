"""Regression checks for isolated ME inverse SHORT V2 Scanner source mode.

The production V2 delivery call site remains unchanged: preflight's
negative-control tests must detect its removal.
"""
from __future__ import annotations

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
