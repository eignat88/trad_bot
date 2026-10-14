from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.outcomes import evaluate_baseline


T = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
E = 100
SL = 97.5
TP = 103


def candle(i, low=99, high=101):
    opened = T + timedelta(minutes=5 * i)
    return ClosedCandle(
        opened, opened + timedelta(minutes=5),
        100, high, low, 100,
    )


def evaluate(rows, minutes=20):
    return evaluate_baseline(
        rows, T, T + timedelta(minutes=minutes), E, SL, TP
    )


def test_missing_first_candle_before_stop_rejected():
    result = evaluate([candle(1, low=97)])
    assert result.status == "INCOMPLETE_COVERAGE"
    assert result.coverage_reason == "INCOMPLETE_BASELINE_COVERAGE"
    assert result.exit_reason is None


def test_internal_gap_before_target_rejected():
    result = evaluate([candle(0), candle(2, high=104)])
    assert result.status == "INCOMPLETE_COVERAGE"
    assert result.exit_reason is None


def test_contiguous_early_stop_accepted():
    result = evaluate([candle(0), candle(1, low=97)])
    assert result.status == "EVALUATED"
    assert result.exit_reason == "STOP_LOSS"
    assert result.exit_at == T + timedelta(minutes=10)


def test_early_target_before_later_gap_accepted():
    result = evaluate([candle(0, high=104), candle(3)])
    assert result.status == "EVALUATED"
    assert result.exit_reason == "TAKE_PROFIT"
    assert result.exit_at == T + timedelta(minutes=5)


def test_missing_final_candle_prevents_timeout():
    result = evaluate([candle(0), candle(1), candle(2)])
    assert result.status == "INCOMPLETE_COVERAGE"
    assert result.exit_reason is None


def test_complete_window_allows_timeout():
    result = evaluate([candle(i) for i in range(4)])
    assert result.status == "EVALUATED"
    assert result.exit_reason == "FIXED_HORIZON_TIMEOUT"
    assert result.exit_at == T + timedelta(minutes=20)


def test_misaligned_candle_rejected():
    opened = T + timedelta(minutes=1)
    bad = ClosedCandle(
        opened, opened + timedelta(minutes=5),
        100, 101, 99, 100,
    )
    result = evaluate([bad])
    assert result.status == "INCOMPLETE_COVERAGE"


def test_duplicate_timestamp_rejected():
    result = evaluate([candle(0), candle(0)])
    assert result.status == "INCOMPLETE_COVERAGE"
    assert result.coverage_reason == "DUPLICATE_CANDLE_TIMESTAMP"


def test_same_candle_ambiguous_stop_first_preserved():
    result = evaluate([candle(0, low=97, high=104)])
    assert result.status == "EVALUATED"
    assert result.exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.ambiguous is True
    assert result.exit_price == SL
