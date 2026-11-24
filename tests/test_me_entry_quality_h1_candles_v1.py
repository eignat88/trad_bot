"""Issue #297 (H1-A.1 / H1-B) — HTF candle validation tests.

Pure, deterministic tests: no PostgreSQL, no Bybit, no network, no clock
dependency.  Scenario coverage: 5, 6, 7, 8, 9, 15, 16, 18, 19, 20 of the
Issue #297 H1-B matrix plus the H1-A.1 contract items.
"""
from __future__ import annotations

import math

import pytest

from app.models import Candle
from app.research.me_entry_quality_h1_v1.htf_candles import (
    MIN_CLOSED_CANDLES,
    REASON_CANDLE_GAP,
    REASON_CONFLICTING_CANDLE,
    REASON_DUPLICATE_CANDLE,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_ASOF,
    REASON_INVALID_OHLC,
    REASON_INVALID_TIMEFRAME,
    REASON_INVALID_TIMESTAMP,
    REASON_INVALID_VOLUME,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    REASON_NO_CLOSED_CANDLES,
    REASON_OK,
    REASON_OUT_OF_ORDER_CANDLE,
    REASON_UNALIGNED_CANDLE,
    TIMEFRAME_1D,
    TIMEFRAME_1D_MS,
    TIMEFRAME_4H,
    TIMEFRAME_4H_MS,
    latest_closed_open_ms,
    validate_htf_candles,
)

DAY_MS = 86_400_000
# 1970-02-01T00:00:00Z — aligned to BOTH the 4H and the 1D UTC grid
BASE_MS = 31 * DAY_MS


def _bar(open_ms, high, low, *, close=None, open_=None, volume=1.0):
    mid = (high + low) / 2.0
    return Candle(
        open_ms,
        mid if open_ is None else open_,
        high,
        low,
        mid if close is None else close,
        volume,
    )


def _series(n, *, tf=TIMEFRAME_4H_MS, asof=None, start_ms=None):
    """n contiguous candles ending at the newest bucket closed at ``asof``."""
    if asof is None:
        asof = BASE_MS + n * tf
    if start_ms is None:
        start_ms = asof - n * tf
    return [_bar(start_ms + i * tf, 12.0 + i, 10.0 + i) for i in range(n)]


# --- H1-A.1 basics --------------------------------------------------------


def test_valid_4h_series_is_accepted():
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert result.ok, (result.reason, result.detail)
    assert result.reason == REASON_OK
    assert result.closed_candles == tuple(candles)
    assert result.dropped_unclosed == ()
    assert result.closed_open_ms == tuple(int(c.timestamp) for c in candles)


def test_valid_1d_series_is_accepted():
    candles = _series(10, tf=TIMEFRAME_1D_MS)
    asof = int(candles[-1].timestamp) + TIMEFRAME_1D_MS
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_1D, evaluation_asof_ms=asof)
    assert result.ok, (result.reason, result.detail)
    assert result.closed_candles == tuple(candles)


def test_1d_candles_align_to_utc_midnight_not_local_day():
    """The 1D grid is EXACTLY 00:00:00 UTC — never a server-local day."""
    from datetime import datetime, timezone

    candles = _series(5, tf=TIMEFRAME_1D_MS)
    for candle in candles:
        open_ms = int(candle.timestamp)
        assert open_ms % TIMEFRAME_1D_MS == 0
        utc = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        assert (utc.hour, utc.minute, utc.second, utc.microsecond) == (0, 0, 0, 0)
    # a local-shifted "midnight" (e.g. 23:00 UTC previous day) is off-grid
    off_grid = _bar(BASE_MS - 3_600_000, 12.0, 10.0)
    result = validate_htf_candles(
        [off_grid], timeframe=TIMEFRAME_1D,
        evaluation_asof_ms=BASE_MS + TIMEFRAME_1D_MS,
    )
    assert not result.ok
    assert result.reason == REASON_UNALIGNED_CANDLE


