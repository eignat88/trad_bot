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


def candle(i, low, high, close=100.0):
    opened = T + timedelta(minutes=i * 5)
    return ClosedCandle(
        opened, opened + timedelta(minutes=5),
        ENTRY, high, low, close,
    )


@pytest.mark.parametrize(
    "low,high,reason,ambiguous",
    [
        (97.0, 101.0, "STOP_LOSS", False),
        (98.0, 104.0, "TAKE_PROFIT", False),
        (97.0, 104.0, "AMBIGUOUS_STOP_FIRST", True),
    ],
)
def test_intrabar_conflict_preserves_terminal_precedence(
    low, high, reason, ambiguous
):
    candles = [candle(0, low, high)]

    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )

    assert baseline.exit_reason == reason
    assert baseline.ambiguous is ambiguous

    result = evaluate_variant(
        VARIANTS[0], candles, T,
        ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_exit_reason == reason
    # Baseline ambiguity and variant threshold/terminal ambiguity differ.
    assert result.ambiguous_ordering is True
    assert result.variant_status == "AMBIGUOUS"
    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"
    assert result.horizon_end_at == T + timedelta(minutes=10)
    assert result.variant_exit_at == T + timedelta(minutes=5)
    assert result.mae_r is not None
    assert result.mfe_r is not None


def test_mae_only_triggers_at_horizon_close():
    candles = [
        candle(0, 98.0, 101.0),
        candle(1, 99.0, 101.0, close=100.5),
    ]

    result = evaluate_variant(
        VARIANTS[0], candles, T,
        ENTRY, STOP, TARGET,
        evaluate_baseline(
            candles, T, T + timedelta(minutes=240),
            ENTRY, STOP, TARGET,
        ),
    )

    assert result.trigger_result == "TRIGGERED"
    assert result.variant_exit_reason == "HORIZON_END_CONDITIONAL_MAE_EXIT"
    assert result.variant_exit_at == T + timedelta(minutes=10)
    assert result.variant_exit_price == 100.5
    assert result.ambiguous_ordering is False
