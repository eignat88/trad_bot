"""Issue #176 Phase-2 mandatory test matrix (3): causal 5m/1H contracts, ATR,
window-independent level identity, no look-ahead."""
from __future__ import annotations

import math

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_contracts_v1 import (
    BAR_MS,
    HTF_MS,
    ContractViolation,
    aggregate_closed_htf,
    htf_coverage_from_htf_series,
    validate_execution_series,
    validate_htf_series,
)
from app.research.htf_baseline_short_phase2_level_universe_v1 import (
    build_resistance_universe,
    is_touch,
    level_id_for,
)
from app.research.htf_keylevel_point_b import _causal_atr_series

HOUR0 = (1_760_000_000_000 // HTF_MS) * HTF_MS


def five_min(n: int, *, price: float = 100.0, start: int | None = None) -> list[Candle]:
    start = HOUR0 if start is None else start
    return [
        Candle(start + i * BAR_MS, price, price + 0.1, price - 0.1, price + 0.05, 1.0)
        for i in range(n)
    ]


def htf_candles(highs: list[float], *, start: int | None = None) -> list[Candle]:
    start = HOUR0 if start is None else start
    return [
        Candle(start + i * HTF_MS, 100.0, h, 99.0, 100.0, 1.0)
        for i, h in enumerate(highs)
    ]


def crafted_htf() -> list[Candle]:
    """48h series: two prior touches (109.8) and one 4/4 swing at i=30 (110.0)."""
    highs = [100.0 + (i % 3) * 0.1 for i in range(48)]
    for i in (10, 20):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    highs[30] = 110.0
    return htf_candles(highs)


# ------------------------------------------------------- 5m series validation
def test_valid_5m_series_accepted():
    series = five_min(30)
    asof = int(series[-1].timestamp) + BAR_MS
    validate_execution_series(series, asof)  # no raise


def test_5m_gap_rejected():
    series = five_min(12)
    broken = series[:5] + series[7:]  # two bars missing in the middle
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(broken, int(broken[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "NON_CONTIGUOUS_5M"


def test_5m_duplicate_and_conflicting_rejected():
    series = five_min(5)
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(series + [series[-1]], int(series[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "DUPLICATE_CANDLE"
    clash = Candle(series[-1].timestamp, 999, 999, 999, 999, 1)
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(series[:-1] + [clash, series[-1]], int(series[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "CONFLICTING_CANDLE"


def test_5m_out_of_order_and_unaligned_rejected():
    series = five_min(5)
    swapped = [series[1], series[0]] + series[2:]
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(swapped, int(series[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "OUT_OF_ORDER_CANDLE"
    bad = [Candle(int(series[0].timestamp) + 12345, 100, 101, 99, 100, 1)] + series[1:]
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(bad, int(series[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "UNALIGNED_CANDLE"


def test_5m_forming_candle_rejected():
    series = five_min(10)
    asof = int(series[-1].timestamp) + BAR_MS
    forming = Candle(series[-1].timestamp + BAR_MS, 100, 101, 99, 100, 1)
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(series + [forming], asof)
    assert excinfo.value.reason == "FUTURE_OR_UNCLOSED_CANDLE"


def test_5m_stale_prefix_rejected():
    series = five_min(10)
    asof = int(series[-1].timestamp) + BAR_MS + BAR_MS  # one full bar late
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(series, asof)
    assert excinfo.value.reason == "STALE_PREFIX"


def test_5m_invalid_ohlc_rejected():
    series = five_min(5)
    bad = [Candle(series[2].timestamp, 100, 90, 105, 100, 1) if i == 2 else c for i, c in enumerate(series)]
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series(bad, int(series[-1].timestamp) + BAR_MS)
    assert excinfo.value.reason == "INVALID_OHLC"


def test_empty_series_rejected():
    with pytest.raises(ContractViolation) as excinfo:
        validate_execution_series([], HOUR0 + BAR_MS)
    assert excinfo.value.reason == "EMPTY_SERIES"


# ------------------------------------------------------ 1H coverage accounting
def test_complete_hours_aggregated_from_5m():
    series = five_min(12 * 3)  # 3 complete hours
    asof = int(series[-1].timestamp) + BAR_MS
    hours, coverage = aggregate_closed_htf(series, asof)
    assert len(hours) == 3
    assert coverage.clean
    assert len(coverage.complete_hours) == 3


def test_partial_and_missing_hours_reported_never_silent():
    series = five_min(12 * 3)
    asof = int(series[-1].timestamp) + BAR_MS
    # drop the middle hour entirely and half of the last hour
    broken = series[:12] + series[24:30]
    hours, coverage = aggregate_closed_htf(broken, asof)
    assert len(coverage.missing_hours) == 1
    assert len(coverage.partial_hours) == 1
    assert len(hours) == 1  # excluded from use...
    assert not coverage.clean  # ...but reported, never silent


def test_provider_1h_gap_accounting():
    series = htf_candles([101.0, 102.0, 103.0])
    missing_gap = [series[0], series[2]]
    asof = int(series[2].timestamp) + HTF_MS
    coverage = htf_coverage_from_htf_series(missing_gap, asof)
    assert coverage.complete_hours == (int(series[0].timestamp), int(series[2].timestamp))
    assert coverage.missing_hours == (int(series[1].timestamp),)


def test_provider_1h_forming_hour_rejected():
    series = htf_candles([101.0])
    with pytest.raises(ContractViolation) as excinfo:
        validate_htf_series(series, int(series[0].timestamp))  # hour not closed yet
    assert excinfo.value.reason == "UNCLOSED_1H"


# --------------------------------------------------------- level universe
def test_universe_finds_crafted_swing_with_stable_identity():
    htf = crafted_htf()
    asof = int(htf[34].timestamp) + HTF_MS
    execs = five_min(200, start=asof - 200 * BAR_MS)
    universe, diag = build_resistance_universe(htf[:35], execs, asof, symbol="BTCUSDT")
    target = [r for r in universe if abs(r.price - 110.0) < 1e-9]
    assert len(target) == 1
    record = target[0]
    assert record.level_id == level_id_for("BTCUSDT", "SHORT", int(htf[30].timestamp), int(htf[34].timestamp) + HTF_MS, 110.0)
    assert record.touch_count == 6  # 2 dips + their 4 tolerance-band neighbors
    assert record.touch_count >= 2  # KEY_LEVEL_MIN_TOUCHES satisfied
    assert record.available_ms == int(htf[34].timestamp) + HTF_MS


def test_level_identity_independent_of_rolling_window():
    htf = crafted_htf()
    asof1 = int(htf[34].timestamp) + HTF_MS
    asof2 = int(htf[40].timestamp) + HTF_MS
    execs1 = five_min(200, start=asof1 - 200 * BAR_MS)
    execs2 = five_min(200, start=asof2 - 200 * BAR_MS)
    u1, _ = build_resistance_universe(htf[:35], execs1, asof1, symbol="BTCUSDT")
    u2, _ = build_resistance_universe(htf[:41], execs2, asof2, symbol="BTCUSDT")
    ids1 = {r.level_id for r in u1 if abs(r.price - 110.0) < 1e-9}
    ids2 = {r.level_id for r in u2 if abs(r.price - 110.0) < 1e-9}
    assert ids1 and ids1 == ids2


def test_forming_level_not_visible_before_confirmation_no_lookahead():
    htf = crafted_htf()
    asof_early = int(htf[33].timestamp) + HTF_MS  # only 3 right bars exist
    execs = five_min(200, start=asof_early - 200 * BAR_MS)
    universe, _ = build_resistance_universe(htf[:34], execs, asof_early, symbol="BTCUSDT")
    assert not any(abs(r.price - 110.0) < 1e-9 for r in universe)


def test_symbol_scoped_identity_no_cross_symbol_collision():
    htf = crafted_htf()
    asof = int(htf[34].timestamp) + HTF_MS
    execs = five_min(200, start=asof - 200 * BAR_MS)
    u_btc, _ = build_resistance_universe(htf[:35], execs, asof, symbol="BTCUSDT")
    u_eth, _ = build_resistance_universe(htf[:35], execs, asof, symbol="ETHUSDT")
    assert {r.level_id for r in u_btc}.isdisjoint({r.level_id for r in u_eth})


def test_decision_atr_used_not_fallback_atr_fixes_r3():
    from app.research.htf_keylevel_point_b import FALLBACK_ATR_FRACTION

    htf = crafted_htf()
    asof = int(htf[34].timestamp) + HTF_MS
    execs = five_min(200, start=asof - 200 * BAR_MS)
    _, diag = build_resistance_universe(htf[:35], execs, asof, symbol="BTCUSDT")
    atr = _causal_atr_series(execs)
    assert diag.decision_atr == atr[-1]
    assert diag.decision_atr != pytest.approx(FALLBACK_ATR_FRACTION * 100.0)


def test_universe_rejects_invalid_5m_input_fail_closed():
    htf = crafted_htf()
    asof = int(htf[34].timestamp) + HTF_MS
    execs = five_min(50, start=asof - 200 * BAR_MS)  # gap before series, stale prefix
    with pytest.raises(ContractViolation):
        build_resistance_universe(htf, execs, asof, symbol="BTCUSDT")


def test_is_touch_respects_availability_and_tolerance():
    htf = crafted_htf()
    bar = Candle(HOUR0 + 100 * HTF_MS, 100, 110.0, 99, 100, 1)
    available = HOUR0 + 101 * HTF_MS
    assert not is_touch(bar, 110.0, available, atr=1.0)  # before availability
    assert is_touch(bar, 110.0, HOUR0, atr=1.0)
    far = Candle(HOUR0 + 100 * HTF_MS, 100, 100.0, 99, 100, 1)
    assert not is_touch(far, 110.0, HOUR0, atr=1.0)
