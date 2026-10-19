"""Isolated synthetic tests for #170 5m SHORT replay (no DB/network)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from tools.research.htf_keylevel_baseline_short_replay_v1 import (
    CANDLE_MS, HOLD_BARS, replay_short,
)

T = int(datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def candles(*, high=100.5, low=99.5, close=100.0):
    return [
        SimpleNamespace(timestamp=T+i*CANDLE_MS, open=100.0,
                        high=high, low=low, close=close)
        for i in range(HOLD_BARS)
    ]


def go(rows, **kwargs):
    return replay_short(signal_time=T, stop=101, target=98,
                        candles=rows, evaluation_asof_ms=T+HOLD_BARS*CANDLE_MS,
                        **kwargs)


def test_timeout_flat_loses_costs():
    r = go(candles())
    assert r.status == "EVALUATED" and r.path_class == "TIMEOUT"
    assert r.gross_r == 0.0
    assert r.net_r_normal < 0 and r.net_r_elevated < r.net_r_normal


def test_short_take_profit_direction_and_costs():
    x = candles()
    x[1] = SimpleNamespace(timestamp=T+CANDLE_MS, open=100, high=100, low=97, close=98)
    r = go(x)
    assert r.path_class == "TP_FIRST" and r.gross_r == 2.0
    assert r.net_r_normal < r.gross_r


def test_short_stop_loss():
    x = candles()
    x[1] = SimpleNamespace(timestamp=T+CANDLE_MS, open=100, high=102, low=99, close=101)
    r = go(x)
    assert r.path_class == "SL_FIRST" and r.gross_r == -1.0


def test_ambiguous_same_bar_is_stop_first():
    x = candles()
    x[1] = SimpleNamespace(timestamp=T+CANDLE_MS, open=100, high=102, low=97, close=100)
    r = go(x)
    assert r.path_class == "SL_FIRST" and r.ambiguous_intrabar
    assert r.gross_r == -1.0


def test_missing_candle_fails_closed():
    x = candles()
    x[4] = SimpleNamespace(timestamp=T+5*CANDLE_MS, open=100, high=100.5, low=99.5, close=100)
    assert go(x).reason == "MISSING_OR_DUPLICATE_CANDLE"


def test_shorter_path_fails_closed():
    assert go(candles()[:-1]).reason == "INCOMPLETE_240M_COVERAGE"


def test_asof_incomplete_fails_closed():
    x = candles()
    r = replay_short(signal_time=T, stop=101, target=98, candles=x,
                     evaluation_asof_ms=T+HOLD_BARS*CANDLE_MS-1)
    assert r.reason == "CANDLE_NOT_CLOSED_ASOF"


def test_invalid_fill_geometry_fails_closed():
    x = candles()
    r = replay_short(signal_time=T, stop=99, target=98, candles=x,
                     evaluation_asof_ms=T+HOLD_BARS*CANDLE_MS)
    assert r.reason == "INVALID_FILL_BASED_SHORT_GEOMETRY"


def test_ill_formed_ohlc_fails_closed():
    x = candles()
    x[3] = SimpleNamespace(timestamp=T+3*CANDLE_MS, open=100, high=99, low=99.5, close=100)
    assert go(x).reason == "INVALID_OHLC"


def test_naive_signal_timestamp_fails_closed():
    r = replay_short(signal_time=datetime(2026, 10, 19, 12),
                     stop=101, target=98, candles=candles(),
                     evaluation_asof_ms=T+HOLD_BARS*CANDLE_MS)
    assert r.reason == "INVALID_INPUT"
