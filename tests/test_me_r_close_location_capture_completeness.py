"""ME clean OOS capture must not depend on execution risk geometry."""

from datetime import datetime, timezone

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator


ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class FakeMEScanner:
    name = ME_NAME

    def __init__(self, candidate):
        self.candidate = candidate

    def scan(self, ctx):
        return [self.candidate]


def test_invalid_execution_geometry_reaches_me_observe_only():
    candidate = SetupCandidate(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc),
        entry_zone_low=100.0,
        entry_zone_high=101.0,
        invalidation_price=102.0,
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

    orchestrator = ScannerOrchestrator(enabled_scanners=[ME_NAME])
    orchestrator.scanners = {
        ME_NAME: FakeMEScanner(candidate)
    }

    from types import SimpleNamespace

    ctx = SimpleNamespace(
        symbol="BTCUSDT",
        evaluated_at=datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc),
        candles_5m=[],
        candles_15m=[],
        candles_1h=[],
        candles_4h=[],
        market_regime="RANGE",
    )

    tradeable, stats = orchestrator.scan_all_with_stats(
        ctx,
        gate_policy=None,
        expectancy_filter=None,
    )

    observed = [
        c for c in stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]

    assert len(observed) == 1, (
        "ME base event was lost at execution risk geometry gate"
    )

    assert observed[0].features["_observe_only"] is True
    assert observed[0].features["_oos_rejected"] is True
    assert all(c.scanner_name != ME_NAME for c in tradeable)


def test_same_candle_repeated_scan_is_not_a_new_base_event():
    """A scanner revisit of the same candle must not duplicate observation."""
    from types import SimpleNamespace

    decision = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
    candle_open_ms = int(
        datetime(2026, 10, 9, 13, 55, tzinfo=timezone.utc).timestamp() * 1000
    )

    candidate = SetupCandidate(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=decision,
        entry_timeframe="5m",
        signal_candle_open_time=candle_open_ms,
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
    orchestrator.scanners = {
        ME_NAME: FakeMEScanner(candidate),
    }

    first_tradeable, first_stats = orchestrator.scan_all_with_stats(
        ctx, gate_policy=None, expectancy_filter=None,
    )
    second_tradeable, second_stats = orchestrator.scan_all_with_stats(
        ctx, gate_policy=None, expectancy_filter=None,
    )

    first = [
        c for c in first_stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]
    second = [
        c for c in second_stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]

    assert len(first) == 1
    assert len(second) == 1

    # Second delivery is a retry of the SAME candle, not a new event.
    assert first[0].symbol == second[0].symbol
    assert first[0].signal_candle_open_time == second[0].signal_candle_open_time
    assert first[0].scanner_version == second[0].scanner_version

    assert first[0].features["_observe_only"] is True
    assert second[0].features["_observe_only"] is True
    assert (
        second[0].features["_observe_only_reason"]
        == "ME_CLEAN_OOS_IDEMPOTENT_REDELIVERY"
    )

    assert all(c.scanner_name != ME_NAME for c in first_tradeable)
    assert all(c.scanner_name != ME_NAME for c in second_tradeable)
