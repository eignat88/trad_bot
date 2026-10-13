from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import Candle
from app.research.me_reverse_long_v3.temporal import (
    verify_source_timeframes,
)

UTC = timezone.utc
START = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


def candle(open_at):
    return Candle(
        timestamp=int(open_at.timestamp() * 1000),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10.0,
    )


def context(
    m5=datetime(2026, 10, 12, 17, 0, tzinfo=UTC),
    m15=datetime(2026, 10, 12, 16, 45, tzinfo=UTC),
    h1=datetime(2026, 10, 12, 16, 0, tzinfo=UTC),
):
    return SimpleNamespace(
        candles_5m=[candle(m5)],
        candles_15m=[candle(m15)],
        candles_1h=[candle(h1)],
    )


def test_all_timeframes_closed():
    valid, reason = verify_source_timeframes(
        context(), START + timedelta(minutes=5)
    )

    assert valid is True
    assert reason == "ALL_SOURCE_TIMEFRAMES_CLOSED"


@pytest.mark.parametrize("field,opened", [
    ("m5", datetime(2026, 10, 12, 17, 5, tzinfo=UTC)),
    ("m15", datetime(2026, 10, 12, 17, 0, tzinfo=UTC)),
    ("h1", datetime(2026, 10, 12, 17, 0, tzinfo=UTC)),
])
def test_unclosed_timeframes_rejected(field, opened):
    kwargs = {field: opened}
    valid, reason = verify_source_timeframes(
        context(**kwargs), START + timedelta(minutes=5)
    )

    assert valid is False
    assert reason.endswith("_NOT_CLOSED")


def test_missing_source_rejected():
    ctx = context()
    ctx.candles_1h = []

    valid, reason = verify_source_timeframes(
        ctx, START + timedelta(minutes=5)
    )

    assert valid is False
    assert reason == "CANDLES_1H_MISSING"


def test_unaligned_15m_rejected():
    ctx = context(
        m15=datetime(2026, 10, 12, 16, 46, tzinfo=UTC)
    )

    valid, reason = verify_source_timeframes(
        ctx, START + timedelta(minutes=5)
    )

    assert valid is False
    assert reason == "CANDLES_15M_UNALIGNED"


def test_naive_detection_rejected():
    valid, reason = verify_source_timeframes(
        context(), datetime(2026, 10, 12, 17, 5)
    )

    assert valid is False
    assert reason == "NAIVE_DETECTION_TIME"
