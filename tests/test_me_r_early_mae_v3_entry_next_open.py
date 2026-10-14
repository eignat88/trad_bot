from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.entry_policy import decide_next_open

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def c(i, open_price=100.0):
    o = T + timedelta(minutes=5 * i)
    return ClosedCandle(o, o + timedelta(minutes=5), open_price, open_price + 1, open_price - 1, open_price)

def decision(candles, eligible=T, asof=T + timedelta(minutes=10), detected=T, activation=T):
    return decide_next_open(candles, eligible, detected, activation, asof)

def test_exact_boundary_uses_next_open_only():
    d = decision([c(1)], eligible=T, asof=T + timedelta(minutes=10))
    assert d.state == "ENTRY_FILLED" and d.entry.entry_at == T + timedelta(minutes=5)
    assert d.entry.entry_price == 100

def test_mid_candle_availability_is_not_retroactive():
    d = decision([c(1)], eligible=T + timedelta(seconds=1), asof=T + timedelta(minutes=10))
    assert d.state == "ENTRY_FILLED" and d.entry.entry_at == T + timedelta(minutes=5)

def test_missing_entry_candle_is_incomplete_not_zero_fill():
    d = decision([], eligible=T, asof=T + timedelta(minutes=10))
    assert d.state == "INCOMPLETE_COVERAGE"

def test_duplicate_entry_candle_is_incomplete():
    d = decision([c(1), c(1)], eligible=T, asof=T + timedelta(minutes=10))
    assert d.state == "INCOMPLETE_COVERAGE"

def test_expiry_after_entry_window_is_not_filled():
    d = decision([c(i) for i in range(48)], eligible=T, asof=T + timedelta(minutes=241))
    assert d.state == "ENTRY_NOT_FILLED" and d.reason == "EXPIRED_AFTER_ENTRY_WINDOW"

def test_incomplete_coverage_retry_remains_pending():
    d = decision([c(1)], eligible=T, asof=T + timedelta(minutes=10))
    assert d.state == "ENTRY_FILLED"
    d = decision([], eligible=T, asof=T + timedelta(minutes=10))
    assert d.state == "INCOMPLETE_COVERAGE"
