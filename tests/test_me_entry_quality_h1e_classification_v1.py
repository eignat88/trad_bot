"""Issue #324 / H1-E.3 — ASOF 4H/1D classification: 60/60 vs 59/60, close
boundary ±1 µs, nine regimes, invalid sources, no-look-ahead determinism.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1e_v1.classification import (
    NONELIGIBLE_HTF_INCOMPLETE_COVERAGE,
    NONELIGIBLE_HTF_INVALID_SOURCE,
    REASON_ELIGIBLE,
    STATUS_CLASSIFIED,
    classify_h1e_event,
    replay_canonical_bytes,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    admit_source_row,
)
from tests.h1e_v1_fixtures import (
    DAY_MS,
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    H4_MS,
    StubTransport,
    make_source_row,
    timeframe_payload,
    zigzag_rows,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)


def _event(signal_time_us: int = FIXTURE_ASOF_US):
    row = make_source_row(signal_time_us=signal_time_us)
    decision = admit_source_row(row, freeze=FREEZE)
    assert decision.accepted, decision.detail
    return decision.event


def _transport(regime_4h="BULLISH", regime_1d="BULLISH", *, rows_4h=None, rows_1d=None):
    rows4 = rows_4h if rows_4h is not None else zigzag_rows(
        regime_4h, timeframe_ms=H4_MS,
        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows1 = rows_1d if rows_1d is not None else zigzag_rows(
        regime_1d, timeframe_ms=DAY_MS,
        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    return StubTransport(default={
        "240": timeframe_payload(rows4, interval="240"),
        "D": timeframe_payload(rows1, interval="D"),
    })


REGIMES = ("BULLISH", "BEARISH", "NEUTRAL")


# ─── 60/60 and the nine 4H×1D combinations ─────────────────────────────────


@pytest.mark.parametrize("regime_4h", REGIMES)
@pytest.mark.parametrize("regime_1d", REGIMES)
def test_all_nine_combinations_eligible_with_60_of_60(regime_4h, regime_1d):
    outcome = classify_h1e_event(_event(), _transport(regime_4h, regime_1d))
    record = outcome.record
    assert record.status == STATUS_CLASSIFIED
    assert record.eligible
    assert record.eligibility_reason == REASON_ELIGIBLE
    assert record.coverage_4h_present == 60
    assert record.coverage_1d_present == 60
    # all nine combinations retained VERBATIM — never merged, never averaged
    assert record.combined_regime == f"{regime_4h}/{regime_1d}"
    assert record.regime_4h == regime_4h
    assert record.regime_1d == regime_1d
    assert record.snapshot_sha256


# ─── 59/60 and gaps ─────────────────────────────────────────────────────────


def test_59_of_60_is_non_eligible_with_exact_missing_bucket():
    rows4 = zigzag_rows("BULLISH", timeframe_ms=H4_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows1 = zigzag_rows("BULLISH", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    # drop the EARLIEST 4H candle → exactly 59/60 with one missing bucket
    rows4_missing = rows4[1:]
    record = classify_h1e_event(
        _event(), _transport(rows_4h=rows4_missing, rows_1d=rows1)).record
    assert not record.eligible
    assert NONELIGIBLE_HTF_INCOMPLETE_COVERAGE in record.non_eligible_codes
    assert record.coverage_4h_present == 59
    assert record.coverage_4h_missing_open_ms == (rows4[0]["open_ms"],)
    assert record.coverage_1d_present == 60
    # an incomplete source NEVER becomes a market mode
    assert record.combined_regime is None


def test_gap_inside_series_is_invalid_source():
    rows4 = zigzag_rows("BULLISH", timeframe_ms=H4_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows1 = zigzag_rows("BULLISH", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    rows4_gap = rows4[:30] + rows4[31:]  # hole strictly inside the window
    record = classify_h1e_event(
        _event(), _transport(rows_4h=rows4_gap, rows_1d=rows1)).record
    assert not record.eligible
    assert record.coverage_4h_present == 59
    assert record.coverage_4h_missing_open_ms == (rows4[30]["open_ms"],)


# ─── close boundary at ±1 µs ────────────────────────────────────────────────


def test_close_boundary_inclusive_exact_close_us():
    # signal_time EXACTLY at the close instant of the 60th candle → 60/60
    outcome = classify_h1e_event(_event(), _transport())
    assert outcome.record.eligible
    assert outcome.record.coverage_4h_present == 60


def test_close_boundary_minus_one_microsecond_is_59_of_60():
    # one microsecond BEFORE the close instant → that candle is unclosed
    event = _event(signal_time_us=FIXTURE_ASOF_US - 1)
    record = classify_h1e_event(event, _transport()).record
    assert record.asof_us == FIXTURE_ASOF_US - 1
    assert not record.eligible
    assert record.coverage_4h_present == 59
    assert record.coverage_1d_present == 59
    assert NONELIGIBLE_HTF_INCOMPLETE_COVERAGE in record.non_eligible_codes


# ─── no look-ahead / determinism ────────────────────────────────────────────


def test_future_append_cannot_change_an_earlier_classification():
    event = _event()
    base = classify_h1e_event(event, _transport()).record
    rows4 = zigzag_rows("BULLISH", timeframe_ms=H4_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows1 = zigzag_rows("BULLISH", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    # the SAME history plus appended future candles
    future4 = [
        {"open_ms": FIXTURE_ASOF_US // 1000 + i * H4_MS,
         "open": 200.0 + i, "high": 201.0 + i, "low": 199.0 + i,
         "close": 200.5 + i, "volume": 1.0}
        for i in range(5)
    ]
    future1 = [
        {"open_ms": FIXTURE_ASOF_US // 1000 + i * DAY_MS,
         "open": 300.0 + i, "high": 302.0 + i, "low": 298.0 + i,
         "close": 301.0 + i, "volume": 1.0}
        for i in range(3)
    ]
    extended = classify_h1e_event(
        event,
        StubTransport(default={
            "240": timeframe_payload(rows4 + future4, interval="240"),
            "D": timeframe_payload(rows1 + future1, interval="D"),
        }),
    ).record
    assert extended.to_dict() == base.to_dict()
    assert extended.snapshot_sha256 == base.snapshot_sha256


def test_classification_asof_is_signal_time_never_captured_at():
    row = make_source_row(
        signal_time_us=FIXTURE_ASOF_US, captured_us=FIXTURE_ASOF_US + 999_000_000)
    event_a = admit_source_row(row, freeze=FREEZE).event
    row2 = make_source_row(
        signal_time_us=FIXTURE_ASOF_US, captured_us=FIXTURE_ASOF_US + 7_200_000_000)
    event_b = admit_source_row(row2, freeze=FREEZE).event
    out_a = classify_h1e_event(event_a, _transport()).record
    out_b = classify_h1e_event(event_b, _transport()).record
    assert out_a.asof_us == FIXTURE_ASOF_US
    assert out_b.asof_us == FIXTURE_ASOF_US
    assert out_a.to_dict() == out_b.to_dict()  # delivery time never leaks in


def test_offline_replay_is_deterministic_and_byte_identical():
    outcome = classify_h1e_event(_event(), _transport())
    assert outcome.snapshot is not None
    first = replay_canonical_bytes(outcome.snapshot)
    second = replay_canonical_bytes(outcome.snapshot)
    assert first == second
    assert first.startswith(b"{")  # canonical JSON output
    assert b'"eligible":true' in first


# ─── invalid sources fail closed ────────────────────────────────────────────


def test_invalid_ohlc_row_never_becomes_a_market_mode():
    rows1 = zigzag_rows("BEARISH", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    rows4 = zigzag_rows("BEARISH", timeframe_ms=H4_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    rows4[20] = {**rows4[20], "high": rows4[20]["low"] - 1.0}  # high < low
    record = classify_h1e_event(
        _event(), _transport(rows_4h=rows4, rows_1d=rows1)).record
    assert not record.eligible
    assert record.eligibility_reason != REASON_ELIGIBLE
    assert record.non_eligible_codes


def test_empty_payload_is_non_eligible_with_evidence():
    record = classify_h1e_event(
        _event(), _transport(rows_4h=[], rows_1d=[])).record
    assert not record.eligible
    assert NONELIGIBLE_HTF_INVALID_SOURCE in record.non_eligible_codes
    assert record.combined_regime is None
    assert record.snapshot_sha256 == ""


def test_insufficient_structure_is_invalid_source_not_neutral():
    # monotone series: zero confirmed swing pairs → INVALID_SOURCE
    rows4 = zigzag_rows(
        "BULLISH", timeframe_ms=H4_MS,
        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * H4_MS)
    flat = []
    for i, row in enumerate(rows4):
        flat.append({**row, "open": 100.0 + i, "high": 100.5 + i,
                     "low": 99.5 + i, "close": 100.0 + i})
    rows1 = zigzag_rows("BULLISH", timeframe_ms=DAY_MS,
                        first_open_ms=FIXTURE_ASOF_US // 1000 - 60 * DAY_MS)
    record = classify_h1e_event(
        _event(), _transport(rows_4h=flat, rows_1d=rows1)).record
    assert not record.eligible
    # 60/60 coverage but no market mode is invented
    assert record.coverage_4h_present == 60
    assert record.regime_4h is None or record.regime_4h == "INVALID_SOURCE"
