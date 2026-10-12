from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app.scanners.me_r_close_location_asof_context import (
    build_me_clean_asof_context,
)
from app.scanners.context_builder import (
    _build_indicators,
    _classify_market_regime,
)


@dataclass
class Candle:
    timestamp: datetime
    open: float = 100.0
    high: float = 102.0
    low: float = 98.0
    close: float = 100.0
    volume: float = 1000.0


@dataclass
class Context:
    evaluated_at: datetime
    candles_5m: tuple
    candles_15m: tuple
    candles_1h: tuple
    indicators: object = None
    market_regime: str = "UNKNOWN"


def candles(timeframe_minutes, count, end_time):
    start = end_time - timedelta(minutes=timeframe_minutes * count)
    return tuple(
        Candle(
            timestamp=start + timedelta(minutes=i * timeframe_minutes),
            close=100 + i * 0.1,
        )
        for i in range(count)
    )


def make_context():
    asof = datetime(2026, 10, 9, 13, 32, tzinfo=timezone.utc)

    return Context(
        evaluated_at=asof,
        candles_5m=candles(5, 40, datetime(2026, 10, 9, 13, 35, tzinfo=timezone.utc)),
        candles_15m=candles(15, 40, datetime(2026, 10, 9, 13, 45, tzinfo=timezone.utc)),
        candles_1h=candles(60, 40, datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)),
    )


def test_only_closed_candles_are_included():
    ctx = make_context()
    clean = build_me_clean_asof_context(ctx)

    assert clean is not None

    for series, interval in (
        (clean.candles_5m, 5),
        (clean.candles_15m, 15),
        (clean.candles_1h, 60),
    ):
        assert all(
            candle.timestamp + timedelta(minutes=interval) <= ctx.evaluated_at
            for candle in series
        )

    assert clean.candles_5m[-1].timestamp.hour == 13
    assert clean.candles_5m[-1].timestamp.minute == 25
    assert clean.candles_15m[-1].timestamp.minute == 15
    assert clean.candles_1h[-1].timestamp.hour == 12


def test_original_context_not_mutated():
    ctx = make_context()
    original = (
        ctx.candles_5m,
        ctx.candles_15m,
        ctx.candles_1h,
    )

    build_me_clean_asof_context(ctx)

    assert (
        ctx.candles_5m,
        ctx.candles_15m,
        ctx.candles_1h,
    ) == original


def test_indicators_recomputed_from_closed_1h():
    ctx = make_context()
    clean = build_me_clean_asof_context(ctx)

    expected = _build_indicators(list(clean.candles_1h))

    assert clean.indicators.rsi == pytest.approx(expected.rsi)
    assert clean.indicators.atr == pytest.approx(expected.atr)
    assert clean.indicators.ema20 == pytest.approx(expected.ema20)

    expected_regime = _classify_market_regime(
        expected,
        clean.candles_1h[-1].close,
    )
    assert clean.market_regime == expected_regime


def test_insufficient_1h_history_fails_closed():
    ctx = make_context()
    ctx.candles_1h = ctx.candles_1h[-10:]

    assert build_me_clean_asof_context(ctx) is None


def test_unaligned_timestamp_rejected():
    ctx = make_context()
    items = list(ctx.candles_15m)
    items[-1].timestamp += timedelta(minutes=1)
    ctx.candles_15m = tuple(items)

    with pytest.raises(ValueError, match="unaligned"):
        build_me_clean_asof_context(ctx)


def test_duplicate_timestamp_rejected():
    ctx = make_context()
    items = list(ctx.candles_5m)
    items[10].timestamp = items[9].timestamp
    ctx.candles_5m = tuple(items)

    with pytest.raises(ValueError, match="unordered or duplicate"):
        build_me_clean_asof_context(ctx)


def test_missing_1h_candle_rejected():
    ctx = make_context()
    ctx.candles_1h = (
        ctx.candles_1h[:15] + ctx.candles_1h[16:]
    )

    with pytest.raises(ValueError, match="missing candle"):
        build_me_clean_asof_context(ctx)


def test_missing_5m_candle_rejected():
    ctx = make_context()
    ctx.candles_5m = (
        ctx.candles_5m[:12] + ctx.candles_5m[13:]
    )

    with pytest.raises(ValueError, match="missing candle"):
        build_me_clean_asof_context(ctx)
