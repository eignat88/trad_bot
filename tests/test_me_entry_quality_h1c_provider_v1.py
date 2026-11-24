"""Issue #308 / H1-C.2 — read-only HTF provider tests.

Covers: source identity + payload hash, row parsing, strict causal
validation (ASOF boundary, unclosed/future exclusion, gaps, duplicates,
bad OHLCV), stale tail, insufficient history, canonical-window coverage
(COMPLETE / PARTIAL / NONE), tamper detection, determinism.

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

import math

import pytest

from app.research.me_entry_quality_h1_v1.htf_candles import (
    MIN_CLOSED_CANDLES,
    REASON_CANDLE_GAP,
    REASON_CONFLICTING_CANDLE,
    REASON_DUPLICATE_CANDLE,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_OHLC,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    REASON_UNALIGNED_CANDLE,
    TIMEFRAME_1D,
    TIMEFRAME_1D_MS,
    TIMEFRAME_4H,
    TIMEFRAME_4H_MS,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    COVERAGE_COMPLETE,
    COVERAGE_NONE,
    COVERAGE_PARTIAL,
    EMPTY_PAYLOAD_HASH,
    HtfSourceIdentity,
    REASON_INVALID_SOURCE_IDENTITY,
    REASON_ROW_PARSE_ERROR,
    REASON_SOURCE_PAYLOAD_MISMATCH,
    REASON_UNKNOWN_SOURCE_KIND,
    build_source_identity,
    provide_htf_candles,
    source_payload_hash,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_BYBIT_KLINE_V5,
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)

TF = TIMEFRAME_4H_MS
DAY = 86_400_000
BASE_MS = 31 * DAY  # 1970-02-01T00:00:00Z — on both the 4H and 1D grids
N = 10
ASOF = BASE_MS + N * TF + TF  # close of the 9th 4H candle


def _row(open_ms, high, low, *, close=None, volume=1.0):
    mid = (high + low) / 2.0
    return {
        "open_ms": open_ms,
        "open": mid,
        "high": float(high),
        "low": float(low),
        "close": mid if close is None else float(close),
        "volume": volume,
    }


def _rows(n=N, *, tf=TF, asof=ASOF):
    return [_row(asof - (n - i) * tf, 20.0 + i * 0.5, 10.0 + i * 0.5) for i in range(n)]


def _identity(rows, kind=SOURCE_KIND_OFFLINE_SNAPSHOT, label="test:4h"):
    return build_source_identity(kind, label, rows, fetched_at_ms=ASOF)


# ─── source identity ──────────────────────────────────────────────────────────


class TestSourceIdentity:
    def test_build_identity_captures_provenance(self):
        rows = _rows()
        ident = _identity(rows)
        assert ident.source_kind == SOURCE_KIND_OFFLINE_SNAPSHOT
        assert ident.source_label == "test:4h"
        assert ident.fetched_at_ms == ASOF
        assert ident.row_count == len(rows)
        assert len(ident.payload_hash) == 64

    def test_identity_to_dict_shape(self):
        ident = _identity(_rows())
        assert set(ident.to_dict()) == {
            "source_kind",
            "source_label",
            "fetched_at_ms",
            "row_count",
            "payload_hash",
        }

    def test_unknown_source_kind_rejected(self):
        with pytest.raises(ValueError):
            build_source_identity("MYSTERY_STORE", "x", _rows())

    def test_empty_label_rejected(self):
        with pytest.raises(ValueError):
            build_source_identity(SOURCE_KIND_OFFLINE_SNAPSHOT, "  ", _rows())

    def test_non_int_fetched_at_rejected(self):
        with pytest.raises(ValueError):
            build_source_identity(
                SOURCE_KIND_OFFLINE_SNAPSHOT, "x", _rows(), fetched_at_ms=1.5
            )

    def test_empty_payload_identity_rejected(self):
        with pytest.raises(ValueError):
            build_source_identity(SOURCE_KIND_OFFLINE_SNAPSHOT, "x", [])

    def test_empty_multiset_hash_is_defined(self):
        assert source_payload_hash([]) == EMPTY_PAYLOAD_HASH
        assert source_payload_hash(()) == EMPTY_PAYLOAD_HASH
        assert len(EMPTY_PAYLOAD_HASH) == 64


class TestPayloadHash:
    def test_hash_is_order_insensitive(self):
        rows = _rows()
        assert source_payload_hash(rows) == source_payload_hash(list(reversed(rows)))

    def test_hash_changes_with_content(self):
        rows = _rows()
        tampered = [dict(r) for r in rows]
        tampered[3] = dict(tampered[3], close=tampered[3]["close"] + 1.0)
        assert source_payload_hash(tampered) != source_payload_hash(rows)

    def test_hash_is_rerun_identical(self):
        rows = _rows()
        assert source_payload_hash(rows) == source_payload_hash(_rows())


# ─── provide_htf_candles ──────────────────────────────────────────────────────


class TestProvideHtfCandles:
    def test_happy_path_returns_closed_candles_with_identity(self):
        rows = _rows()
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is True
        assert result.reason == "OK"
        assert len(result.candles) == N
        assert result.source_identity.row_count == N
        assert result.coverage.status == COVERAGE_COMPLETE
        assert result.coverage.missing_open_ms == ()

    def test_result_to_dict_shape(self):
        rows = _rows()
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        keys = set(result.to_dict())
        assert {
            "provider_version",
            "timeframe",
            "evaluation_asof_ms",
            "candle_count",
            "dropped_unclosed_count",
            "latest_closed_open_ms",
            "source_identity",
            "coverage",
            "ok",
            "reason",
            "detail",
        } <= keys

    def test_asof_boundary_inclusive_at_close(self):
        rows = _rows()
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        # the newest row [ASOF - TF, ASOF) is CLOSED exactly at ASOF
        assert result.candles[-1].timestamp == ASOF - TF
        assert result.latest_closed_open_ms == ASOF - TF

    def test_unclosed_and_future_rows_dropped_not_used(self):
        rows = _rows() + [
            _row(ASOF, 21.0, 19.0),  # opens exactly at ASOF — never closed
            _row(ASOF + TF, 22.0, 20.0),  # future
        ]
        ident = _identity(rows)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
            canonical_lookback=N + 2,
        )
        assert result.ok is True
        assert len(result.candles) == N
        assert len(result.dropped_unclosed) == 2
        assert result.coverage.dropped_unclosed_count == 2
        assert all(c.timestamp < ASOF for c in result.candles)

    def test_1d_grid_and_utc_midnight(self):
        asof = BASE_MS + 8 * DAY  # UTC midnight
        rows = [
            _row(asof - (8 - i) * DAY, 20.0 + i, 10.0 + i) for i in range(8)
        ]
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_1D,
            evaluation_asof_ms=asof,
            source_identity=build_source_identity(
                SOURCE_KIND_OFFLINE_SNAPSHOT, "test:1d", rows
            ),
            canonical_lookback=8,
        )
        assert result.ok is True
        assert result.candles[-1].timestamp == asof - DAY
        assert (asof - DAY) % TIMEFRAME_1D_MS == 0

    def test_gap_fails_closed(self):
        rows = _rows()
        del rows[4]
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_CANDLE_GAP

    def test_duplicate_and_conflicting_rows_fail_closed(self):
        rows = _rows()
        dup = list(rows) + [dict(rows[3])]
        result = provide_htf_candles(
            dup,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(dup),
            canonical_lookback=N,
        )
        assert result.reason == REASON_DUPLICATE_CANDLE

        conflict = list(rows) + [dict(rows[3], close=rows[3]["close"] + 1.0)]
        result = provide_htf_candles(
            conflict,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(conflict),
            canonical_lookback=N,
        )
        assert result.reason == REASON_CONFLICTING_CANDLE

    def test_off_grid_row_fails_closed(self):
        rows = _rows()
        rows[2] = dict(rows[2], open_ms=rows[2]["open_ms"] + 60_000)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_UNALIGNED_CANDLE

    def test_nan_row_fails_closed_at_parse(self):
        rows = _rows()
        rows[1] = dict(rows[1], high=math.nan)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_ROW_PARSE_ERROR

    def test_bad_ohlc_fails_closed(self):
        rows = _rows()
        rows[5] = dict(rows[5], low=rows[5]["high"] + 1.0)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_INVALID_OHLC

    def test_stale_tail_fails_closed(self):
        rows = _rows()[:-1]  # newest closed bucket missing at ASOF
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_MISSING_LATEST_CLOSED_CANDLE

    def test_stale_tail_allowed_only_by_explicit_override(self):
        rows = _rows()[:-1]
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
            require_latest_closed=False,
        )
        assert result.ok is True

    def test_insufficient_history_fails_closed(self):
        rows = _rows(n=MIN_CLOSED_CANDLES - 1)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_INSUFFICIENT_HISTORY

    def test_empty_payload_fails_closed(self):
        ident = HtfSourceIdentity(
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="empty",
            fetched_at_ms=None,
            row_count=0,
            payload_hash=EMPTY_PAYLOAD_HASH,
        )
        result = provide_htf_candles(
            [],
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
        )
        assert result.ok is False
        assert result.reason == REASON_EMPTY_SERIES

    def test_unknown_source_kind_fails_closed(self):
        rows = _rows()
        ident = HtfSourceIdentity(
            source_kind="UNVERIFIED_STORE",
            source_label="x",
            fetched_at_ms=None,
            row_count=len(rows),
            payload_hash="",
        )
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
        )
        assert result.ok is False
        assert result.reason == REASON_UNKNOWN_SOURCE_KIND

    def test_missing_identity_fails_closed(self):
        result = provide_htf_candles(
            _rows(),
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=None,
        )
        assert result.ok is False
        assert result.reason == REASON_INVALID_SOURCE_IDENTITY

    def test_empty_payload_hash_rejected(self):
        rows = _rows()
        ident = HtfSourceIdentity(
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="x",
            fetched_at_ms=None,
            row_count=len(rows),
            payload_hash="",  # empty hash is never acceptable
        )
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
        )
        assert result.ok is False
        assert result.reason == REASON_INVALID_SOURCE_IDENTITY

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "A" * 64,  # uppercase
            "a" * 63,  # short
            "a" * 65,  # long
            "z" * 64,  # non-hex
            "g" + "a" * 63,
        ],
    )
    def test_malformed_payload_hash_rejected(self, bad_hash):
        rows = _rows()
        ident = HtfSourceIdentity(
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="x",
            fetched_at_ms=None,
            row_count=len(rows),
            payload_hash=bad_hash,
        )
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
        )
        assert result.ok is False
        assert result.reason == REASON_INVALID_SOURCE_IDENTITY

    def test_row_count_verified_even_with_valid_hash(self):
        rows = _rows()
        ident = _identity(rows)
        shortened = HtfSourceIdentity(
            source_kind=ident.source_kind,
            source_label=ident.source_label,
            fetched_at_ms=ident.fetched_at_ms,
            row_count=ident.row_count + 1,
            payload_hash=ident.payload_hash,
        )
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=shortened,
        )
        assert result.ok is False
        assert result.reason == REASON_SOURCE_PAYLOAD_MISMATCH

    def test_tampered_rows_fail_closed(self):
        rows = _rows()
        ident = _identity(rows)
        tampered = [dict(r) for r in rows]
        tampered[3] = dict(tampered[3], close=tampered[3]["close"] + 1.0)
        result = provide_htf_candles(
            tampered,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
        )
        assert result.ok is False
        assert result.reason == REASON_SOURCE_PAYLOAD_MISMATCH

    def test_row_count_mismatch_fails_closed(self):
        rows = _rows()
        ident = _identity(rows)
        shortened = HtfSourceIdentity(
            source_kind=ident.source_kind,
            source_label=ident.source_label,
            fetched_at_ms=ident.fetched_at_ms,
            row_count=ident.row_count + 1,
            payload_hash=ident.payload_hash,
        )
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=shortened,
        )
        assert result.ok is False
        assert result.reason == REASON_SOURCE_PAYLOAD_MISMATCH


class TestCoverage:
    def test_partial_coverage_reports_missing_leading_buckets(self):
        rows = _rows(n=8)  # 8 of 10 canonical buckets
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=10,
        )
        assert result.ok is True
        assert result.coverage.status == COVERAGE_PARTIAL
        assert result.coverage.present_buckets == 8
        assert len(result.coverage.missing_open_ms) == 2
        assert result.coverage.missing_open_ms[0] == ASOF - 10 * TF

    def test_complete_coverage(self):
        rows = _rows(n=10)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=10,
        )
        assert result.coverage.status == COVERAGE_COMPLETE
        assert result.coverage.present_buckets == 10

    def test_coverage_diagnostics_on_invalid_series(self):
        # an interior gap fails validation, but coverage must still report
        # the ACTUAL present/missing buckets — never a bogus NONE
        rows = _rows(n=10)
        del rows[4]
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=10,
        )
        assert result.ok is False
        assert result.reason == REASON_CANDLE_GAP
        assert result.coverage.status == COVERAGE_PARTIAL
        assert result.coverage.present_buckets == 9
        assert result.coverage.missing_open_ms == (ASOF - 6 * TF,)

    def test_coverage_diagnostics_on_stale_series(self):
        rows = _rows(n=10)[:-1]  # newest closed bucket missing
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=10,
        )
        assert result.ok is False
        assert result.reason == REASON_MISSING_LATEST_CLOSED_CANDLE
        assert result.coverage.status == COVERAGE_PARTIAL
        assert result.coverage.present_buckets == 9
        assert result.coverage.missing_open_ms == (ASOF - TF,)

    def test_coverage_none_only_when_nothing_present(self):
        ident = HtfSourceIdentity(
            source_kind=SOURCE_KIND_OFFLINE_SNAPSHOT,
            source_label="empty",
            fetched_at_ms=None,
            row_count=0,
            payload_hash=EMPTY_PAYLOAD_HASH,
        )
        result = provide_htf_candles(
            [],
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
            canonical_lookback=10,
        )
        assert result.coverage.status == COVERAGE_NONE

    def test_coverage_to_dict_shape(self):
        rows = _rows(n=10)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=10,
        )
        assert set(result.coverage.to_dict()) == {
            "status",
            "expected_buckets",
            "present_buckets",
            "missing_open_ms",
            "window_open_ms",
            "window_close_ms",
            "dropped_unclosed_count",
        }
        assert result.coverage.window_close_ms == ASOF
        assert result.coverage.window_open_ms == ASOF - 10 * TF


class TestAsofIsolation:
    """Review fix 1: corrupt future/unclosed payloads never affect history."""

    def test_corrupt_future_rows_are_excluded_before_ohlc_validation(self):
        rows = _rows() + [
            {"open_ms": ASOF, "open": float("nan"), "high": float("nan"),
             "low": float("nan"), "close": float("nan"), "volume": float("nan")},
            {"open_ms": ASOF + TF, "open": -1.0, "high": float("inf"),
             "low": 5.0, "close": 2.0, "volume": -7.0},
        ]
        ident = _identity(rows)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
            canonical_lookback=N + 2,
        )
        assert result.ok is True
        assert result.reason == "OK"
        assert len(result.candles) == N
        assert result.coverage.dropped_unclosed_count == 2

    def test_corrupt_unclosed_boundary_row_is_excluded(self):
        rows = _rows() + [
            {"open_ms": ASOF, "open": float("nan"), "high": "garbage",
             "low": None, "close": float("nan"), "volume": "x"},
        ]
        ident = _identity(rows)
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=ident,
            canonical_lookback=N + 1,
        )
        assert result.ok is True
        assert len(result.candles) == N
        assert result.coverage.dropped_unclosed_count == 1

    def test_corrupt_closed_row_still_fails_closed(self):
        rows = _rows()
        rows[3] = dict(rows[3], high=float("nan"))
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        assert result.ok is False
        assert result.reason == REASON_ROW_PARSE_ERROR

    def test_future_corruption_cannot_change_past_result(self):
        clean = _rows()
        before = provide_htf_candles(
            clean,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(clean),
            canonical_lookback=N,
        )
        dirty = clean + [
            {"open_ms": ASOF + i * TF, "open": float("nan"), "high": None,
             "low": "x", "close": float("inf"), "volume": -1}
            for i in range(3)
        ]
        after = provide_htf_candles(
            dirty,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(dirty),
            canonical_lookback=N,
        )
        assert before.candles == after.candles
        assert before.ok is True and after.ok is True
        assert before.reason == after.reason


class TestDeterminism:
    def test_rerun_is_identical(self):
        rows = _rows()
        first = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N,
        )
        second = provide_htf_candles(
            _rows(),
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(_rows()),
            canonical_lookback=N,
        )
        assert first.to_dict() == second.to_dict()

    def test_future_append_does_not_change_past_result(self):
        rows = _rows()
        before = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows),
            canonical_lookback=N + 3,
        )
        extended = rows + [
            _row(ASOF + i * TF, 30.0, 20.0) for i in range(3)
        ]
        after = provide_htf_candles(
            extended,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(extended),
            canonical_lookback=N + 3,
        )
        # the validated CLOSED series is unchanged (future rows are dropped,
        # never used); only the drop diagnostics legitimately differ
        assert before.candles == after.candles
        assert before.ok == after.ok
        assert before.reason == after.reason
        assert before.latest_closed_open_ms == after.latest_closed_open_ms
        assert before.coverage.status == after.coverage.status
        assert before.coverage.missing_open_ms == after.coverage.missing_open_ms
        assert len(after.dropped_unclosed) == 3

    def test_invalid_timeframe_raises(self):
        rows = _rows()
        with pytest.raises(ValueError):
            provide_htf_candles(
                rows,
                timeframe="7H",
                evaluation_asof_ms=ASOF,
                source_identity=_identity(rows),
            )

    def test_invalid_asof_raises(self):
        rows = _rows()
        with pytest.raises(ValueError):
            provide_htf_candles(
                rows,
                timeframe=TIMEFRAME_4H,
                evaluation_asof_ms=True,
                source_identity=_identity(rows),
            )

    def test_bybit_kind_accepted(self):
        rows = _rows()
        result = provide_htf_candles(
            rows,
            timeframe=TIMEFRAME_4H,
            evaluation_asof_ms=ASOF,
            source_identity=_identity(rows, kind=SOURCE_KIND_BYBIT_KLINE_V5),
            canonical_lookback=N,
        )
        assert result.ok is True


def test_closed_row_hidden_after_future_suffix_fails_closed():
    """A future suffix must not conceal a later out-of-order closed row."""
    rows = _rows()
    rows += [_row(ASOF, 25.0, 15.0), rows[-1]]
    result = provide_htf_candles(
        rows, timeframe="4H", evaluation_asof_ms=ASOF,
        source_identity=_identity(rows), canonical_lookback=10,
    )
    assert not result.ok
    assert result.reason == "OUT_OF_ORDER_CANDLE"


def test_future_suffix_with_bad_ohlc_still_does_not_invalidate_history():
    rows = _rows()
    future = _row(ASOF, 25.0, 15.0)
    future["open"] = float("nan")
    rows.append(future)
    result = provide_htf_candles(
        rows, timeframe="4H", evaluation_asof_ms=ASOF,
        source_identity=_identity(rows), canonical_lookback=10,
    )
    assert result.ok
    assert result.coverage.status == "COMPLETE"
