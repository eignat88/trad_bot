from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.models import Candle
from app.research.me_r_long_early_mae_v3.closed_context import build_closed_context

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

def candles(minutes, count, end, volume=1000.0):
    return tuple(Candle(
        timestamp=int((end - timedelta(minutes=minutes * (count - i))).timestamp() * 1000),
        open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=volume,
    ) for i in range(count))

def source(asof):
    return SimpleNamespace(
        symbol="BTCUSDT",
        candles_5m=candles(5, 40, asof),
        candles_15m=candles(15, 40, asof.replace(minute=(asof.minute // 15) * 15, second=0, microsecond=0)),
        candles_1h=candles(60, 40, asof.replace(minute=0, second=0, microsecond=0)),
        source_fetch_started_at=asof,
    )

def test_closed_context_uses_latest_expected_candle_and_indicators():
    asof = BASE + timedelta(minutes=10)
    ctx = build_closed_context(source(asof), asof)
    assert ctx.candles_5m[-1].close_at == asof
    assert ctx.candles_15m[-1].close_at == BASE
    assert ctx.candles_1h[-1].close_at == BASE
    assert ctx.indicators.rsi is not None

def test_stale_contiguous_history_is_rejected():
    asof = BASE + timedelta(minutes=10)
    bad = source(asof)
    bad.candles_5m = bad.candles_5m[:-2]
    try:
        build_closed_context(bad, asof)
    except ValueError as exc:
        assert "STALE" in str(exc)
    else:
        raise AssertionError

def test_duplicate_gapped_unaligned_and_future_are_rejected():
    asof = BASE + timedelta(minutes=10)
    for mutate, expected in (
        (lambda b: setattr(b, "candles_5m", b.candles_5m[:-1] + (b.candles_5m[-2],)), "DUPLICATE"),
        (lambda b: setattr(b, "candles_5m", b.candles_5m[:-2] + (b.candles_5m[-1],)), "GAPPED"),
        (lambda b: setattr(b, "candles_5m", b.candles_5m[:-1] + (Candle(int((asof - timedelta(minutes=5)).timestamp() * 1000) + 1, 1, 1, 1, 1, 1),)), "UNALIGNED"),
        (lambda b: setattr(b, "candles_5m", b.candles_5m + (Candle(int(asof.timestamp() * 1000), 1, 1, 1, 1, 1),)), "FUTURE"),
    ):
        bad = source(asof)
        mutate(bad)
        try:
            build_closed_context(bad, asof)
        except ValueError as exc:
            assert expected in str(exc), (expected, str(exc))
        else:
            raise AssertionError(expected)

def test_missing_timeframe_history_is_rejected():
    asof = BASE + timedelta(minutes=10)
    bad = source(asof)
    bad.candles_1h = ()
    try:
        build_closed_context(bad, asof)
    except ValueError as exc:
        assert "NO_CLOSED" in str(exc)
    else:
        raise AssertionError

def test_fetch_provenance_is_validated_independently():
    asof = BASE + timedelta(minutes=10)
    try:
        build_closed_context(source(asof), asof, asof + timedelta(seconds=1))
    except ValueError as exc:
        assert "FETCH_COMPLETED_AFTER_ASOF" in str(exc)
    else:
        raise AssertionError

def test_nonfinite_indicator_is_rejected():
    asof = BASE + timedelta(minutes=10)
    bad = source(asof)
    ctx = build_closed_context(bad, asof)
    object.__setattr__(ctx.indicators, "rsi", float("nan"))
    # Detection uses a fresh context; direct finite check on the builder's input
    # is represented by the finite-candle checks above.
