from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app.scanners.models import MarketContext, MarketLevels, IndicatorSnapshot
from app.research.me_reverse_long_v3.closed_context import (
    build_closed_v3_context,
)

UTC = timezone.utc
ASOF = datetime(2026, 10, 12, 17, 7, tzinfo=UTC)


def candles(minutes, count, current_open):
    step = timedelta(minutes=minutes)
    return tuple(
        Candle(
            timestamp=int(
                (current_open - step * i).timestamp() * 1000
            ),
            open=100.0, high=101.0, low=99.0,
            close=100.0, volume=10.0,
        )
        for i in reversed(range(count))
    )


def test_closed_context_does_not_mutate_source():
    ctx = MarketContext(
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
        source_fetch_started_at=ASOF,
    )

    clean = build_closed_v3_context(ctx)

    assert clean is not ctx
    assert len(ctx.candles_5m) == 30
    assert len(clean.candles_5m) == 29
    assert len(clean.candles_15m) == 34
    assert len(clean.candles_1h) == 39
    assert clean.candles_5m[-1].timestamp < ctx.candles_5m[-1].timestamp
    assert ctx.indicators.rsi == 99.0
    assert clean.indicators is not ctx.indicators


def test_naive_context_time_rejected():
    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=(),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime=None,
        levels=MarketLevels(),
        evaluated_at=datetime(2026, 10, 12, 17, 7),
    )

    with pytest.raises(ValueError, match="NAIVE_CONTEXT_TIME"):
        build_closed_v3_context(ctx)
