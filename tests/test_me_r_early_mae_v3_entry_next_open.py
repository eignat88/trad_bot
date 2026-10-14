from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.entry_policy import evaluate_next_open, next_boundary

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def c(i):
    o = T + timedelta(minutes=5 * i)
    return ClosedCandle(o, o + timedelta(minutes=5), 100, 101, 99, 100)

def test_exact_boundary_next_open():
    assert next_boundary(T) == T + timedelta(minutes=5)
    entry = evaluate_next_open([c(1)], T, T + timedelta(minutes=240), T + timedelta(minutes=6))
    assert entry is not None
    assert entry.entry_at == T + timedelta(minutes=5)
    assert entry.entry_price == 100

def test_no_retroactive_entry():
    assert evaluate_next_open([c(0)], T + timedelta(seconds=1), T + timedelta(minutes=240), T) is None

def test_expiry_rejects_late_boundary():
    assert evaluate_next_open([c(48)], T, T + timedelta(minutes=240), T + timedelta(minutes=241)) is None

def test_missing_entry_candle_fails_closed():
    assert evaluate_next_open([], T, T + timedelta(minutes=240), T + timedelta(minutes=10)) is None
