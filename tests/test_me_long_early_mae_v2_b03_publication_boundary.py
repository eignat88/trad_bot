from datetime import timedelta

from tests.test_me_long_early_mae_v2_pipeline import BASE, candle, make_source
from app.research.me_r_long_early_mae_v2.virtual_execution import evaluate_virtual_fill


def test_unpublished_source_cannot_fill():
    source = make_source(source_available_at=None)
    candles = [candle(0, 100.2, 99.9)]

    assert evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=240)
    ) is None


def test_prepublication_touch_cannot_fill():
    available = BASE + timedelta(minutes=7)
    source = make_source(source_available_at=available)

    candles = [
        candle(0, 102.0, 101.0),
        candle(5, 100.2, 99.9),
        candle(10, 102.0, 101.0),
        candle(15, 102.0, 101.0),
    ]

    assert evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=240)
    ) is None


def test_late_publication_after_expiry_cannot_fill():
    source = make_source(
        source_available_at=BASE + timedelta(minutes=241)
    )

    candles = [
        candle(0, 100.2, 99.9),
        candle(5, 100.2, 99.9),
    ]

    assert evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=300)
    ) is None


def test_first_complete_candle_after_offgrid_publication_allows_fill():
    available = BASE + timedelta(minutes=7)
    source = make_source(source_available_at=available, entry_observable_at=available)

    candles = [
        candle(5, 100.2, 99.9),
        candle(10, 102.0, 101.0),
        candle(15, 100.2, 99.9),
    ]

    fill = evaluate_virtual_fill(
        source, candles, BASE + timedelta(minutes=240)
    )

    assert fill is not None
    assert fill.entry_at == BASE + timedelta(minutes=20)
    assert fill.entry_available_at == available
