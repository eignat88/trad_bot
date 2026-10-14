from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketLevels
from app.research.me_r_long_early_mae_v3.closed_context import build_closed_context

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

def candles(minutes, count, end):
    return tuple(Candle(
        timestamp=int((end - timedelta(minutes=minutes * (count - i))).timestamp() * 1000),
        open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000,
    ) for i in range(count))

def source():
    asof = BASE + timedelta(minutes=10)
    return SimpleNamespace(
        symbol="BTCUSDT",
        candles_5m=candles(5, 40, BASE + timedelta(minutes=10)),
        candles_15m=candles(15, 40, BASE + timedelta(minutes=15)),
        candles_1h=candles(60, 40, BASE + timedelta(hours=1)),
        source_fetch_started_at=asof,
    )

def test_closed_context_excludes_unclosed_and_recomputes_indicators():
    ctx = build_closed_context(source(), BASE + timedelta(minutes=10))
    assert ctx.candles_5m[-1].close_at == BASE + timedelta(minutes=10)
    assert ctx.candles_15m[-1].close_at == BASE + timedelta(minutes=0)
    assert ctx.candles_1h[-1].close_at == BASE
    assert ctx.indicators.rsi is not None

def test_future_or_unaligned_data_fails_closed():
    bad = source()
    bad.candles_5m = bad.candles_5m[:-1] + (Candle(
        timestamp=int((BASE + timedelta(minutes=5, seconds=1)).timestamp() * 1000), open=1, high=1,
        low=1, close=1, volume=1,
    ),)
    try:
        build_closed_context(bad, BASE + timedelta(minutes=10))
    except ValueError as exc:
        assert "UNALIGNED" in str(exc)
    else:
        raise AssertionError

def test_gapped_source_fails_closed():
    bad = source()
    bad.candles_1h = bad.candles_1h[:-2] + (bad.candles_1h[-1],)
    try:
        build_closed_context(bad, BASE + timedelta(hours=1))
    except ValueError as exc:
        assert "GAPPED" in str(exc)
    else:
        raise AssertionError
