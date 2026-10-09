"""ME clean observations survive expectancy rejection."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator


ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class FakeMEScanner:
    def scan(self, ctx):
        return [SetupCandidate(
            scanner_name=ME_NAME,
            scanner_version="1.2.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=ctx.evaluated_at,
            entry_timeframe="5m",
            signal_candle_open_time=int(ctx.evaluated_at.timestamp() * 1000) - 300000,
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
        )]


def test_me_observation_survives_expectancy_rejection():
    ctx = SimpleNamespace(
        symbol="BTCUSDT",
        evaluated_at=datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc),
        candles_5m=[],
        candles_15m=[],
        candles_1h=[],
        candles_4h=[],
        market_regime="RANGE",
    )

    orchestrator = ScannerOrchestrator(enabled_scanners=[ME_NAME])
    orchestrator.scanners = {ME_NAME: FakeMEScanner()}

    calls = []

    def reject_all(candidates, *args, **kwargs):
        calls.append(list(candidates))
        return [], len(candidates)

    with patch("app.scanners.orchestrator.filter_candidates", side_effect=reject_all):
        tradeable, stats = orchestrator.scan_all_with_stats(
            ctx,
            expectancy_filter=object(),
            gate_policy=None,
        )

    observed = [
        c for c in stats["_observe_candidates"]
        if c.scanner_name == ME_NAME
    ]

    assert calls, "Expectancy filter was not invoked"
    assert any(
        c.scanner_name == ME_NAME
        for batch in calls for c in batch
    ), "ME candidate was not tested by expectancy"

    assert len(observed) == 1
    assert observed[0].features["_observe_only"] is True
    assert all(c.scanner_name != ME_NAME for c in tradeable)
