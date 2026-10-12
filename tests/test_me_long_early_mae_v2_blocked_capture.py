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

ME = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
OTHER = "TREND_PULLBACK_V2"


class StaticScanner:
    def __init__(self, name, direction="LONG"):
        self.name = name
        self.direction = direction

    def scan(self, ctx):
        return [
            SetupCandidate(
                scanner_name=self.name,
                scanner_version="1.0.0",
                symbol="BTCUSDT",
                direction=self.direction,
                detected_at=ctx.evaluated_at,
                signal_candle_open_time=1791643200000,
                state=SetupState.SETUP_READY,
                score=90.0,
                entry_timeframe="5m",
                entry_zone_low=99.8 if self.direction == "LONG" else 100.2,
                entry_zone_high=100.1 if self.direction == "LONG" else 100.5,
                invalidation_price=97.5 if self.direction == "LONG" else 102.5,
                target_1=103.0 if self.direction == "LONG" else 97.0,
                features={"research_test": True},
            )
        ]


class Context:
    symbol = "BTCUSDT"
    market_regime = "RANGING"
    candles_5m = ()
    candles_15m = ()
    candles_1h = ()
    candles_4h = ()
    evaluated_at = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def scan_with_gate(scanner_name, direction, status):
    orchestrator = ScannerOrchestrator(enabled_scanners=[scanner_name])
    orchestrator.scanners = {
        scanner_name: StaticScanner(scanner_name, direction)
    }

    gate = ScannerDirectionGate(
        scanner_name, direction, status, reason="test"
    )
    policy = ScannerDirectionGatePolicy(
        {(scanner_name, direction): gate}, {}
    )

    return orchestrator.scan_all_with_stats(
        Context(), gate_policy=policy
    )


def test_blocked_v1_long_is_in_independent_v2_cohort():
    candidates, stats = scan_with_gate(ME, "LONG", GATE_BLOCKED)

    cohort = stats["_me_early_mae_v2_candidates"]
    assert len(cohort) == 1
    assert cohort[0].scanner_name == ME
    assert cohort[0].direction == "LONG"
    assert cohort[0].signal_candle_open_time > 0


def test_blocked_v1_long_cannot_be_ready_to_trade():
    candidates, stats = scan_with_gate(ME, "LONG", GATE_BLOCKED)

    assert all(c.state != SetupState.SETUP_READY for c in candidates)

    v1_candidates = [c for c in candidates if c.scanner_name == ME]
    assert len(v1_candidates) == 1
    assert v1_candidates[0].features.get("_shadow_control") is True

    assert len(stats["_me_early_mae_v2_candidates"]) == 1


def test_enabled_v1_long_remains_in_v2_cohort():
    candidates, stats = scan_with_gate(ME, "LONG", GATE_ENABLED)

    assert len(stats["_me_early_mae_v2_candidates"]) == 1
    assert len([c for c in candidates if c.scanner_name == ME]) == 1


@pytest.mark.parametrize(
    "scanner_name,direction",
    [
        (OTHER, "LONG"),
        (ME, "SHORT"),
    ],
)
def test_unrelated_candidates_never_enter_me_v2_cohort(
    scanner_name, direction
):
    _, stats = scan_with_gate(scanner_name, direction, GATE_BLOCKED)
    assert stats["_me_early_mae_v2_candidates"] == []
