from unittest.mock import patch

from app.scanners.funnel_diagnostics import FunnelCollector
from app.scanners.momentum_exhaustion_reverse_long_v2 import (
    MomentumExhaustionReverseLongV2Scanner,
)


def test_production_v2_uses_global_collector():
    scanner = MomentumExhaustionReverseLongV2Scanner()
    global_funnel = FunnelCollector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")

    with patch(
        "app.scanners.momentum_exhaustion_reverse_long_v2.get_funnel_collector",
        return_value=global_funnel,
    ) as getter:
        scanner._scan_long(
            type("Context", (), {"candles_15m": (), "candles_5m": ()})()
        )

    getter.assert_called_once_with(scanner.name)
    assert global_funnel._current.TOTAL_SCANS == 1


def test_v3_research_funnel_does_not_touch_global_collector():
    scanner = MomentumExhaustionReverseLongV2Scanner()
    research_funnel = FunnelCollector("ME_REVERSE_LONG_V3_RESEARCH_ONLY")
    scanner._research_funnel = research_funnel

    with patch(
        "app.scanners.momentum_exhaustion_reverse_long_v2.get_funnel_collector"
    ) as getter:
        scanner._scan_long(
            type("Context", (), {"candles_15m": (), "candles_5m": ()})()
        )

    getter.assert_not_called()
    assert research_funnel._current.TOTAL_SCANS == 1
    assert research_funnel._current.NO_DATA == 1
