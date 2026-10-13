from datetime import datetime, timedelta, timezone

from app.models import Candle
from app.research.me_reverse_long_v3.candle_adapter import (
    build_v3_window,
)

UTC = timezone.utc
START = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)


def source():
    return [
        Candle(
            timestamp=int(
                (START + timedelta(minutes=5 * i)).timestamp() * 1000
            ),
            open=100.0, high=101.0, low=99.0,
            close=100.0, volume=10.0,
        )
        for i in range(48)
    ]


def test_complete_240m_window():
    candles, error = build_v3_window(
        source(), START, START + timedelta(minutes=240)
    )
    assert error is None
    assert len(candles) == 48
    assert candles[-1].close_at == START + timedelta(minutes=240)


def test_immature_not_accepted():
    candles, error = build_v3_window(
        source(), START, START + timedelta(minutes=239)
    )
    assert candles == []
    assert error == "IMMATURE_WINDOW"


def test_unaligned_entry_not_accepted():
    candles, error = build_v3_window(
        source(), START + timedelta(seconds=1),
        START + timedelta(minutes=241),
    )
    assert candles == []
    assert error == "UNALIGNED_ENTRY_TIME"


def test_gap_not_repaired():
    data = source()
    data.pop(10)
    candles, error = build_v3_window(
        data, START, START + timedelta(minutes=240)
    )
    assert candles == []
    assert error == "MISSING_CANDLE_GAP"


def test_duplicate_not_repaired():
    data = source()
    data.append(data[12])
    candles, error = build_v3_window(
        data, START, START + timedelta(minutes=240)
    )
    assert candles == []
    assert error == "DUPLICATE_CANDLE_TIMESTAMP"


def test_incomplete_end_not_finalized():
    candles, error = build_v3_window(
        source()[:-1], START,
        START + timedelta(minutes=240)
    )
    assert candles == []
    assert error == "INCOMPLETE_WINDOW_END"


def test_current_open_candle_not_used():
    data = source()
    # The final candle is not closed at the evaluation time.
    candles, error = build_v3_window(
        data, START, START + timedelta(minutes=237)
    )
    assert candles == []
    assert error == "IMMATURE_WINDOW"
