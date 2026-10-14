from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.outcomes import evaluate_baseline
from app.research.me_r_long_early_mae_v3.protocol import VARIANTS
from app.research.me_r_long_early_mae_v3.variants import evaluate_variant


T = datetime(2026, 10, 14, tzinfo=timezone.utc)
ENTRY = 100.0
STOP = 97.5
TARGET = 103.0


def candle(index, low, high):
    opened = T + timedelta(minutes=index * 5)
    return ClosedCandle(
        opened,
        opened + timedelta(minutes=5),
        ENTRY, high, low, ENTRY,
    )


@pytest.mark.parametrize(
    "low,high,expected_exit",
    [
        (97.0, 101.0, "STOP_LOSS"),
        (98.0, 104.0, "TAKE_PROFIT"),
    ],
)
def test_threshold_terminal_same_horizon_candle_is_diagnostic_ambiguous(
    low, high, expected_exit
):
    candles = [
        candle(0, low=99.5, high=101.0),
        candle(1, low=low, high=high),
    ]

    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )

    assert baseline.exit_at == T + timedelta(minutes=10)
    assert baseline.exit_reason == expected_exit

    result = evaluate_variant(
        VARIANTS[0], candles, T,
        ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_exit_reason == expected_exit
    assert result.variant_exit_at == baseline.exit_at
    assert result.variant_exit_price == baseline.exit_price
    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"

    # Новое требование: сохранить неопределённость OHLC.
    assert result.variant_status == "AMBIGUOUS"
    assert result.ambiguous_ordering is True


def test_target_without_mae_threshold_is_not_ambiguous():
    candles = [
        candle(0, low=99.5, high=101.0),
        candle(1, low=99.5, high=104.0),
    ]

    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )

    result = evaluate_variant(
        VARIANTS[0], candles, T,
        ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_exit_reason == "TAKE_PROFIT"
    assert result.variant_status == "EVALUATED"
    assert result.ambiguous_ordering is False


def test_mae_threshold_in_previous_candle_not_same_candle_collision():
    candles = [
        candle(0, low=98.0, high=101.0),
        candle(1, low=99.5, high=104.0),
    ]

    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )

    result = evaluate_variant(
        VARIANTS[0], candles, T,
        ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_exit_reason == "TAKE_PROFIT"
    assert result.variant_status == "EVALUATED"
    assert result.ambiguous_ordering is False
