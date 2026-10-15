from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import Candle
from app.scanners.swing_engine import find_swing_highs
from app.research.me_r_long_early_mae_v3.detector import (
    detect_v1_closed,
    v1_reference_candidate,
)
from app.research.me_r_long_early_mae_v3.closed_context import (
    to_frozen_v1_candles,
)

ASOF = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)


def candle(start, o, h, l, c, volume=1000.0):
    return Candle(
        timestamp=int(start.timestamp() * 1000),
        open=o, high=h, low=l, close=c, volume=volume,
    )


def positive_source(asof=ASOF):
    five = []
    for i in range(40):
        t = asof - timedelta(minutes=5 * (40 - i))
        if i == 39:
            five.append(candle(t, 105.25, 106.0, 104.9, 105.2))
        else:
            five.append(candle(t, 100.5, 101.0, 100.0, 100.4))

    fifteen = []
    for i in range(40):
        t = asof - timedelta(minutes=15 * (40 - i))
        high = 101.0 + i * 0.005
        if i == 10:
            high = 105.0
        if i == 25:
            high = 107.0
        fifteen.append(candle(t, 100.5, high, 99.5, 100.4))

    hourly = []
    for i in range(40):
        t = asof - timedelta(hours=40 - i)
        close = 100.0 + i * 0.5
        hourly.append(candle(
            t, close - 0.2, close + 0.2, close - 0.4, close,
        ))

    return SimpleNamespace(
        symbol="BTCUSDT",
        candles_5m=tuple(five),
        candles_15m=tuple(fifteen),
        candles_1h=tuple(hourly),
        source_fetch_started_at=asof,
    )


def detect_pair(src=None, asof=ASOF):
    if src is None:
        src = positive_source(asof)
    result = detect_v1_closed(src, asof, asof)
    assert result.status == "SIGNAL", (result.status, result.reason)
    assert result.context is not None
    assert result.candidate is not None

    reference = v1_reference_candidate(result.context)
    assert reference is not None, "Frozen V1 did not produce SIGNAL"
    return result, reference


def test_positive_signal_and_swing_geometry():
    result, reference = detect_pair()
    ctx = result.context

    swings = find_swing_highs(
        to_frozen_v1_candles(ctx.candles_15m), 5
    )
    assert len(swings) >= 2
    assert swings[-2].price == pytest.approx(105.0)

    assert ctx.indicators.rsi >= 65
    assert max(c.high for c in ctx.candles_5m[-5:]) == 106.0

    last = ctx.candles_5m[-1]
    assert last.close <= last.open
    assert last.close <= 105.0 * 1.003
    assert (last.open - last.close) / (last.high - last.low) <= 0.7

    assert result.candidate.direction == reference.direction


def test_positive_trade_geometry_parity():
    result, reference = detect_pair()
    a, b = result.candidate, reference

    for field in (
        "reference_price",
        "entry_zone_low",
        "entry_zone_high",
        "invalidation_price",
        "target_1",
    ):
        assert getattr(a, field) == pytest.approx(
            getattr(b, field)
        ), field

    assert a.target_2 == b.target_2
    assert a.market_regime == b.market_regime
    assert a.state == b.state
    assert a.symbol == b.symbol

    assert a.invalidation_price == pytest.approx(105.2 * 0.975)
    assert a.target_1 == pytest.approx(105.2 * 1.03)


def test_positive_common_features_parity():
    result, reference = detect_pair()
    actual = dict(result.candidate.features)
    expected = dict(reference.features)

    # V3-only provenance field: explicitly allowed.
    assert set(actual) - set(expected) == {"ruleset"}
    assert not (set(expected) - set(actual))

    for key in expected:
        assert actual[key] == pytest.approx(expected[key]) \
            if isinstance(expected[key], (int, float)) \
            else actual[key] == expected[key], key

    assert actual["ruleset"] == "ORIGINAL_V1_NO_RSI_DELTA_FILTER"


def test_positive_closed_candle_and_identity():
    first, _ = detect_pair()
    second, _ = detect_pair()

    assert first.source["source_setup_id"] == second.source["source_setup_id"]
    assert first.source["source_candle_open_at"] == (
        ASOF - timedelta(minutes=5)
    )
    assert first.source["source_candle_close_at"] == ASOF
    assert first.context.candles_15m[-1].close_at == ASOF
    assert first.context.candles_1h[-1].close_at == ASOF
    assert first.source["source_available_at"] <= first.candidate.detected_at


