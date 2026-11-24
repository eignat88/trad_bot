"""Issue #297 (H1-A.2 / H1-B) — causal swing structure and BOS tests.

Pure, deterministic tests: no PostgreSQL, no Bybit, no network.
Scenario coverage: 10, 11, 12, 13 of the Issue #297 H1-B matrix plus the
H1-A.2 contract items (formed/confirmed timestamps, equal-extremum policy,
deterministic tie-breaking, lookback parameterization).
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.me_entry_quality_h1_v1.structure import (
    BOS_BEARISH,
    BOS_BULLISH,
    EQUAL_REL_TOL,
    RELATION_EQUAL,
    RELATION_HIGHER,
    RELATION_LOWER,
    SWING_HIGH,
    SWING_LOW,
    SWING_LOOKBACK_DEFAULT,
    Swing,
    detect_bos_events,
    detect_swings,
    pair_relation,
    usable_swings,
)

TF = 14_400_000  # 4H
BASE_MS = 31 * 86_400_000  # 1970-02-01T00:00:00Z, 4H- and 1D-aligned


def _zigzag(points, n):
    """Deterministic piecewise-linear values through (index, value) points."""
    xs = [p[0] for p in points]
    ys = [float(p[1]) for p in points]
    out = []
    for i in range(n):
        if i <= xs[0]:
            out.append(ys[0])
            continue
        if i >= xs[-1]:
            out.append(ys[-1])
            continue
        for j in range(len(xs) - 1):
            if xs[j] <= i <= xs[j + 1]:
                span = xs[j + 1] - xs[j]
                t = (i - xs[j]) / span
                out.append(ys[j] + (ys[j + 1] - ys[j]) * t)
                break
    return out


def _bar(open_ms, high, low, *, close=None):
    mid = (high + low) / 2.0
    return Candle(open_ms, mid, high, low, mid if close is None else close, 1.0)


def _bars(high_pts, low_pts, n):
    highs = _zigzag(high_pts, n)
    lows = _zigzag(low_pts, n)
    assert all(lo <= hi for hi, lo in zip(highs, lows)), "fixture OHLC must be feasible"
    return [_bar(BASE_MS + i * TF, highs[i], lows[i]) for i in range(n)]


# canonical fixture (k=3): exactly 2 swing highs (24@3, 27@10) and
# 2 swing lows (6@6, 8@13); HH + HL -> BULLISH structure
N = 17
HIGH_PTS = [(0, 20), (3, 24), (6, 21), (10, 27), (13, 22), (16, 23)]
LOW_PTS = [(0, 11), (6, 6), (10, 10), (13, 8), (16, 9)]


def _fixture():
    return _bars(HIGH_PTS, LOW_PTS, N)


def _close_of(index):
    return BASE_MS + index * TF + TF


# --- formed / confirmed timestamps ----------------------------------------


def test_swing_formed_and_confirmed_timestamps():
    candles = _fixture()
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    by_key = {(s.swing_type, s.index): s for s in swings}

    assert set(by_key) == {
        (SWING_HIGH, 3), (SWING_HIGH, 10), (SWING_LOW, 6), (SWING_LOW, 13),
    }

    high_3 = by_key[(SWING_HIGH, 3)]
    assert high_3.price == 24.0
    assert high_3.formed_at_ms == BASE_MS + 3 * TF        # extremum candle open
    assert high_3.confirmed_at_ms == _close_of(6)          # close of the 3rd right candle

    low_13 = by_key[(SWING_LOW, 13)]
    assert low_13.price == 8.0
    assert low_13.formed_at_ms == BASE_MS + 13 * TF
    assert low_13.confirmed_at_ms == _close_of(16)

    for swing in swings:
        assert swing.confirmed_at_ms == swing.formed_at_ms + 3 * TF + TF


# --- scenarios 10 / 11: causality gate ------------------------------------


def test_formed_but_unconfirmed_swing_is_not_usable():
    """Scenario 10: a swing formed later than the gate is invisible."""
    candles = _fixture()
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    # just before the first confirmation closes: nothing usable
    assert usable_swings(swings, _close_of(6) - 1) == ()
    # the forming LOW@13 exists geometrically but is not usable yet
    early = usable_swings(swings, _close_of(15))
    assert all(s.confirmed_at_ms <= _close_of(15) for s in early)
    assert (SWING_LOW, 13) not in {(s.swing_type, s.index) for s in early}


def test_swing_used_only_after_confirmation():
    """Scenario 11: usable exactly from confirmed_at_ms (inclusive)."""
    candles = _fixture()
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    high_10 = next(s for s in swings if s.swing_type == SWING_HIGH and s.index == 10)
    assert high_10 not in usable_swings(swings, high_10.confirmed_at_ms - 1)
    assert high_10 in usable_swings(swings, high_10.confirmed_at_ms)
    assert len(usable_swings(swings, _close_of(16))) == 4


# --- equal-extremum policy ------------------------------------------------


def test_equal_high_plateau_collapses_to_one_representative():
    # a 2-candle plateau of equal highs at indices 5 and 6 (k=3)
    high_pts = [(0, 10), (2, 14), (4, 16), (5, 20), (6, 20), (8, 15), (10, 12), (12, 14)]
    low_pts = [(0, 6), (6, 4), (12, 5)]
    candles = _bars(high_pts, low_pts, 13)
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    highs = [s for s in swings if s.swing_type == SWING_HIGH]
    plateau = [s for s in highs if s.price == 20.0]
    assert len(plateau) == 1  # ONE formation level, ONE confirmation
    assert plateau[0].index == 5  # deterministic: first qualifying member
    assert plateau[0].formed_at_ms == BASE_MS + 5 * TF


def test_independent_equal_price_swings_are_kept_separately():
    # two separate single-candle peaks at the same price, split by lower prints
    high_pts = [(0, 10), (3, 20), (6, 12), (9, 20), (12, 10), (15, 8)]
    low_pts = [(0, 5), (15, 4)]
    candles = _bars(high_pts, low_pts, 16)
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    highs = [s for s in swings if s.swing_type == SWING_HIGH]
    assert [(s.index, s.price) for s in highs] == [(3, 20.0), (9, 20.0)]


def test_pair_relation_equality_policy():
    assert pair_relation(100.0, 100.0) == RELATION_EQUAL
    assert pair_relation(100.0, 100.0 + 1e-12) == RELATION_EQUAL  # float noise
    assert pair_relation(100.0, 100.001) == RELATION_HIGHER        # a real move
    assert pair_relation(100.001, 100.0) == RELATION_LOWER
    # the tolerance is relative and far below one market tick
    assert EQUAL_REL_TOL == 1e-9
    assert pair_relation(50_000.0, 50_000.0 + 1e-5) == RELATION_EQUAL  # << tick noise
    assert pair_relation(50_000.0, 50_000.1) == RELATION_HIGHER        # one tick


# --- lookback parameterization (sensitivity support) ----------------------


def test_swing_lookback_is_parameterized_and_deterministic():
    high_pts = [(0, 20), (6, 24), (12, 21), (18, 27), (24, 22), (30, 29), (36, 25)]
    low_pts = [(0, 11), (6, 6), (12, 9), (18, 7), (24, 11), (30, 8), (36, 10)]
    candles = _bars(high_pts, low_pts, 37)
    for lookback in (2, 3, 5):
        swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=lookback)
        again = detect_swings(candles, timeframe_ms=TF, swing_lookback=lookback)
        assert swings == again
        highs = [s for s in swings if s.swing_type == SWING_HIGH]
        lows = [s for s in swings if s.swing_type == SWING_LOW]
        assert [s.index for s in highs] == [6, 18, 30]
        assert [s.index for s in lows] == [6, 18, 30]
    assert SWING_LOOKBACK_DEFAULT == 3  # research default, not frozen for OOS


def test_invalid_swing_lookback_raises():
    candles = _fixture()
    with pytest.raises(ValueError):
        detect_swings(candles, timeframe_ms=TF, swing_lookback=0)
    with pytest.raises(ValueError):
        detect_swings(candles, timeframe_ms=TF, swing_lookback=True)
    with pytest.raises(ValueError):
        detect_swings(candles, timeframe_ms=TF, swing_lookback=-1)
    with pytest.raises(ValueError):
        detect_swings(candles, timeframe_ms=0)


def test_short_series_has_no_swings():
    candles = _fixture()[:6]  # < 2 * 3 + 1
    assert detect_swings(candles, timeframe_ms=TF, swing_lookback=3) == ()


# --- scenarios 12 / 13: BOS ----------------------------------------------


def _bos_fixture(close_16, high_16):
    candles = _fixture()
    last = candles[16]
    candles[16] = _bar(int(last.timestamp), high_16, float(last.low), close=close_16)
    return candles


def test_bos_by_close_beyond_confirmed_level():
    """Scenario 13: close beyond a confirmed Swing High -> BULLISH_BOS."""
    candles = _bos_fixture(close_16=28.0, high_16=28.5)
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    events = detect_bos_events(
        candles, swings, timeframe_ms=TF, evaluation_asof_ms=_close_of(16)
    )
    bullish = [e for e in events if e.direction == BOS_BULLISH]
    assert len(bullish) == 1
    event = bullish[0]
    assert event.level_price == 27.0
    assert event.level_type == SWING_HIGH
    assert event.level_formed_at_ms == BASE_MS + 10 * TF
    assert event.level_confirmed_at_ms == _close_of(13)
    assert event.break_open_ms == BASE_MS + 16 * TF
    assert event.break_confirmed_at_ms == _close_of(16)
    # causality invariants
    assert event.level_confirmed_at_ms < event.break_confirmed_at_ms
    assert event.break_confirmed_at_ms <= _close_of(16)


def test_bos_by_wick_without_close_is_not_bos():
    """Scenario 12: wick beyond the level with close back inside -> NO_BOS."""
    # dedicated fixture: ONE confirmed Swing High (20@3), then a wick candle
    highs = [10.0, 15.0, 18.0, 20.0, 18.0, 15.0, 13.0]
    lows = [8.0, 9.0, 10.0, 11.0, 12.0, 11.0, 10.0]
    candles = [_bar(BASE_MS + i * TF, highs[i], lows[i]) for i in range(7)]
    # wick candle: high 23 > 20 but close 19.5 < 20
    candles.append(_bar(BASE_MS + 7 * TF, 23.0, 14.0, close=19.5))
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    assert [(s.price, s.swing_type) for s in swings] == [(20.0, SWING_HIGH)]
    events = detect_bos_events(
        candles, swings, timeframe_ms=TF, evaluation_asof_ms=_close_of(7)
    )
    assert events == ()


def test_bos_bearish_close_below_confirmed_swing_low():
    # dedicated fixture: ONE confirmed Swing Low (8@3), then a close below it
    lows = [14.0, 12.0, 10.0, 8.0, 10.0, 12.0, 13.0]
    highs = [16.0, 15.0, 14.0, 13.0, 14.0, 15.0, 16.0]
    candles = [_bar(BASE_MS + i * TF, highs[i], lows[i]) for i in range(7)]
    candles.append(_bar(BASE_MS + 7 * TF, 8.0, 6.5, close=6.5))
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    assert [(s.price, s.swing_type) for s in swings] == [(8.0, SWING_LOW)]
    events = detect_bos_events(
        candles, swings, timeframe_ms=TF, evaluation_asof_ms=_close_of(7)
    )
    bearish = [e for e in events if e.direction == BOS_BEARISH]
    assert len(bearish) == 1
    assert bearish[0].level_price == 8.0
    assert bearish[0].level_type == SWING_LOW
    assert bearish[0].level_confirmed_at_ms == _close_of(6)
    assert bearish[0].break_confirmed_at_ms == _close_of(7)
    assert bearish[0].level_confirmed_at_ms < bearish[0].break_confirmed_at_ms


def test_bos_level_must_be_confirmed_before_the_break_close():
    # synthetic level confirmed exactly at the candidate break close -> no BOS
    candles = [_bar(BASE_MS, 21.0, 19.0, close=20.5)]
    level = Swing(
        formed_at_ms=BASE_MS - TF,
        confirmed_at_ms=BASE_MS + TF,  # == the break close time
        price=20.0,
        swing_type=SWING_HIGH,
        index=0,
    )
    events = detect_bos_events(
        candles, [level], timeframe_ms=TF, evaluation_asof_ms=BASE_MS + TF
    )
    assert events == ()
    # ...and the same level confirmed one ms earlier IS breakable
    earlier = Swing(
        formed_at_ms=BASE_MS - TF,
        confirmed_at_ms=BASE_MS + TF - 1,
        price=20.0,
        swing_type=SWING_HIGH,
        index=0,
    )
    events = detect_bos_events(
        candles, [earlier], timeframe_ms=TF, evaluation_asof_ms=BASE_MS + TF
    )
    assert len(events) == 1
    assert events[0].direction == BOS_BULLISH
    assert events[0].level_confirmed_at_ms == BASE_MS + TF - 1


def test_bos_level_breaks_only_once():
    # three consecutive closes beyond the same confirmed level -> one event
    candles = [
        _bar(BASE_MS + i * TF, 21.0 + i, 19.0 + i, close=20.5 + i) for i in range(3)
    ]
    level = Swing(
        formed_at_ms=BASE_MS - 2 * TF,
        confirmed_at_ms=BASE_MS - 1,
        price=20.0,
        swing_type=SWING_HIGH,
        index=0,
    )
    events = detect_bos_events(
        candles, [level], timeframe_ms=TF, evaluation_asof_ms=BASE_MS + 3 * TF
    )
    assert len(events) == 1
    assert events[0].break_open_ms == BASE_MS  # first close-beyond candle


def test_bos_is_gated_by_evaluation_asof():
    candles = _bos_fixture(close_16=28.0, high_16=28.5)
    swings = detect_swings(candles, timeframe_ms=TF, swing_lookback=3)
    # before the break candle closes there is no BOS at all
    early = detect_bos_events(
        candles, swings, timeframe_ms=TF, evaluation_asof_ms=_close_of(16) - 1
    )
    assert early == ()
    late = detect_bos_events(
        candles, swings, timeframe_ms=TF, evaluation_asof_ms=_close_of(16)
    )
    assert all(e.break_confirmed_at_ms <= _close_of(16) for e in late)


def test_bos_retires_all_levels_crossed_by_first_close():
    """A close over two highs retires both; the lower cannot fire later."""
    candles = [
        _bar(BASE_MS, 14.0, 9.0, close=13.0),
        _bar(BASE_MS + TF, 14.0, 9.0, close=11.0),
    ]
    levels = [
        Swing(formed_at_ms=BASE_MS - 5 * TF,
              confirmed_at_ms=BASE_MS - TF,
              price=10.0, swing_type=SWING_HIGH, index=0),
        Swing(formed_at_ms=BASE_MS - 4 * TF,
              confirmed_at_ms=BASE_MS - TF,
              price=12.0, swing_type=SWING_HIGH, index=1),
    ]
    events = detect_bos_events(
        candles, levels, timeframe_ms=TF,
        evaluation_asof_ms=BASE_MS + 2 * TF,
    )
    bullish = [e for e in events if e.direction == BOS_BULLISH]
    assert len(bullish) == 1
    assert bullish[0].level_price == 12.0
    assert bullish[0].break_open_ms == BASE_MS
