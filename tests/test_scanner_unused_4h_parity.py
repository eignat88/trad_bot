"""Issue #250: 4H removal must not affect FVG's nonzero orchestrator outcomes.

Research-only deterministic replay: no external API, repository or observer.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID

import app.scanners.fvg_reaction_long_local_struct_v1 as fvg
from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
from app.scanners.orchestrator import ScannerOrchestrator


_MS = 300_000
_NOW = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)


def _c(ts, o, h, l, c):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=100)


def _window(cycle):
    w = [_c(i * _MS, 100, 102, 98, 100) for i in range(200)]
    if cycle == 1:
        w[197] = _c(197 * _MS, 100, 102, 98, 101)
        w[198] = _c(198 * _MS, 101, 112, 100, 110)
        w[199] = _c(199 * _MS, 110, 115, 103, 114)
    elif cycle == 2:
        w[198] = _c(199 * _MS, 110, 115, 103, 114)
        w[199] = _c(200 * _MS, 114, 116, 102, 103)
    else:
        w[197] = _c(199 * _MS, 110, 115, 103, 114)
        w[198] = _c(200 * _MS, 114, 116, 102, 103)
        w[199] = _c(201 * _MS, 103, 120, 102, 118)
    return tuple(w)


def _ctx(candles_5m):
    return MarketContext(
        symbol="BTC",
        candles_5m=candles_5m,
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime="RANGE",
        levels=MarketLevels(),
        evaluated_at=_NOW,
        source_fetch_started_at=_NOW,
    )


def _normalized_stats(stats):
    return {
        name: (
            {k: v for k, v in data.items() if k != "duration_ms"}
            if isinstance(data, dict)
            else [asdict(v) if hasattr(v, "__dataclass_fields__") else v for v in data]
            if isinstance(data, list) else data
        )
        for name, data in stats.items()
    }


def test_fvg_nonzero_orchestrator_parity_with_and_without_4h():
    baseline = ScannerOrchestrator(enabled_scanners=["FVG_REACTION_LONG_LOCAL_STRUCT_V1"])
    variant = ScannerOrchestrator(enabled_scanners=["FVG_REACTION_LONG_LOCAL_STRUCT_V1"])
    assert baseline.repository is variant.repository is None
    assert baseline.research_observer is variant.research_observer is None

    original_candidate = fvg.SetupCandidate

    def fixed_candidate(**kwargs):
        return original_candidate(setup_id=UUID(int=250), **kwargs)

    # Patch the constructor at the FVG import site, NOT dataclass default_factory.
    # Never patch FVG.scan: orchestration must execute its original scanner.
    with patch.object(fvg, "SetupCandidate", fixed_candidate), patch.object(fvg, "datetime") as clock:
        clock.now.return_value = _NOW
        for cycle in (1, 2, 3):
            candles = _window(cycle)
            context = _ctx(candles)
            left, left_stats = baseline.scan_all_with_stats(
                replace(context, candles_4h=candles)
            )
            right, right_stats = variant.scan_all_with_stats(
                replace(context, candles_4h=())
            )
            assert [asdict(c) for c in left] == [asdict(c) for c in right]
            assert _normalized_stats(left_stats) == _normalized_stats(right_stats)
            assert left_stats["FVG_REACTION_LONG_LOCAL_STRUCT_V1"]["errors_count"] == 0
            assert right_stats["FVG_REACTION_LONG_LOCAL_STRUCT_V1"]["errors_count"] == 0
            if cycle < 3:
                assert not left and not right
            else:
                assert len(left) == len(right) == 1
                assert left[0].direction == right[0].direction == "LONG"
                assert left[0].signal_candle_open_time == right[0].signal_candle_open_time
