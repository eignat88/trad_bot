"""ME Early MAE V2 scanner-runner capture isolation tests."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import scanner_runner
from app.config import Settings
from app.scanners.models import SetupCandidate, SetupState

ME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"


def make_candidate():
    return SetupCandidate(
        scanner_name=ME,
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        signal_candle_open_time=1791806400000,
        state=SetupState.SETUP_READY,
        score=90.0,
        entry_timeframe="5m",
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        reference_price=100.0,
        invalidation_price=97.5,
        target_1=103.0,
        features={"research_test": True},
    )


def run_mock_cycle(monkeypatch, pipeline):
    original = make_candidate()

    shadow = replace(
        original,
        features={**original.features, "_shadow_control": True},
    )

    class FakeOrchestrator:
        scanners = {ME: object()}

        def scan_all_with_stats(self, ctx, **kwargs):
            return [shadow], {
                ME: {
                    "candidates_found": 1,
                    "setups_saved": 0,
                    "errors_count": 0,
                    "duration_ms": 1,
                },
                "_me_early_mae_v2_candidates": [original],
            }

    monkeypatch.setattr(
        scanner_runner,
        "build_market_context",
        lambda *args, **kwargs: object(),
    )

    repo = MagicMock()

    result = scanner_runner.run_scan_cycle(
        object(),
        FakeOrchestrator(),
        repo,
        ["BTCUSDT"],
        None,
        Settings(scanner_workers=1),
        me_early_mae_v2_pipeline=pipeline,
    )

    return result, repo, original


def test_blocked_me_long_reaches_research_capture_once(monkeypatch):
    pipeline = MagicMock()

    (total, scanned, failed), repo, original = run_mock_cycle(
        monkeypatch, pipeline
    )

    assert scanned == 1
    assert failed == 0

    pipeline.capture_candidate.assert_called_once_with(original)

    saved = [
        call.args[0]
        for call in repo.save_setup.call_args_list
        if call.args
    ]

    me_saved = [
        candidate for candidate in saved
        if candidate.scanner_name == ME
    ]

    assert len(me_saved) == 1
    assert me_saved[0].state != SetupState.SETUP_READY
    assert me_saved[0].features.get("_shadow_control") is True


def test_disabled_pipeline_does_not_capture(monkeypatch):
    (_, scanned, failed), repo, _ = run_mock_cycle(
        monkeypatch, None
    )

    assert scanned == 1
    assert failed == 0

    saved = [
        call.args[0]
        for call in repo.save_setup.call_args_list
        if call.args and call.args[0].scanner_name == ME
    ]

    assert len(saved) == 1
    assert saved[0].state != SetupState.SETUP_READY
