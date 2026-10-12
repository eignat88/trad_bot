"""Off-grid detection boundary tests for Early MAE V2."""

from datetime import timedelta

from tests.test_me_long_early_mae_v2_pipeline import BASE, candle, make_source
from app.research.me_r_long_early_mae_v2.entry_coverage import check_entry_coverage
from app.research.me_r_long_early_mae_v2.virtual_execution import evaluate_virtual_fill


def test_detection_inside_candle_skips_partial_candle():
    detected = BASE + timedelta(minutes=2)
    source = make_source(
        detected_at=detected,
        source_available_at=detected,
        entry_observable_at=detected,
    )

    candles = [
        candle(0, 100.2, 99.9),
        candle(5, 102.0, 101.0),
        candle(10, 100.2, 99.9),
    ]

    fill = evaluate_virtual_fill(
        source, candles, detected + timedelta(minutes=240)
    )

    assert fill is not None
    assert fill.entry_at == BASE + timedelta(minutes=15)


def test_off_grid_entry_coverage_with_missing_candle():
    detected = BASE + timedelta(minutes=2)
    expiry = detected + timedelta(minutes=240)

    candles = [
        candle(i * 5, 102.0, 101.0)
        for i in range(1, 48)
        if i != 2
    ]

    complete, reason = check_entry_coverage(
        candles, detected, expiry, expiry,
    )

    assert complete is False
    assert reason == "INCOMPLETE_ENTRY_COVERAGE"


def test_off_grid_entry_coverage_complete():
    detected = BASE + timedelta(minutes=2)
    expiry = detected + timedelta(minutes=240)

    candles = [
        candle(i * 5, 102.0, 101.0)
        for i in range(1, 48)
    ]

    complete, reason = check_entry_coverage(
        candles, detected, expiry, expiry,
    )

    assert complete is True
    assert reason is None
