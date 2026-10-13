from datetime import datetime, timedelta, timezone

from app.models import Candle
from app.research.me_reverse_long_v3.temporal import (
    verify_trigger_candle,
)

UTC = timezone.utc
START = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


def candle(open_at=START):
    return Candle(
        timestamp=int(open_at.timestamp() * 1000),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
    )


def test_closed_trigger():
    result = verify_trigger_candle(
        [candle()], START + timedelta(minutes=5, seconds=1)
    )
    assert result.status == "TEMPORAL_VALIDATED"
    assert result.execution_entry_at == START + timedelta(minutes=10)


def test_current_open_candle_rejected():
    result = verify_trigger_candle(
        [candle()], START + timedelta(minutes=3)
    )
    assert result.status == "TEMPORAL_UNVERIFIED"
    assert result.reason == "TRIGGER_CANDLE_NOT_CLOSED"


def test_boundary_at_exact_close():
    result = verify_trigger_candle(
        [candle()], START + timedelta(minutes=5)
    )
    assert result.status == "TEMPORAL_VALIDATED"


def test_no_candles():
    result = verify_trigger_candle(
        [], START + timedelta(minutes=5)
    )
    assert result.reason == "NO_SOURCE_CANDLES"


def test_unaligned_trigger():
    result = verify_trigger_candle(
        [candle(START + timedelta(seconds=1))],
        START + timedelta(minutes=6),
    )
    assert result.reason == "UNALIGNED_TRIGGER_CANDLE"
