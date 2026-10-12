"""Verify ME research isolation through real ScannerOrchestrator."""

from datetime import datetime, timezone

import pytest

from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.models import SetupCandidate, SetupState
from app.scanners.direction_gate import (
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
    GATE_ENABLED,
    GATE_BLOCKED,
)


ME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class StaticMEScanner:
    name = ME

    def __init__(self, passed):
        self.passed = passed

    def scan(self, ctx):
        return [
            SetupCandidate(
                scanner_name=ME,
                symbol="BTCUSDT",
                direction="LONG",
                detected_at=datetime.now(timezone.utc),
                state=(
                    SetupState.SETUP_READY
                    if self.passed else SetupState.EXPIRED
                ),
                score=90.0,
                entry_timeframe="5m",
                entry_zone_low=100.0,
                entry_zone_high=101.0,
                invalidation_price=99.0,
                target_1=104.0,
                features={
                    "close_location_passed": self.passed,
                    **(
                        {}
                        if self.passed
                        else {"_oos_rejected": True}
                    ),
                },
            )
        ]


@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("gate_status", ["ENABLED", "BLOCKED", "NONE"])
def test_me_never_enters_tradeable_candidates(passed, gate_status):
    orchestrator = ScannerOrchestrator(
        enabled_scanners=[ME],
    )

    # Run the real orchestrator with a deterministic scanner.
    orchestrator.scanners = {ME: StaticMEScanner(passed)}

    class Context:
        symbol = "BTCUSDT"
        market_regime = "RANGING"
        candles_5m = ()
        candles_15m = ()
        candles_1h = ()
        candles_4h = ()
        evaluated_at = datetime.now(timezone.utc)

    gate_policy = None

    if gate_status != "NONE":
        gate = ScannerDirectionGate(
            ME,
            "LONG",
            GATE_ENABLED if gate_status == "ENABLED" else GATE_BLOCKED,
            reason="test",
        )
        gate_policy = ScannerDirectionGatePolicy(
            {(ME, "LONG"): gate},
            {},
        )

    tradeable, stats = orchestrator.scan_all_with_stats(
        Context(),
        gate_policy=gate_policy,
    )

    me_tradeable = [
        candidate
        for candidate in tradeable
        if candidate.scanner_name == ME
    ]

    observed = [
        candidate
        for candidate in stats.get("_observe_candidates", [])
        if candidate.scanner_name == ME
    ]

    assert not me_tradeable, "ME candidate leaked into tradeable list"
    assert len(observed) == 1
    assert observed[0].features["_observe_only"] is True
    assert observed[0].features["close_location_passed"] is passed

    if not passed:
        assert observed[0].features["_oos_rejected"] is True
