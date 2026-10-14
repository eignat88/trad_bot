from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.outcomes import evaluate_baseline

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def candle(i, low=99, high=101, close=100):
    o = T + timedelta(minutes=5 * i)
    return ClosedCandle(o, o + timedelta(minutes=5), 100, high, low, close)

def test_stop_loss():
    r = evaluate_baseline([candle(0, low=97)], T, T + timedelta(minutes=240), 100, 97.5, 103)
    assert r.exit_reason == "STOP_LOSS" and r.exit_price == 97.5

def test_take_profit():
    r = evaluate_baseline([candle(0, high=104)], T, T + timedelta(minutes=240), 100, 97.5, 103)
    assert r.exit_reason == "TAKE_PROFIT" and r.exit_price == 103

def test_same_candle_stop_first_ambiguous():
    r = evaluate_baseline([candle(0, low=97, high=104)], T, T + timedelta(minutes=240), 100, 97.5, 103)
    assert r.exit_reason == "AMBIGUOUS_STOP_FIRST" and r.ambiguous is True

def test_timeout_is_completed_boundary():
    r = evaluate_baseline([candle(i) for i in range(48)], T, T + timedelta(minutes=240), 100, 97.5, 103)
    assert r.exit_reason == "FIXED_HORIZON_TIMEOUT" and r.exit_at == T + timedelta(minutes=240)

def test_incomplete_coverage_never_finalizes():
    r = evaluate_baseline([candle(0)], T, T + timedelta(minutes=10), 100, 97.5, 103)
    assert r.status == "INCOMPLETE_COVERAGE"
