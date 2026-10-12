from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v2.candles import ClosedCandle
from app.research.me_r_long_early_mae_v2.outcomes import baseline_outcome
from tools.research.me_rl_v2_exhaustion_filter_execution_reassessment_v1 import (
    classify,
    net_r,
    next_boundary,
)

UTC = timezone.utc


def candle(high, low, close):
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    return ClosedCandle(
        open_at=start,
        close_at=start + timedelta(minutes=5),
        open=100.0,
        high=high,
        low=low,
        close=close,
    )


def test_next_boundary():
    t = datetime(2026, 10, 1, 10, 2, 30, tzinfo=UTC)
    assert next_boundary(t) == datetime(2026, 10, 1, 10, 5, tzinfo=UTC)


def test_next_boundary_exact():
    t = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
    assert next_boundary(t) == t


def test_filter_boundary():
    assert classify("0.4") == "PASS"
    assert classify("0.40001") == "REJECT"
    assert classify(None) == "MISSING"


def test_stop_first_same_candle():
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    result = baseline_outcome(
        [candle(104, 96, 100)],
        start,
        start + timedelta(minutes=5),
        100,
        97.5,
        103,
    )
    assert result is not None
    assert result.exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.ambiguous is True
    assert result.gross_r == -1.0


def test_normal_cost():
    actual = net_r(1.2, 100, 103, 2.5, 0.0005)
    expected = 1.2 - 0.00105 * (100 + 103) / 2.5
    assert abs(actual - expected) < 1e-10