def test_future_forming_candle_rejected():
    src = positive_source()
    src.candles_5m += (
        candle(ASOF, 105.2, 200.0, 1.0, 180.0),
    )
    result = detect_v1_closed(src, ASOF, ASOF)
    assert result.status == "REJECTED"
    assert "FUTURE" in result.reason or "FORMING" in result.reason


def test_boundary_rsi_v1_v3_parity():
    from dataclasses import replace

    base, _ = detect_pair()

    for rsi, expected_signal in (
        (64.999, False),
        (65.0, True),
        (65.001, True),
    ):
        ctx = replace(
            base.context,
            indicators=replace(base.context.indicators, rsi=rsi),
        )

        from app.research.me_r_long_early_mae_v3.detector import (
            _detect_original_v1,
        )

        actual = _detect_original_v1(ctx)
        reference = v1_reference_candidate(ctx)

        assert (actual is not None) == expected_signal, rsi
        assert (reference is not None) == expected_signal, rsi


@pytest.mark.parametrize(
    "close,expected_signal",
    [
        (105.0 * 1.003 - 0.0001, True),
        (105.0 * 1.003, True),
        (105.0 * 1.003 + 0.0001, False),
    ],
)
def test_return_threshold_boundary(close, expected_signal):
    from dataclasses import replace
    from app.research.me_r_long_early_mae_v3.detector import (
        _detect_original_v1,
    )

    base, _ = detect_pair()
    ctx = base.context
    last = ctx.candles_5m[-1]

    modified = replace(
        last,
        open=close + 0.05,
        high=max(last.high, close + 0.1),
        low=min(last.low, close - 0.1),
        close=close,
    )
    ctx = replace(
        ctx,
        candles_5m=ctx.candles_5m[:-1] + (modified,),
    )

    actual = _detect_original_v1(ctx)
    reference = v1_reference_candidate(ctx)

    assert (actual is not None) == expected_signal
    assert (reference is not None) == expected_signal


def test_hour_boundary_positive_signal():
    result, reference = detect_pair()

    assert result.status == "SIGNAL"
    assert reference is not None
    assert result.source["source_candle_close_at"].minute == 0
    assert result.context.candles_1h[-1].close_at == ASOF
    assert result.context.candles_15m[-1].close_at == ASOF
    assert result.context.candles_5m[-1].close_at == ASOF


def test_invalid_source_is_fail_closed():
    src = positive_source()
    src.candles_5m = src.candles_5m[:-2] + (
        src.candles_5m[-1],
    )

    result = detect_v1_closed(src, ASOF, ASOF)

    assert result.status == "REJECTED"
    assert "GAPPED" in result.reason


@pytest.mark.parametrize(
    "ratio",
    [0.699, 0.700, 0.701],
)
def test_body_ratio_boundary(ratio):
    from dataclasses import replace
    from app.research.me_r_long_early_mae_v3.detector import (
        _detect_original_v1,
    )

    base, _ = detect_pair()
    ctx = base.context
    last = ctx.candles_5m[-1]

    modified = replace(
        last,
        open=105.2 + ratio,
        high=106.2,
        low=105.2,
        close=105.2,
    )
    ctx = replace(ctx, candles_5m=ctx.candles_5m[:-1] + (modified,))

    actual_ratio = abs(modified.close - modified.open) / (
        modified.high - modified.low
    )
    expected = actual_ratio <= 0.7

    actual = _detect_original_v1(ctx)
    reference = v1_reference_candidate(ctx)

    assert (actual is not None) == expected, (
        ratio, actual_ratio
    )
    assert (reference is not None) == expected, (
        ratio, actual_ratio
    )
    assert (actual is not None) == (reference is not None)
    print(f"requested={ratio}, actual={actual_ratio!r}, signal={expected}")


@pytest.mark.parametrize(
    "recent_high,expected",
    [
        (105.0, False),
        (105.0001, True),
        (106.0, True),
    ],
)
def test_swing_high_break_boundary(recent_high, expected):
    from dataclasses import replace
    from app.research.me_r_long_early_mae_v3.detector import (
        _detect_original_v1,
    )

    base, _ = detect_pair()
    ctx = base.context
    five = list(ctx.candles_5m)

    for i in range(-5, 0):
        c = five[i]
        five[i] = replace(
            c,
            high=max(c.open, c.close, recent_high),
        )

    # Preserve a valid bearish signal candle.
    last = five[-1]
    five[-1] = replace(
        last,
        open=104.9,
        close=104.8,
        high=recent_high,
        low=104.5,
    )

    ctx = replace(ctx, candles_5m=tuple(five))

    actual = _detect_original_v1(ctx)
    reference = v1_reference_candidate(ctx)

    assert (actual is not None) == expected
    assert (reference is not None) == expected


