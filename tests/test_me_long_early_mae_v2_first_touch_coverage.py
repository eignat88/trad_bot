"""Regression tests for first-touch entry coverage."""

from datetime import timedelta

from tests.test_me_long_early_mae_v2_pipeline import BASE, candle, make_source
from app.research.me_r_long_early_mae_v2.virtual_execution import evaluate_virtual_fill


def test_missing_candle_before_touch_must_not_create_fill():
    source = make_source(source_status="DETECTED")
    candles = [
        candle(5, 102.0, 101.0),
        candle(10, 100.2, 99.9),
    ]

    fill = evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=240)
    )

    assert fill is None


def test_complete_path_to_first_touch_allows_fill():
    source = make_source(source_status="DETECTED")
    candles = [
        candle(0, 102.0, 101.0),
        candle(5, 102.0, 101.0),
        candle(10, 100.2, 99.9),
    ]

    fill = evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=240)
    )

    assert fill is not None
    assert fill.entry_at == BASE + timedelta(minutes=15)
