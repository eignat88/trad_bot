"""ME clean OOS candidate must remain retryable after observer failure."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator


ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class FakeMEScanner:
    name = ME_NAME

    def __init__(self, candidate):
        self.candidate = candidate

    def scan(self, ctx):
        return [self.candidate]


def test_same_candle_is_retryable_after_failed_persistence():
    decision = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
    candle_ms = int(decision.timestamp() * 1000) - 300000

    candidate = SetupCandidate(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=decision,
        entry_timeframe="5m",
        signal_candle_open_time=candle_ms,
        entry_zone_low=100.0,
        entry_zone_high=101.0,
        invalidation_price=99.0,
        target_1=103.0,
        reference_price=100.0,
        state=SetupState.EXPIRED,
        features={
            "close_location": 0.30,
            "close_location_passed": False,
            "_oos_rejected": True,
            "oos_clean_observer_version": "1.2.0",
        },
    )

    ctx = SimpleNamespace(
        symbol="BTCUSDT",
        evaluated_at=decision,
        candles_5m=[],
        candles_15m=[],
        candles_1h=[],
        candles_4h=[],
        market_regime="RANGE",
    )

    orchestrator = ScannerOrchestrator(enabled_scanners=[ME_NAME])
    orchestrator.scanners = {ME_NAME: FakeMEScanner(candidate)}

    _, first_stats = orchestrator.scan_all_with_stats(
        ctx, gate_policy=None, expectancy_filter=None,
    )

    first = [
        c for c in first_stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]
    assert len(first) == 1

    # Simulate unsuccessful dedicated PostgreSQL observation.
    # No persistence acknowledgement is sent to orchestrator.

    _, second_stats = orchestrator.scan_all_with_stats(
        ctx, gate_policy=None, expectancy_filter=None,
    )

    second = [
        c for c in second_stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]

    assert len(second) == 1, (
        "Failed ME observation was suppressed by in-memory dedup"
    )