def source_extended_to(asof):
    """Extend a deterministic closed source without changing its history."""
    src = positive_source()

    for name, minutes in (
        ("candles_5m", 5),
        ("candles_15m", 15),
        ("candles_1h", 60),
    ):
        items = list(getattr(src, name))
        step = timedelta(minutes=minutes)

        next_open = datetime.fromtimestamp(
            items[-1].timestamp / 1000, timezone.utc
        ) + step

        while next_open + step <= asof:
            items.append(candle(
                next_open, 105.25, 106.0, 104.9, 105.2
            ))
            next_open += step

        setattr(src, name, tuple(items))

    src.source_fetch_started_at = asof
    return src


@pytest.mark.parametrize(
    "offset,minutes_5m,minutes_15m,hour_1h",
    [
        (5, 5, 0, 12),
        (10, 10, 0, 12),
        (15, 15, 15, 12),
        (55, 55, 45, 12),
        (60, 0, 0, 13),
    ],
)
def test_completed_timeframe_cutoffs(
    offset, minutes_5m, minutes_15m, hour_1h
):
    from app.research.me_r_long_early_mae_v3.closed_context import (
        build_closed_context,
    )

    asof = ASOF + timedelta(minutes=offset)
    ctx = build_closed_context(source_extended_to(asof), asof, asof)

    assert ctx.candles_5m[-1].close_at == asof
    assert ctx.candles_15m[-1].close_at.minute == minutes_15m
    assert ctx.candles_1h[-1].close_at.hour == hour_1h
    assert ctx.candles_1h[-1].close_at.minute == 0

    for name in ("candles_5m", "candles_15m", "candles_1h"):
        assert all(
            c.close_at <= asof for c in getattr(ctx, name)
        )


@pytest.mark.parametrize("offset_seconds", [-1, 0, 1, 299, 300, 301])
def test_detection_availability_boundary(offset_seconds):
    from app.research.me_r_long_early_mae_v3.temporal import (
        validate_detection_temporal,
    )

    t = ASOF
    detected = t + timedelta(seconds=offset_seconds)

    if 0 <= offset_seconds <= 300:
        result = validate_detection_temporal(
            t, detected, detected
        )
        assert result.source_available_at <= detected
    else:
        with pytest.raises(ValueError):
            validate_detection_temporal(t, detected, detected)


def test_fetch_completed_after_asof_rejected():
    src = positive_source()
    result = detect_v1_closed(
        src, ASOF, ASOF + timedelta(seconds=1)
    )
    assert result.status == "REJECTED"
    assert "FETCH_COMPLETED_AFTER_ASOF" in result.reason


def test_fetch_completed_before_started_rejected():
    src = positive_source()
    result = detect_v1_closed(
        src, ASOF, ASOF - timedelta(seconds=1)
    )
    assert result.status == "REJECTED"
    assert "FETCH_COMPLETED_BEFORE_START" in result.reason


def test_future_ohlc_cannot_change_closed_signal():
    baseline, reference = detect_pair()
    src = positive_source()

    src.candles_5m += (
        candle(
            ASOF,
            105.2,
            100000.0,
            0.01,
            90000.0,
        ),
    )

    rejected = detect_v1_closed(src, ASOF, ASOF)

    assert baseline.status == "SIGNAL"
    assert reference is not None
    assert rejected.status == "REJECTED"
    assert rejected.candidate is None
    assert rejected.source is None
    assert "FUTURE" in rejected.reason or "FORMING" in rejected.reason


def test_positive_measured_fetch_timeline():
    from app.research.me_r_long_early_mae_v3.closed_context import (
        build_closed_context,
    )

    close_at = ASOF
    fetch_started = ASOF + timedelta(seconds=1)
    fetch_completed = ASOF + timedelta(seconds=2)
    detected_at = ASOF + timedelta(seconds=3)

    src = positive_source()
    src.source_fetch_started_at = fetch_started

    ctx = build_closed_context(src, detected_at, fetch_completed)

    result = detect_v1_closed(
        src,
        detected_at,
        fetch_completed,
    )

    assert result.status == "SIGNAL", (result.status, result.reason)
    assert result.source is not None

    assert close_at <= fetch_started
    assert fetch_started <= fetch_completed
    assert fetch_completed <= detected_at

    assert ctx.source_fetch_started_at == fetch_started
    assert ctx.source_fetch_completed_at == fetch_completed

    assert result.source["source_fetch_started_at"] == fetch_started
    assert result.source["source_fetch_completed_at"] == fetch_completed
    assert result.source["source_available_at"] == fetch_completed
    assert result.source["detected_at"] == detected_at
