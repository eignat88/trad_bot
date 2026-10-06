"""Focused tests for the isolated HTF key-level Point-B prospective detector.

These tests are intentionally pure and do not touch PostgreSQL, paper trading,
or VPS.  They use only deterministic synthetic candle histories.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app.research.htf_keylevel_point_b import (
    KeyLevel,
    _causal_atr_series,
    _candle_time,
    _find_htf_key_levels,
    _find_reaction_end,
    _find_structure_break,
    _historical_atr,
    detect_point_b_setup,
)


BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candle(index: int, *, high: float, low: float, close: float, volume: float = 10.0) -> Candle:
    return Candle(int((BASE_TS + timedelta(minutes=index * 5)).timestamp() * 1000), close, high, low, close, volume)


def htf_candle(index: int, *, high: float, low: float, close: float, volume: float = 10.0) -> Candle:
    return Candle(int((BASE_TS + timedelta(hours=index)).timestamp() * 1000), close, high, low, close, volume)


def closed_time(count: int) -> datetime:
    return BASE_TS + timedelta(minutes=(count - 1) * 5, seconds=1)


def _short_htf_candle(index: int, *, high: float, low: float, close: float, volume: float = 10.0) -> Candle:
    return Candle(int((BASE_TS + timedelta(hours=index - 26)).timestamp() * 1000), close, high, low, close, volume)


def _long_htf_candle(index: int, *, high: float, low: float, close: float, volume: float = 10.0) -> Candle:
    return Candle(int((BASE_TS + timedelta(hours=index - 24)).timestamp() * 1000), close, high, low, close, volume)


def _flat_htf(direction: str, high: float = 105.0, low: float = 95.0) -> list[Candle]:
    """Construct explicit past-only HTF swing levels with repeated touches."""
    rows = []
    if direction == "LONG":
        rows.extend([
            _long_htf_candle(0, high=101.0, low=100.1, close=100.5),
            _long_htf_candle(1, high=101.0, low=100.0, close=100.4),
            _long_htf_candle(2, high=101.0, low=100.0, close=100.4),
            _long_htf_candle(3, high=101.0, low=100.1, close=100.4),
            _long_htf_candle(4, high=101.2, low=100.2, close=100.4),
            _long_htf_candle(5, high=101.0, low=100.0, close=100.5),
            _long_htf_candle(6, high=101.0, low=100.2, close=100.6),
            _long_htf_candle(7, high=101.0, low=100.3, close=100.7),
            _long_htf_candle(8, high=101.0, low=100.4, close=100.8),
            _long_htf_candle(9, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(10, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(11, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(12, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(13, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(14, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(15, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(16, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(17, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(18, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(19, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(20, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(21, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(22, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(23, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(24, high=101.0, low=100.5, close=100.9),
            _long_htf_candle(25, high=101.0, low=100.5, close=100.9),
        ])
    else:
        rows.extend([
            _short_htf_candle(0, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(1, high=101.7, low=98.0, close=100.0),
            _short_htf_candle(2, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(3, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(4, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(5, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(6, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(7, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(8, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(9, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(10, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(11, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(12, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(13, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(14, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(15, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(16, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(17, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(18, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(19, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(20, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(21, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(22, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(23, high=102.1, low=99.0, close=101.0),
            _short_htf_candle(24, high=101.1, low=98.0, close=100.0),
            _short_htf_candle(25, high=102.1, low=99.0, close=101.0),
        ])
    return rows


def _long_history() -> list[Candle]:
    start_index = 30
    rows = []

    # Stable history around the 100 support.
    rows.append(
        candle(start_index, high=100.6, low=100.4, close=100.5)
    )
    for i in range(1, 20):
        rows.append(
            candle(
                start_index + i,
                high=100.2,
                low=99.8,
                close=100.0,
                volume=10,
            )
        )

    rows.extend([
        # Touch / beginning of reaction.
        candle(
            start_index + 20,
            high=100.3,
            low=99.4,
            close=100.1,
            volume=20,
        ),

        # Strong bullish reaction.
        candle(
            start_index + 21,
            high=104.0,
            low=99.5,
            close=103.7,
            volume=100,
        ),

        # Second post-touch/reaction candle.
        # Must NOT break the prior high.
        candle(
            start_index + 22,
            high=104.0,
            low=103.3,
            close=103.6,
            volume=20,
        ),

        # Genuine bullish structure break.
        # Prior causal local HIGH is approximately 104.0.
        # Close is deliberately far above it so this is not
        # a threshold-boundary test.
        candle(
            start_index + 23,
            high=106.0,
            low=103.8,
            close=105.7,
            volume=30,
        ),

        # First Point-B candidate.
        #
        # If structure reference is 104.0 and break close is 105.7:
        # leg = 1.7
        #
        # low=104.85 gives:
        # (105.7 - 104.85) / 1.7 = 0.50
        #
        # Therefore this is cleanly inside frozen 0.25–0.75.
        candle(
            start_index + 24,
            high=105.5,
            low=104.85,
            close=105.1,
            volume=15,
        ),

        candle(
            start_index + 25,
            high=105.4,
            low=104.9,
            close=105.2,
            volume=10,
        ),
        candle(
            start_index + 26,
            high=105.5,
            low=105.0,
            close=105.3,
            volume=10,
        ),
        candle(
            start_index + 27,
            high=105.5,
            low=105.0,
            close=105.2,
            volume=10,
        ),
        candle(
            start_index + 28,
            high=105.6,
            low=105.0,
            close=105.3,
            volume=10,
        ),
    ])

    return rows


def _short_history() -> list[Candle]:
    rows = []

    # Stable execution history below HTF resistance.
    for i in range(20):
        rows.append(
            candle(
                i,
                high=100.2,
                low=99.8,
                close=100.0,
                volume=10,
            )
        )

    rows.extend([
        # Resistance touch.
        candle(
            20,
            high=102.2,
            low=100.0,
            close=101.6,
            volume=20,
        ),

        # First bearish reaction candle.
        candle(
            21,
            high=101.7,
            low=96.5,
            close=96.8,
            volume=100,
        ),

        # Reaction confirmation.
        # Its low becomes the causal local structure reference.
        candle(
            22,
            high=97.0,
            low=96.0,
            close=96.3,
            volume=20,
        ),

        # Genuine bearish structure break.
        # prior_low ~= 96.0; this close is deliberately far below it.
        candle(
            23,
            high=95.0,
            low=94.2,
            close=94.5,
            volume=30,
        ),

        # Point-B retracement.
        # break=94.5, structure~=96.0, leg~=1.5
        # high=95.25 -> retrace ~= 0.50.
        candle(
            24,
            high=95.25,
            low=94.7,
            close=95.0,
            volume=15,
        ),

        candle(
            25,
            high=95.2,
            low=94.7,
            close=94.9,
            volume=10,
        ),
        candle(
            26,
            high=95.1,
            low=94.6,
            close=94.8,
            volume=10,
        ),
        candle(
            27,
            high=95.0,
            low=94.5,
            close=94.7,
            volume=10,
        ),
        candle(
            28,
            high=95.0,
            low=94.4,
            close=94.7,
            volume=10,
        ),
    ])

    return rows


def test_long_valid_path() -> None:
    execution_candles = _long_history()
    current_time = _candle_time(execution_candles[27])
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=execution_candles,
        htf_candles=_flat_htf("LONG"),
        current_time=current_time,
    )
    assert result.signal is not None
    assert result.baseline is not None
    assert result.signal.features["setup_event_id"] == result.baseline.features["setup_event_id"]
    assert result.signal.features["cohort"] == "POINT_B"
    assert result.baseline.features["cohort"] == "BASELINE"
    assert result.stages[-1] == "POINT_B_FORMED"
    assert result.signal.features["risk_abs"] > 0


def test_short_valid_path() -> None:
    history = _short_history()
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="SHORT",
        execution_candles=history,
        htf_candles=_flat_htf("SHORT"),
        current_time=_candle_time(history[26]),
    )
    assert result.signal is not None
    assert result.signal.direction == "SHORT"
    assert result.signal.features["key_level_type"] == "resistance"



def test_long_post_break_invalidation_prevents_later_point_b_reentry() -> None:
    rows = _long_history()

    # Index 24 is the first post-break candle. Force structural invalidation:
    # reaction_low is 99.4, so close=99.3 invalidates the setup.
    # Later candles still contain valid-looking Point-B retracement geometry,
    # but the event must remain terminal.
    rows[24] = candle(
        30 + 24,
        high=105.2,
        low=99.0,
        close=99.3,
        volume=15,
    )

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[-1]) + timedelta(minutes=5),
    )

    assert result.signal is None
    assert result.reason == "structural_invalidation_before_entry"


def test_short_post_break_invalidation_prevents_later_point_b_reentry() -> None:
    rows = _short_history()

    # reaction_high is above 101; force a close beyond it immediately
    # after the bearish structure break. Later rows must not resurrect it.
    rows[24] = candle(
        24,
        high=103.0,
        low=94.7,
        close=102.5,
        volume=15,
    )

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="SHORT",
        execution_candles=rows,
        htf_candles=_flat_htf("SHORT"),
        current_time=_candle_time(rows[-1]) + timedelta(minutes=5),
    )

    assert result.signal is None
    assert result.reason == "structural_invalidation_before_entry"


def test_touch_without_reaction_has_no_signal() -> None:
    rows = []
    for i in range(20):
        rows.append(candle(i, high=100.2, low=99.8, close=100.0))
    rows.append(candle(20, high=100.0, low=98.0, close=98.4, volume=20))
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=closed_time(20),
    )
    assert result.signal is None
    assert result.baseline is None
    assert result.reason in {
        "no_reaction_confirmed",
        "insufficient_closed_execution_history",
        "no_htf_key_level",
    }


def test_reaction_without_break_has_no_signal() -> None:
    prefix = _long_history()[:22]
    neutral_tail = [
        candle(52 + index, high=99.6, low=99.4, close=99.5, volume=10)
        for index in range(14)
    ]
    rows = prefix + neutral_tail
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[35]),
    )
    assert result.signal is None
    assert result.reason == "no_structure_break"


def test_break_without_point_b_has_no_signal() -> None:
    # Include the genuine structure-break candle, but never retrace
    # deeply enough to enter the frozen 0.25-0.75 Point-B band.
    rows = _long_history()[:24]
    neutral_tail = [
        candle(54 + index, high=105.9, low=105.4, close=105.6, volume=10)
        for index in range(12)
    ]
    rows.extend(neutral_tail)
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[35]),
    )
    assert result.signal is None
    assert result.reason == "no_point_b_in_frozen_retrace_bounds"


def test_point_b_outside_frozen_bounds_has_no_signal() -> None:
    rows = _long_history()[:26]

    # Point-B is evaluated first at position 24.
    # break=105.7, structure~=104.0, leg~=1.7.
    # low=105.4 gives retrace~=0.176, below frozen minimum 0.25.
    rows[24] = candle(
        30 + 24,
        high=105.9,
        low=105.4,
        close=105.6,
        volume=20,
    )

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[25]),
    )
    assert result.signal is None
    assert result.reason == "no_point_b_in_frozen_retrace_bounds"

def test_determinism_and_deduplication() -> None:
    history = _long_history()
    kwargs = dict(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=history,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(history[27]),
    )
    first = detect_point_b_setup(**kwargs)
    second = detect_point_b_setup(**kwargs)
    assert first.signal is not None and second.signal is not None
    assert first.setup_event_id == second.setup_event_id
    assert first.signal.features["setup_event_id"] == second.signal.features["setup_event_id"]
    assert first.signal.signal_candle_open_time == second.signal.signal_candle_open_time


def _causal_atr(candles: list[Candle], index: int) -> float:
    return _historical_atr(candles[: index + 1], float(candles[index].close))


def _level_candidate(execution_candle: Candle, htf_candles: list[Candle]) -> tuple[KeyLevel | None, tuple[float, float, float] | None]:
    return _find_htf_key_levels(htf_candles, [execution_candle], 5.0)


def _htf_availability_fixture(count: int = 31) -> list[Candle]:
    htf_candles = [
        htf_candle(index, high=101.0, low=100.0, close=100.5)
        for index in range(count)
    ]
    htf_candles[4] = htf_candle(4, high=103.0, low=98.0, close=100.0)
    htf_candles[5] = htf_candle(5, high=101.0, low=97.0, close=99.0)
    htf_candles[6] = htf_candle(6, high=101.0, low=97.0, close=100.0)
    htf_candles[7] = htf_candle(7, high=101.0, low=97.0, close=100.0)
    htf_candles[8] = htf_candle(8, high=101.0, low=97.0, close=100.0)
    return htf_candles


def test_htf_level_requires_confirmation_availability_before_execution() -> None:
    htf_candles = _htf_availability_fixture()
    target_available = htf_candles[8].timestamp + 60 * 60_000
    pre_confirmation = replace(
        candle(4, high=101.0, low=100.0, close=100.0),
        timestamp=int((BASE_TS + timedelta(hours=8, minutes=55)).timestamp() * 1000),
    )
    exact_boundary = replace(
        candle(8, high=101.0, low=100.0, close=100.0),
        timestamp=int((BASE_TS + timedelta(hours=9)).timestamp() * 1000),
    )
    post_confirmation = replace(
        candle(9, high=101.0, low=100.0, close=100.0),
        timestamp=int((BASE_TS + timedelta(hours=9, minutes=5)).timestamp() * 1000),
    )
    pre_level, _ = _level_candidate(pre_confirmation, htf_candles)
    exact_level, _ = _level_candidate(exact_boundary, htf_candles)
    post_level, _ = _level_candidate(post_confirmation, htf_candles)

    assert target_available == htf_candles[8].timestamp + 60 * 60_000
    assert pre_confirmation.timestamp < target_available
    assert exact_boundary.timestamp >= target_available
    assert post_confirmation.timestamp >= target_available
    if pre_level is not None and pre_level.index == 4:
        raise AssertionError("target level must not be available before confirmation close")
    if exact_level is not None and exact_level.index != 4:
        raise AssertionError("exact boundary should expose the target level if no earlier support outranks it")
    if post_level is not None and post_level.index != 4:
        raise AssertionError("post boundary should expose the target level if no earlier support outranks it")


def test_directional_structure_break_does_not_accept_false_long_risk() -> None:
    rows = _long_history()
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[27]),
    )
    assert result.signal is not None
    assert result.signal.reference_price > result.signal.invalidation_price


def test_directional_structure_break_short_symmetry() -> None:
    rows = _short_history()
    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="SHORT",
        execution_candles=rows,
        htf_candles=_flat_htf("SHORT"),
        current_time=_candle_time(rows[26]),
    )
    assert result.signal is not None
    assert result.signal.reference_price < result.signal.invalidation_price


def test_valid_point_b_signal_survives_zero_risk_baseline_case() -> None:
    rows = _long_history()
    rows.append(candle(30 + 29, high=105.4, low=105.0, close=105.0, volume=10))
    result = detect_point_b_setup(symbol="BTCUSDT", direction="LONG", execution_candles=rows, htf_candles=_flat_htf("LONG"), current_time=_candle_time(rows[28]))
    assert result.signal is not None
    assert result.signal.features["risk_abs"] > 0


def test_baseline_handling_does_not_change_point_b_geometry() -> None:
    rows = _long_history()
    rows.append(candle(30 + 29, high=105.4, low=105.0, close=105.0, volume=10))

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[28]),
    )

    assert result.signal is not None
    assert result.signal.features["entry_reference_price"] == 105.1
    assert result.signal.features["structural_stop_price"] == 99.4
    assert result.signal.features["risk_abs"] == pytest.approx(5.7)
    assert result.signal.features["point_b_price"] == 105.1
    assert result.signal.features["point_b_retrace_pct"] == pytest.approx(50.0)


def test_reaction_stage_uses_only_causal_atr_prefix() -> None:
    prefix = [
        candle(index, high=100.2, low=99.8, close=100.0, volume=10.0)
        for index in range(101)
    ]
    normal_history = prefix + [
        candle(101, high=100.2, low=99.4, close=100.1, volume=10.0),
        candle(102, high=104.0, low=99.4, close=103.7, volume=100.0),
    ] + [
        candle(index, high=100.2, low=99.8, close=100.0, volume=10.0)
        for index in range(103, 120)
    ]
    modified_history = list(normal_history[:103]) + [
        candle(index, high=500.0, low=-100.0, close=200.0, volume=10.0)
        for index in range(103, 120)
    ]
    level = KeyLevel(
        price=100.0,
        level_type="support",
        index=0,
        timestamp=0,
        touches=2,
        strength=0.4,
        available_timestamp=0,
    )
    normal_atr = _causal_atr_series(normal_history)
    modified_atr = _causal_atr_series(modified_history)
    normal_reaction = _find_reaction_end(
        closed=normal_history,
        touch_index=100,
        level=level,
        direction="LONG",
        atr_by_index=normal_atr,
    )
    modified_reaction = _find_reaction_end(
        closed=modified_history,
        touch_index=100,
        level=level,
        direction="LONG",
        atr_by_index=modified_atr,
    )
    normal_reaction_end = normal_reaction[0] if normal_reaction is not None else None
    modified_reaction_end = modified_reaction[0] if modified_reaction is not None else None
    full_normal_atr = _historical_atr(normal_history, float(normal_history[-1].close))
    full_modified_atr = _historical_atr(modified_history, float(modified_history[-1].close))

    assert normal_history[:103] == modified_history[:103]
    assert normal_history[103:] != modified_history[103:]
    assert normal_atr[102] == modified_atr[102]
    assert normal_reaction_end == 102
    assert modified_reaction_end == 102
    assert full_normal_atr != full_modified_atr


def test_reaction_confirmation_is_historically_stable_after_later_closed_candles() -> None:
    original = _long_history()
    original_current_time = _candle_time(original[27])
    original_result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=original,
        htf_candles=_flat_htf("LONG"),
        current_time=original_current_time,
    )
    assert original_result.signal is not None
    reaction_candle_time = original_result.signal.features["reaction_time"]
    reaction_index = next(
        index
        for index, row in enumerate(original)
        if _candle_time(row).isoformat() == reaction_candle_time
    )

    high_volatility = []
    for offset in range(reaction_index + 1, len(original) + 10):
        high = 104.0 + offset * 3.0
        low = 20.0 - offset * 2.0
        close = 60.0 + offset
        high_volatility.append(
            candle(30 + offset, high=high, low=low, close=close, volume=1000.0)
        )
    altered = original[: reaction_index + 1] + high_volatility
    altered_current_time = _candle_time(altered[27])
    altered_result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=altered,
        htf_candles=_flat_htf("LONG"),
        current_time=altered_current_time,
    )

    original_reaction = _find_reaction_end(
        closed=original,
        touch_index=19,
        level=KeyLevel(
            price=100.0,
            level_type="support",
            index=0,
            timestamp=original[0].timestamp,
            touches=2,
            strength=0.4,
            available_timestamp=0,
        ),
        direction="LONG",
        atr_by_index=_causal_atr_series(original),
    )
    altered_reaction = _find_reaction_end(
        closed=altered,
        touch_index=19,
        level=KeyLevel(
            price=100.0,
            level_type="support",
            index=0,
            timestamp=original[0].timestamp,
            touches=2,
            strength=0.4,
            available_timestamp=0,
        ),
        direction="LONG",
        atr_by_index=_causal_atr_series(altered),
    )

    assert original[: reaction_index + 1] == altered[: reaction_index + 1]
    assert original_reaction is not None
    assert altered_reaction is not None
    assert original_reaction[0] == altered_reaction[0] == reaction_index
    assert _causal_atr_series(original)[reaction_index] == _causal_atr_series(altered)[reaction_index]
    assert original_reaction[1:] == altered_reaction[1:]


def test_structure_break_decision_is_historically_stable_after_later_closed_candles() -> None:
    base = [
        candle(index, high=100.2, low=99.8, close=100.0, volume=10.0)
        for index in range(100)
    ]
    history_a = base + [
        candle(100, high=100.2, low=99.8, close=100.0, volume=10.0),
        candle(101, high=100.2, low=99.4, close=100.1, volume=10.0),
        candle(102, high=104.0, low=99.4, close=103.7, volume=100.0),
        # Genuine causal break above the prior local HIGH at 104.0.
        candle(103, high=105.0, low=103.5, close=104.8, volume=10.0),
        candle(104, high=104.1, low=100.0, close=104.0, volume=10.0),
    ] + [
        candle(index, high=104.2, low=103.8, close=104.0, volume=10.0)
        for index in range(105, 122)
    ]
    history_b = list(history_a[:105]) + [
        candle(index, high=500.0, low=-100.0, close=200.0, volume=10.0)
        for index in range(105, 122)
    ]
    atr_a = _causal_atr_series(history_a)
    atr_b = _causal_atr_series(history_b)
    result_a = _find_structure_break(
        closed=history_a,
        reaction_end=102,
        level_type="support",
        direction="LONG",
        atr_by_index=atr_a,
    )
    result_b = _find_structure_break(
        closed=history_b,
        reaction_end=102,
        level_type="support",
        direction="LONG",
        atr_by_index=atr_b,
    )
    full_a = _historical_atr(history_a, float(history_a[-1].close))
    full_b = _historical_atr(history_b, float(history_b[-1].close))

    assert result_a is not None and result_b is not None
    assert history_a[:105] == history_b[:105]
    assert history_a[105:] != history_b[105:]
    assert atr_a[104] == atr_b[104]
    assert result_a[0] == result_b[0] == 103
    assert result_a[2] == result_b[2] == 104.0
    assert result_a[3] == result_b[3] == 102
    assert full_a != full_b


def test_future_candles_do_not_change_already_emitted_signal() -> None:
    history = _long_history()
    time_before = _candle_time(history[27])
    before = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=history,
        htf_candles=_flat_htf("LONG"),
        current_time=time_before,
    )
    after = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=history + [candle(30 + len(history), high=200.0, low=20.0, close=150.0, volume=100)],
        htf_candles=_flat_htf("LONG"),
        current_time=time_before,
    )
    assert before.signal is not None and after.signal is not None
    assert before.setup_event_id == after.setup_event_id
    assert before.signal.reference_price == after.signal.reference_price
    assert before.signal.signal_candle_open_time == after.signal.signal_candle_open_time

def test_short_structure_reference_index_matches_minimum_low() -> None:
    import app.research.htf_keylevel_point_b as detector

    rows = [
        candle(
            index,
            high=101.0 + index * 0.01,
            low=99.0,
            close=100.0,
            volume=10.0,
        )
        for index in range(20)
    ]

    # Put the unique causal minimum inside the active structure window.
    rows[15] = candle(
        15,
        high=100.5,
        low=95.25,
        close=99.0,
        volume=10.0,
    )

    reference = detector._structure_reference(
        rows,
        end_index=20,
        direction="SHORT",
    )

    assert reference is not None
    reference_price, reference_index = reference
    assert reference_price == 95.25
    assert reference_index == 15


def test_short_signal_risk_is_entry_to_reaction_high() -> None:
    history = _short_history()

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="SHORT",
        execution_candles=history,
        htf_candles=_flat_htf("SHORT"),
        current_time=_candle_time(history[26]),
    )

    assert result.signal is not None

    features = result.signal.features
    entry = float(features["entry_reference_price"])
    stop = float(features["structural_stop_price"])
    risk = float(features["risk_abs"])

    assert risk > 0
    assert abs(risk - (stop - entry)) < 1e-12

def test_completed_event_does_not_mask_later_independent_event():
    """A completed event must not permanently mask a later independent event."""
    from app.research.htf_keylevel_point_b import detect_point_b_setups

    first = _long_history()

    # Create enough quiet space after the first completed Point-B.
    rows = list(first)
    base_index = 30 + 29

    for offset in range(1, 22):
        rows.append(
            candle(
                base_index + offset,
                high=100.4,
                low=100.0,
                close=100.2,
                volume=10,
            )
        )

    # Second independent interaction with the same support.
    second_start = base_index + 22

    rows.extend([
        candle(
            second_start,
            high=100.3,
            low=99.4,
            close=100.1,
            volume=20,
        ),
        candle(
            second_start + 1,
            high=104.0,
            low=99.5,
            close=103.7,
            volume=100,
        ),
        candle(
            second_start + 2,
            high=104.0,
            low=103.3,
            close=103.6,
            volume=20,
        ),
        candle(
            second_start + 3,
            high=106.0,
            low=103.8,
            close=105.7,
            volume=30,
        ),
        candle(
            second_start + 4,
            high=105.5,
            low=104.85,
            close=105.1,
            volume=15,
        ),
    ])

    results = detect_point_b_setups(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=rows,
        htf_candles=_flat_htf("LONG"),
        current_time=_candle_time(rows[-1]) + timedelta(minutes=5),
    )

    completed = [r for r in results if r.signal is not None]

    assert len(completed) >= 2
    assert completed[0].setup_event_id != completed[1].setup_event_id
    assert completed[0].signal.detected_at < completed[1].signal.detected_at