def test_4h_grid_is_utc_aligned():
    candles = _series(3)
    for candle in candles:
        assert int(candle.timestamp) % TIMEFRAME_4H_MS == 0
    off_grid = _bar(BASE_MS + 60_000, 12.0, 10.0)
    result = validate_htf_candles(
        [off_grid], timeframe=TIMEFRAME_4H, evaluation_asof_ms=BASE_MS + 2 * TIMEFRAME_4H_MS
    )
    assert result.reason == REASON_UNALIGNED_CANDLE


# --- scenario 18: exact close boundary ------------------------------------


def test_asof_boundary_candle_available_exactly_at_close():
    candles = _series(10)
    close_ms = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    at_close = validate_htf_candles(
        candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=close_ms
    )
    assert at_close.ok
    assert at_close.closed_candles[-1] is candles[-1]

    before_close = validate_htf_candles(
        candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=close_ms - 1
    )
    # the candle is not yet closed 1ms before its close: explicit truncation
    assert before_close.ok
    assert before_close.closed_candles == tuple(candles[:-1])
    assert before_close.dropped_unclosed == (candles[-1],)
    assert len(before_close.closed_candles) + len(before_close.dropped_unclosed) == len(candles)


# --- scenarios 5 / 6: unclosed HTF candles --------------------------------


@pytest.mark.parametrize("tf_name,tf_ms", [(TIMEFRAME_4H, TIMEFRAME_4H_MS), (TIMEFRAME_1D, TIMEFRAME_1D_MS)])
def test_unclosed_candle_is_never_used_and_reported(tf_name, tf_ms):
    """Explicit policy: the unclosed candle is truncated, never used."""
    candles = _series(8, tf=tf_ms)
    asof = int(candles[-1].timestamp) + tf_ms - 1  # last candle still open
    result = validate_htf_candles(candles, timeframe=tf_name, evaluation_asof_ms=asof)
    assert result.ok
    assert result.closed_candles == tuple(candles[:-1])
    assert result.dropped_unclosed == (candles[-1],)
    assert all(int(c.timestamp) + tf_ms <= asof for c in result.closed_candles)


@pytest.mark.parametrize("tf_name,tf_ms", [(TIMEFRAME_4H, TIMEFRAME_4H_MS), (TIMEFRAME_1D, TIMEFRAME_1D_MS)])
def test_future_candles_are_never_used(tf_name, tf_ms):
    candles = _series(8, tf=tf_ms)
    asof = int(candles[-1].timestamp) + tf_ms
    future = [
        _bar(int(candles[-1].timestamp) + (i + 1) * tf_ms, 20.0, 18.0)
        for i in range(3)
    ]
    result = validate_htf_candles(
        candles + future, timeframe=tf_name, evaluation_asof_ms=asof
    )
    assert result.ok
    assert result.closed_candles == tuple(candles)
    assert result.dropped_unclosed == tuple(future)


def test_only_unclosed_input_gives_no_closed_candles():
    candles = _series(3)
    asof = int(candles[0].timestamp)  # nothing closed yet
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_NO_CLOSED_CANDLES
    assert result.dropped_unclosed == tuple(candles)


# --- scenario 7: gap ------------------------------------------------------


