
from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v2.candles import (
    ClosedCandle,
    validate_window,
)


base = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def make_candle(start, duration):
    return ClosedCandle(
        open_at=base + timedelta(minutes=start),
        close_at=base + timedelta(minutes=start + duration),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
    )


def test_valid_five_minute_candle():
    candles = [make_candle(0, 5)]
    selected, reason = validate_window(
        candles, base, base + timedelta(minutes=5)
    )
    assert reason is None
    assert len(selected) == 1


def test_four_minute_candle_rejected():
    candles = [make_candle(0, 4)]
    _, reason = validate_window(
        candles, base, base + timedelta(minutes=4)
    )
    assert reason == 'invalid_candle_duration'.upper()


def test_six_minute_candle_rejected():
    candles = [make_candle(0, 6)]
    _, reason = validate_window(
        candles, base, base + timedelta(minutes=6)
    )
    assert reason == 'invalid_candle_duration'.upper()


def test_contiguous_four_plus_six_minutes_rejected():
    candles = [
        make_candle(0, 4),
        make_candle(4, 6),
    ]
    _, reason = validate_window(
        candles, base, base + timedelta(minutes=10),
        expected_minutes=10,
    )
    assert reason == 'invalid_candle_duration'.upper()



def test_corrupt_candle_before_touch_cannot_fill():
    from app.research.me_r_long_early_mae_v2.virtual_execution import evaluate_virtual_fill
    from tests.test_me_long_early_mae_v2_pipeline import make_source

    source = make_source(
        source_status='detected'.upper(),
        source_available_at=base,
    )

    candles = [
        make_candle(0, 4),
        ClosedCandle(
            open_at=base + timedelta(minutes=5),
            close_at=base + timedelta(minutes=10),
            open=100.0,
            high=101.0,
            low=99.9,
            close=100.0,
        ),
    ]

    result = evaluate_virtual_fill(
        source,
        candles,
        base + timedelta(minutes=240),
    )

    assert result is None
