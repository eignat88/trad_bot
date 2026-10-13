from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.models import Candle
from app.scanners.models import (
    MarketContext,
    MarketLevels,
    IndicatorSnapshot,
    SetupCandidate,
    ScannerDirection,
    SetupState,
)
from app.research.me_reverse_long_v3.detector import detect_v3_closed
from app.research.me_reverse_long_v3.entry_policy import (
    next_executable_entry_at,
)
from app.research.me_reverse_long_v3.temporal import (
    verify_trigger_candle,
    verify_source_timeframes,
)

UTC = timezone.utc
ASOF = datetime(2026, 10, 12, 17, 7, tzinfo=UTC)


def candles(minutes, count, last_open):
    step = timedelta(minutes=minutes)
    return tuple(
        Candle(
            timestamp=int((last_open - i * step).timestamp() * 1000),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
        )
        for i in reversed(range(count))
    )


def make_context():
    return MarketContext(
        symbol="BTCUSDT",
        candles_5m=candles(
            5, 30, datetime(2026, 10, 12, 17, 5, tzinfo=UTC)
        ),
        candles_15m=candles(
            15, 35, datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
        ),
        candles_1h=candles(
            60, 40, datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
        ),
        candles_4h=candles(
            240, 40, datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
        ),
        indicators=IndicatorSnapshot(rsi=99.0),
        market_regime="HIGH_VOLATILITY",
        levels=MarketLevels(),
        evaluated_at=ASOF,
    )


def test_detector_to_temporal_contract():
    candidate = SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG.value,
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=ASOF,
        setup_started_at=ASOF,
        reference_price=105.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        target_2=None,
        market_regime="NORMAL",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.3},
    )

    scanner = MagicMock()
    scanner.scan.return_value = [candidate]

    result = detect_v3_closed(make_context(), scanner=scanner)

    assert result.status == "SIGNAL", result.reason
    assert result.candidate is candidate

    clean = result.clean_context
    assert len(clean.candles_5m) == 29
    assert len(clean.candles_15m) == 34

    temporal = verify_trigger_candle(
        clean.candles_5m,
        candidate.detected_at,
    )
    source_ok, source_reason = verify_source_timeframes(
        clean,
        candidate.detected_at,
    )

    assert temporal.status == "TEMPORAL_VALIDATED"
    assert source_ok, source_reason
    assert temporal.trigger_close_at <= ASOF
    assert temporal.execution_entry_at == next_executable_entry_at(ASOF)
    assert temporal.execution_entry_at > ASOF