def test_missing_htf_candle_is_rejected_never_filled():
    candles = _series(10)
    broken = list(candles)
    del broken[4]  # one missing 4H bucket in the middle
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(broken, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_CANDLE_GAP
    assert result.closed_candles == ()  # no silent gap filling


def test_missing_latest_closed_candle_rejected():
    """Only closed candles matter; a missing newest closed bucket is a gap."""
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS  # all closed
    broken = list(candles)
    del broken[-1]  # newest closed bucket missing entirely
    result = validate_htf_candles(broken, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_MISSING_LATEST_CLOSED_CANDLE


# --- scenario 8: duplicates ----------------------------------------------


def test_duplicate_timestamp_identical_is_rejected():
    candles = _series(8)
    with_dup = list(candles) + [candles[3]]
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(with_dup, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_DUPLICATE_CANDLE


def test_duplicate_timestamp_conflicting_is_rejected():
    candles = _series(8)
    conflicting = _bar(int(candles[3].timestamp), 99.0, 98.0)
    with_dup = list(candles) + [conflicting]
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(with_dup, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_CONFLICTING_CANDLE


# --- scenario 9: invalid OHLC / NaN / Inf ---------------------------------


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("high", math.nan, REASON_INVALID_OHLC),
        ("low", math.inf, REASON_INVALID_OHLC),
        ("close", -math.inf, REASON_INVALID_OHLC),
        ("open", 0.0, REASON_INVALID_OHLC),
        ("volume", math.nan, REASON_INVALID_VOLUME),
        ("volume", -1.0, REASON_INVALID_VOLUME),
    ],
)
def test_non_finite_or_invalid_values_rejected(field, value, expected):
    candles = _series(8)
    bad = list(candles)
    base = {"open": 11.0, "high": 12.0, "low": 10.0, "close": 11.0, "volume": 1.0}
    base[field] = value
    bad[2] = Candle(int(candles[2].timestamp), base["open"], base["high"], base["low"], base["close"], base["volume"])
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(bad, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == expected


def test_inconsistent_ohlc_rejected():
    candles = _series(8)
    bad = list(candles)
    # low above open/close and high below them
    bad[2] = Candle(int(candles[2].timestamp), 11.0, 10.5, 11.5, 11.0, 1.0)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(bad, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_INVALID_OHLC


def test_nan_timestamp_rejected():
    candles = _series(8)
    bad = list(candles)
    bad[2] = Candle(float(candles[2].timestamp), 11.0, 12.0, 10.0, 11.0, 1.0)  # float ts
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(bad, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_INVALID_TIMESTAMP


# --- sorting / order ------------------------------------------------------


def test_out_of_order_input_is_rejected_not_reordered():
    candles = _series(8)
    shuffled = list(candles)
    shuffled[2], shuffled[5] = shuffled[5], shuffled[2]
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(shuffled, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_OUT_OF_ORDER_CANDLE


# --- history sufficiency (scenario 4 support) -----------------------------


def test_insufficient_history_rejected():
    candles = _series(3)  # < MIN_CLOSED_CANDLES
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_INSUFFICIENT_HISTORY
    assert MIN_CLOSED_CANDLES == 7


def test_min_closed_candles_is_parameterized():
    candles = _series(3)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    result = validate_htf_candles(
        candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof, min_closed_candles=3
    )
    assert result.ok


def test_stale_source_tail_is_rejected():
    """Incomplete source (missing newest closed bucket) never passes."""
    candles = _series(8)
    asof = int(candles[-1].timestamp) + 3 * TIMEFRAME_4H_MS  # 2 buckets missing at the tail
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_MISSING_LATEST_CLOSED_CANDLE
    relaxed = validate_htf_candles(
        candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof, require_latest_closed=False
    )
    assert relaxed.ok  # the policy is explicit and parameterized, never implicit


# --- parameter contracts --------------------------------------------------


def test_empty_series_rejected():
    result = validate_htf_candles([], timeframe=TIMEFRAME_4H, evaluation_asof_ms=BASE_MS)
    assert not result.ok
    assert result.reason == REASON_EMPTY_SERIES


def test_invalid_timeframe_rejected():
    result = validate_htf_candles([_bar(BASE_MS, 12.0, 10.0)], timeframe="2H", evaluation_asof_ms=BASE_MS)
    assert not result.ok
    assert result.reason == REASON_INVALID_TIMEFRAME


@pytest.mark.parametrize("asof", [1.5, "123", None, -1, True, float("nan")])
def test_invalid_asof_rejected(asof):
    candles = _series(8)
    result = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert not result.ok
    assert result.reason == REASON_INVALID_ASOF


def test_latest_closed_open_ms_boundary():
    # asof exactly on a bucket close -> that bucket is the newest closed one
    assert latest_closed_open_ms(BASE_MS + TIMEFRAME_4H_MS, TIMEFRAME_4H_MS) == BASE_MS
    # 1ms before the close -> previous bucket
    assert latest_closed_open_ms(BASE_MS + TIMEFRAME_4H_MS - 1, TIMEFRAME_4H_MS) == BASE_MS - TIMEFRAME_4H_MS
    assert latest_closed_open_ms(BASE_MS + TIMEFRAME_1D_MS, TIMEFRAME_1D_MS) == BASE_MS


# --- scenario 15 / 16: determinism and future-append invariance -----------


def test_rerun_is_identical():
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    first = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    second = validate_htf_candles(list(candles), timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert first == second


def test_appending_future_candles_keeps_past_asof_result():
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    before = validate_htf_candles(candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    extended = list(candles) + [
        _bar(int(candles[-1].timestamp) + (i + 1) * TIMEFRAME_4H_MS, 30.0, 28.0)
        for i in range(5)
    ]
    after = validate_htf_candles(extended, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof)
    assert after.ok
    assert after.closed_candles == before.closed_candles
    assert after.reason == before.reason


# --- scenario 20: system timezone independence ---------------------------


def _boom(*args, **kwargs):  # pragma: no cover - must never be called
    raise AssertionError("local-timezone API used by a pure module")


def test_result_does_not_depend_on_system_timezone(monkeypatch):
    import time

    from app.research.me_entry_quality_h1_v1 import classify_combined

    asof = BASE_MS + 12 * DAY_MS  # UTC midnight: both grids close exactly here
    candles_4h = _series(12, asof=asof)
    candles_1d = _series(12, tf=TIMEFRAME_1D_MS, asof=asof)
    baseline = classify_combined(candles_4h, candles_1d, evaluation_asof_ms=asof)

    # hostile environment: any localtime / mktime use would explode
    monkeypatch.setattr(time, "localtime", _boom)
    monkeypatch.setattr(time, "mktime", _boom)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati+14")
    if hasattr(time, "tzset"):  # POSIX only
        time.tzset()
        try:
            monkeypatch.setenv("TZ", "UTC-12")
            time.tzset()
            assert classify_combined(candles_4h, candles_1d, evaluation_asof_ms=asof) == baseline
            monkeypatch.setenv("TZ", "UTC")
            time.tzset()
            assert classify_combined(candles_4h, candles_1d, evaluation_asof_ms=asof) == baseline
        finally:
            monkeypatch.setenv("TZ", "UTC")
            time.tzset()
    assert classify_combined(candles_4h, candles_1d, evaluation_asof_ms=asof) == baseline


def test_unclosed_corrupt_future_ohlc_cannot_change_past_asof_result():
    """A future bad candle must not repaint a valid historical ASOF result."""
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    baseline = validate_htf_candles(
        candles, timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof
    )
    assert baseline.ok
    future = _bar(asof, float("nan"), 10.0)
    observed = validate_htf_candles(
        candles + [future], timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof
    )
    assert observed.ok
    assert observed.closed_candles == baseline.closed_candles
    assert observed.dropped_unclosed == (future,)


def test_unclosed_off_grid_future_candle_cannot_repaint_past():
    candles = _series(10)
    asof = int(candles[-1].timestamp) + TIMEFRAME_4H_MS
    future = _bar(asof + 60_000, 13.0, 11.0)
    result = validate_htf_candles(
        candles + [future], timeframe=TIMEFRAME_4H, evaluation_asof_ms=asof
    )
    assert result.ok
    assert result.closed_candles == tuple(candles)
    assert result.dropped_unclosed == (future,)
