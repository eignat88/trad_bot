from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.outcomes import evaluate_baseline
from app.research.me_r_long_early_mae_v3.protocol import VARIANTS
from app.research.me_r_long_early_mae_v3.variants import evaluate_variant


T = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
ENTRY = 100.0
STOP = 97.5
TARGET = 103.0


def candle(i, low=99.0, high=101.0, close=100.0):
    opened = T + timedelta(minutes=5 * i)
    return ClosedCandle(
        opened, opened + timedelta(minutes=5),
        100.0, high, low, close,
    )


def test_early_stop_preserves_path_geometry_and_original_horizon():
    candles = [
        candle(0, low=99.0, high=101.0),
        candle(1, low=97.0, high=102.0),
    ]
    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )
    assert baseline.exit_reason == "STOP_LOSS"
    assert baseline.exit_at == T + timedelta(minutes=10)

    variant = VARIANTS[0]
    result = evaluate_variant(
        variant, candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_exit_at == baseline.exit_at
    assert result.variant_exit_price == baseline.exit_price
    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"
    assert result.horizon_end_at == T + timedelta(
        minutes=variant.horizon_minutes
    )

    assert result.window_low == 97.0
    assert result.window_high == 102.0
    assert result.mae_r == 1.2
    assert result.mfe_r == 0.8

    assert result.variant_gross_r == baseline.gross_r
    assert result.net_r_normal == baseline.net_r_normal
    assert result.net_r_elevated == baseline.net_r_elevated


def test_early_target_preserves_path_geometry():
    candles = [
        candle(0, low=99.5, high=101.5),
        candle(1, low=99.0, high=104.0),
    ]
    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )
    assert baseline.exit_reason == "TAKE_PROFIT"

    result = evaluate_variant(
        VARIANTS[0], candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.window_low == 99.0
    assert result.window_high == 104.0
    assert result.mae_r == 0.4
    assert result.mfe_r == 1.6
    assert result.horizon_end_at == T + timedelta(
        minutes=VARIANTS[0].horizon_minutes
    )


def test_ambiguous_baseline_keeps_geometry_and_ambiguity():
    candles = [
        candle(0, low=97.0, high=104.0),
    ]
    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )
    assert baseline.ambiguous is True

    result = evaluate_variant(
        VARIANTS[0], candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_status == "AMBIGUOUS"
    assert result.ambiguous_ordering is True
    assert result.variant_exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.window_low == 97.0
    assert result.window_high == 104.0
    assert result.horizon_end_at == T + timedelta(
        minutes=VARIANTS[0].horizon_minutes
    )


def test_stop_before_horizon_preserves_complete_geometry():
    candles = [candle(0, low=97.0, high=102.0)]
    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )
    assert baseline.exit_at == T + timedelta(minutes=5)

    result = evaluate_variant(
        VARIANTS[0], candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_status == "AMBIGUOUS"
    assert result.ambiguous_ordering is True
    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"
    assert result.horizon_end_at == T + timedelta(minutes=10)
    assert result.variant_exit_at == baseline.exit_at
    assert result.window_low == 97.0
    assert result.window_high == 102.0
    assert result.mae_r == 1.2
    assert result.mfe_r == 0.8
    assert result.variant_gross_r == baseline.gross_r
    assert result.net_r_normal == baseline.net_r_normal


def test_target_before_horizon_preserves_complete_geometry():
    candles = [candle(0, low=99.0, high=104.0)]
    baseline = evaluate_baseline(
        candles, T, T + timedelta(minutes=240),
        ENTRY, STOP, TARGET,
    )
    assert baseline.exit_at == T + timedelta(minutes=5)

    result = evaluate_variant(
        VARIANTS[0], candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.trigger_result == "BASELINE_SL_TP_PRECEDENCE"
    assert result.variant_exit_at == baseline.exit_at
    assert result.horizon_end_at == T + timedelta(minutes=10)
    assert result.window_low == 99.0
    assert result.window_high == 104.0
    assert result.mae_r == 0.4
    assert result.mfe_r == 1.6
    assert result.variant_gross_r == baseline.gross_r


def test_early_exit_missing_candle_does_not_finalize():
    from app.research.me_r_long_early_mae_v3.outcomes import outcome

    # Baseline exit на 10-й минуте, но первая свеча отсутствует.
    baseline = outcome(
        ENTRY, STOP, T + timedelta(minutes=10),
        STOP, "STOP_LOSS",
    )
    candles = [candle(1, low=97.0)]

    result = evaluate_variant(
        VARIANTS[0], candles, T, ENTRY, STOP, TARGET, baseline,
    )

    assert result.variant_status == "INCOMPLETE_COVERAGE"
    assert result.coverage_reason is not None
    assert result.mae_r is None
    assert result.variant_exit_at is None
