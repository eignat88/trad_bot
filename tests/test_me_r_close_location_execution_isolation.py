"""ME close-location research-only execution isolation."""

from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import scanner_runner
from app.config import Settings
from app.scanners.models import SetupCandidate, SetupState


ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


@pytest.mark.parametrize("passed", [True, False])
def test_run_scan_cycle_never_promotes_me_to_ready(monkeypatch, passed):
    candidate = SetupCandidate(
        scanner_name=ME_NAME,
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime.now(timezone.utc),
        state=SetupState.SETUP_READY if passed else SetupState.EXPIRED,
        features={
            "close_location_passed": passed,
            "_observe_only": True,
            **({} if passed else {"_oos_rejected": True}),
        },
    )

    class FakeOrchestrator:
        scanners = {ME_NAME: object()}

        def scan_all_with_stats(self, ctx, **kwargs):
            return [], {
                ME_NAME: {
                    "candidates_found": 1,
                    "setups_saved": 0,
                    "errors_count": 0,
                    "duration_ms": 1,
                },
                "_observe_candidates": [candidate],
            }

    repo = MagicMock()
    captured = []

    monkeypatch.setattr(
        scanner_runner,
        "build_market_context",
        lambda *args, **kwargs: object(),
    )

    monkeypatch.setattr(
        scanner_runner,
        "_observe_me_r_long_cl_oos",
        lambda repository, c: captured.append(c),
    )

    _, scanned, failed = scanner_runner.run_scan_cycle(
        object(),
        FakeOrchestrator(),
        repo,
        ["BTCUSDT"],
        None,
        Settings(scanner_workers=1),
    )

    assert scanned == 1
    assert failed == 0

    saved = [
        call.args[0]
        for call in repo.save_setup.call_args_list
        if call.args
    ]

    me_saved = [c for c in saved if c.scanner_name == ME_NAME]

    assert len(me_saved) == 1
    assert me_saved[0].state == SetupState.DETECTED

    assert len(captured) == 1
    assert captured[0].scanner_name == ME_NAME
    assert captured[0].features["close_location_passed"] is passed
    assert captured[0].state == SetupState.DETECTED
