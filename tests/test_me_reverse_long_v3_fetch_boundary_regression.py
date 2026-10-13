from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
)
from app.research.me_reverse_long_v3.closed_context import (
    build_closed_v3_context,
)

UTC = timezone.utc


def candles(step_minutes, count, last_open):
    return tuple(
        Candle(
            timestamp=int(
                (
                    last_open - timedelta(minutes=step_minutes * i)
                ).timestamp() * 1000
            ),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
        )
        for i in reversed(range(count))
    )


def test_fetch_crossing_5m_boundary_excludes_partial_source():
    day = datetime(2026, 10, 13, tzinfo=UTC)
    fetched = day.replace(hour=12, minute=4, second=59)
    evaluated = day.replace(hour=12, minute=5, second=1)

    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=candles(
            5, 40, day.replace(hour=12, minute=0)
        ),
        candles_15m=candles(
            15, 40, day.replace(hour=12, minute=0)
        ),
        candles_1h=candles(
            60, 40, day.replace(hour=12, minute=0)
        ),
        candles_4h=candles(
            240, 40, day.replace(hour=12, minute=0)
        ),
        indicators=IndicatorSnapshot(),
        market_regime="RANGE",
        levels=MarketLevels(),
        evaluated_at=evaluated,
        source_fetch_started_at=fetched,
    )

    clean = build_closed_v3_context(ctx)

    expected_last_5m = day.replace(hour=11, minute=55)
    assert clean.candles_5m[-1].timestamp == int(
        expected_last_5m.timestamp() * 1000
    )

    assert clean.evaluated_at == evaluated
    assert len(clean.candles_5m) == 39


def test_missing_fetch_timestamp_fails_closed():
    day = datetime(2026, 10, 13, tzinfo=UTC)

    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=(),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime=None,
        levels=MarketLevels(),
        evaluated_at=day,
    )

    with pytest.raises(ValueError, match="MISSING_SOURCE_FETCH_TIME"):
        build_closed_v3_context(ctx)


def test_future_fetch_timestamp_fails_closed():
    day = datetime(2026, 10, 13, tzinfo=UTC)

    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=(),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime=None,
        levels=MarketLevels(),
        evaluated_at=day,
        source_fetch_started_at=day + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="SOURCE_FETCH_AFTER_EVALUATION"):
        build_closed_v3_context(ctx)
