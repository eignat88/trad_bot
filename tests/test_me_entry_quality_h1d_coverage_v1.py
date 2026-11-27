"""Issue #314 / H1-D — causal ASOF coverage tests
(``H1D_CAUSAL_ASOF_COVERAGE_V1``).

Covers: 60/60 COMPLETE, 59/60 INCOMPLETE_COVERAGE, exact missing-bucket
diagnostics, ASOF close boundaries at T-1ms / T / T+1ms and ±1µs, future /
unclosed candle exclusion (no look-ahead), gaps, duplicates, out-of-order,
invalid OHLCV (non-positive, high<low, NaN/inf), wrong bucket alignment,
empty payload, 1D grid, determinism.

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1d_v1.coverage import (
    CANONICAL_LOOKBACK,
    COVERAGE_COMPLETE,
    COVERAGE_INCOMPLETE,
    COVERAGE_INVALID,
    H1D_COVERAGE_VERSION,
    REASON_CANDLE_GAP,
    REASON_CONFLICTING_CANDLE,
    REASON_DUPLICATE_CANDLE,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_OHLC,
    REASON_INVALID_VOLUME,
    REASON_OUT_OF_ORDER_CANDLE,
    REASON_ROW_PARSE_ERROR,
    REASON_UNALIGNED_CANDLE,
    TIMEFRAME_1D,
    TIMEFRAME_4H,
    TIMEFRAME_MS,
    canonical_window_bounds,
    evaluate_coverage,
    is_closed_at,
    latest_closed_open_ms,
)

TF = TIMEFRAME_MS[TIMEFRAME_4H]        # 14_400_000
DAY = TIMEFRAME_MS[TIMEFRAME_1D]       # 86_400_000
BASE_MS = 31 * DAY                     # 1970-02-01T00:00:00Z — on both grids


def _row(open_ms, *, o=100.0, h=101.0, low=99.0, c=100.5, v=1.0):
    return {
        "open_ms": open_ms,
        "open": float(o),
        "high": float(h),
        "low": float(low),
        "close": float(c),
        "volume": float(v),
    }


def _rows(latest=BASE_MS, n=60, tf=TF):
    """``n`` contiguous closed rows ending at ``latest`` (descending to 0)."""
    return [_row(latest - i * tf) for i in range(n)][::-1]


def _asof_for_close(open_ms, tf=TF):
    """ASOF (µs) exactly at the CLOSE instant of the bucket ``open_ms``."""
    return (open_ms + tf) * 1000


# ─── canonical 60/60 vs 59/60 ───────────────────────────────────────────────


class TestCanonicalCoverage:
    def test_60_of_60_complete(self):
        result = evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_COMPLETE
        assert result.ok is True
        assert result.present_buckets == 60 == result.expected_buckets
        assert result.missing_open_ms == ()
        assert result.closed_row_count == 60

    def test_59_of_60_is_incomplete_coverage_with_exact_missing_bucket(self):
        rows = _rows(n=59)  # contiguous source, one bucket short at the head
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INCOMPLETE
        assert result.reason == REASON_INSUFFICIENT_HISTORY
        assert result.present_buckets == 59
        window_open, _ = canonical_window_bounds(
            TIMEFRAME_4H, _asof_for_close(BASE_MS))
        assert result.missing_open_ms == (window_open,)
        assert result.window_open_ms == window_open

    def test_59_of_60_missing_latest_bucket_is_reported(self):
        rows = _rows(n=60)
        dropped = rows[:-1]  # the newest bucket is missing (stale tail)
        result = evaluate_coverage(
            dropped, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INCOMPLETE
        assert result.missing_open_ms == (BASE_MS,)
        assert result.present_buckets == 59

    def test_empty_payload_is_invalid_source(self):
        result = evaluate_coverage(
            [], timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_EMPTY_SERIES
        assert result.present_buckets == 0
        assert len(result.missing_open_ms) == 60

    def test_zero_closed_rows_is_incomplete_not_fabricated(self):
        rows = [_row(BASE_MS + (i + 1) * TF) for i in range(3)]
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INCOMPLETE
        assert result.closed_row_count == 0
        assert len(result.dropped_unclosed_opens) == 3

    def test_1d_grid_60_of_60(self):
        latest = 365 * DAY  # far enough from the epoch for 60 daily rows
        rows = _rows(latest=latest, n=60, tf=DAY)
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_1D, asof_us=_asof_for_close(latest, DAY))
        assert result.status == COVERAGE_COMPLETE
        assert result.present_buckets == 60

    def test_result_carries_full_diagnostics_dict(self):
        result = evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        data = result.to_dict()
        for key in ("status", "reason", "missing_open_ms", "present_buckets",
                    "expected_buckets", "row_count", "closed_row_count",
                    "dropped_unclosed_opens", "window_open_ms",
                    "window_latest_open_ms", "asof_us", "asof_ms"):
            assert key in data
        assert data["coverage_version"] == H1D_COVERAGE_VERSION


# ─── ASOF close boundaries (T-1ms / T / T+1ms and ±1µs) ─────────────────────


class TestAsofBoundaries:
    """The close instant of BASE_MS is T = (BASE_MS + TF) * 1000 µs.

    Operative rule: a candle is admissible iff ``close_us <= asof_us``.
    """

    def _probe(self, asof_us):
        return evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=asof_us)

    def test_exact_close_instant_is_admissible(self):
        result = self._probe(_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_COMPLETE
        assert result.last_closed_open_ms == BASE_MS

    def test_one_microsecond_before_close_is_not_admissible(self):
        result = self._probe(_asof_for_close(BASE_MS) - 1)
        assert result.status == COVERAGE_INCOMPLETE
        assert result.last_closed_open_ms == BASE_MS - TF
        assert BASE_MS in result.dropped_unclosed_opens

    def test_one_microsecond_after_close_is_admissible(self):
        result = self._probe(_asof_for_close(BASE_MS) + 1)
        assert result.status == COVERAGE_COMPLETE
        assert result.last_closed_open_ms == BASE_MS

    def test_one_millisecond_before_close_is_not_admissible(self):
        result = self._probe(_asof_for_close(BASE_MS) - 1000)
        assert result.status == COVERAGE_INCOMPLETE
        assert result.last_closed_open_ms == BASE_MS - TF

    def test_one_millisecond_after_close_is_admissible(self):
        result = self._probe(_asof_for_close(BASE_MS) + 1000)
        assert result.status == COVERAGE_COMPLETE

    def test_is_closed_at_boundary_precision(self):
        close_us = _asof_for_close(BASE_MS)
        assert is_closed_at(BASE_MS, TF, close_us) is True
        assert is_closed_at(BASE_MS, TF, close_us - 1) is False
        assert is_closed_at(BASE_MS, TF, close_us + 1) is True

    def test_latest_closed_open_ms_matches_grid(self):
        assert latest_closed_open_ms(_asof_for_close(BASE_MS), TF) == BASE_MS
        assert latest_closed_open_ms(_asof_for_close(BASE_MS) - 1, TF) == BASE_MS - TF


# ─── future / unclosed candles — no look-ahead, no fabrication ──────────────


class TestFutureCandles:
    def test_unclosed_and_future_rows_are_dropped_and_reported(self):
        rows = _rows() + [_row(BASE_MS + TF), _row(BASE_MS + 10 * TF)]
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_COMPLETE
        assert result.dropped_unclosed_opens == (BASE_MS + TF, BASE_MS + 10 * TF)
        assert result.closed_row_count == 60
        assert result.row_count == 62

    def test_future_rows_never_change_an_earlier_asof_result(self):
        base = evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        with_future = evaluate_coverage(
            _rows() + [_row(BASE_MS + TF, o=9999.0, h=1.0, low=-5.0, c=float("nan"))],
            timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        # corrupt FUTURE OHLCV must not even be validated at this ASOF
        assert with_future.status == base.status == COVERAGE_COMPLETE
        assert with_future.missing_open_ms == base.missing_open_ms

    def test_future_row_before_closed_rows_is_out_of_order(self):
        rows = _rows(n=10)
        rows.append(_row(BASE_MS + 5 * TF))   # future, ascending
        rows.append(_row(BASE_MS - 100 * TF))  # back in time → disorder
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_OUT_OF_ORDER_CANDLE


# ─── strict structural / OHLCV checks → INVALID_SOURCE ─────────────────────


class TestInvalidSource:
    def test_gap_inside_delivered_series_is_invalid(self):
        rows = _rows()
        del rows[30]
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_CANDLE_GAP

    def test_gap_even_outside_window_is_invalid(self):
        rows = _rows(n=80)
        del rows[2]  # hole far before the canonical window
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_CANDLE_GAP

    def test_identical_duplicate_is_invalid(self):
        rows = _rows()
        rows.append(_row(BASE_MS - 30 * TF))
        rows.sort(key=lambda row: row["open_ms"])
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_DUPLICATE_CANDLE

    def test_conflicting_duplicate_is_invalid(self):
        rows = _rows()
        rows.append(_row(BASE_MS - 30 * TF, o=123.0))
        rows.sort(key=lambda row: row["open_ms"])
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_CONFLICTING_CANDLE

    def test_out_of_order_is_invalid(self):
        rows = _rows()
        rows[5], rows[6] = rows[6], rows[5]
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_OUT_OF_ORDER_CANDLE

    @pytest.mark.parametrize("bad", [
        {"o": 0.0}, {"o": -1.0}, {"h": 0.0}, {"low": -0.5}, {"c": -2.0},
        {"o": float("nan")}, {"h": float("inf")}, {"c": float("nan")},
        {"low": 105.0}, {"h": 95.0}, {"o": 102.0, "h": 101.0},
        {"c": 98.0, "low": 99.0},
    ])
    def test_invalid_ohlc_is_invalid_source(self, bad):
        rows = _rows()
        rows[10] = _row(BASE_MS - 49 * TF, **bad)
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_INVALID_OHLC

    @pytest.mark.parametrize("bad", [{"v": -1.0}, {"v": float("nan")},
                                     {"v": float("inf")}])
    def test_invalid_volume_is_invalid_source(self, bad):
        rows = _rows()
        rows[10] = _row(BASE_MS - 49 * TF, **bad)
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_INVALID_VOLUME

    def test_wrong_bucket_alignment_is_invalid(self):
        rows = _rows()
        rows[7] = _row(rows[7]["open_ms"] + 1)
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_UNALIGNED_CANDLE

    def test_malformed_row_is_invalid(self):
        rows = _rows()
        rows[3] = {"open_ms": rows[3]["open_ms"]}  # no OHLCV fields
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_INVALID_OHLC

    def test_wrong_arity_row_is_invalid(self):
        rows = _rows()
        rows[3] = [rows[3]["open_ms"], 1.0, 2.0]  # only 3 fields
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_ROW_PARSE_ERROR

    def test_non_integer_open_ms_is_invalid(self):
        rows = _rows()
        rows[3] = _row(rows[3]["open_ms"])
        rows[3]["open_ms"] = "not-a-number"
        result = evaluate_coverage(
            rows, timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert result.status == COVERAGE_INVALID
        assert result.reason == REASON_ROW_PARSE_ERROR


# ─── argument validation + helpers ─────────────────────────────────────────


class TestArguments:
    @pytest.mark.parametrize("asof", [True, -1, 1.5, "5", None])
    def test_invalid_asof_rejected(self, asof):
        with pytest.raises(ValueError):
            evaluate_coverage(_rows(), timeframe=TIMEFRAME_4H, asof_us=asof)

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError):
            evaluate_coverage(_rows(), timeframe="1H", asof_us=10 ** 12)

    def test_canonical_window_bounds(self):
        window_open, window_close = canonical_window_bounds(
            TIMEFRAME_4H, _asof_for_close(BASE_MS))
        assert window_close == BASE_MS + TF
        assert window_open == BASE_MS - 59 * TF
        assert (window_close - window_open) == CANONICAL_LOOKBACK * TF

    def test_evaluation_is_deterministic(self):
        first = evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        second = evaluate_coverage(
            _rows(), timeframe=TIMEFRAME_4H, asof_us=_asof_for_close(BASE_MS))
        assert first == second
