"""Issue #298 — H2 5m candle provider: integrity, grid, ASOF, provenance.

``validate_5m_candles`` mirrors the Issue #297 validator semantics on
``Candle`` objects; raw source rows are parsed fail-closed at the
``provide_5m_candles`` boundary.

Offline: no DB, no network, no filesystem, no clock.
"""
from __future__ import annotations

import math

import pytest

from app.models import Candle
from app.research.me_entry_quality_h1_v1.htf_candles import (
    REASON_CANDLE_GAP,
    REASON_DUPLICATE_CANDLE,
    REASON_CONFLICTING_CANDLE,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_OHLC,
    REASON_INVALID_TIMESTAMP,
    REASON_INVALID_VOLUME,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    REASON_NO_CLOSED_CANDLES,
    REASON_OUT_OF_ORDER_CANDLE,
    REASON_UNALIGNED_CANDLE,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    REASON_INVALID_SOURCE_IDENTITY,
    REASON_ROW_PARSE_ERROR,
    REASON_SOURCE_PAYLOAD_MISMATCH,
    REASON_UNKNOWN_SOURCE_KIND,
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    MIN_CLOSED_CANDLES_RSI_SEED,
    TIMEFRAME_5M,
    build_source_identity,
    provide_5m_candles,
    source_payload_hash,
    validate_5m_candles,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS

GOOD_CLOSES = [100.0 + i for i in range(20)]


def rows_from_closes(closes, start_index=0):
    rows = []
    for i, close in enumerate(closes):
        open_ms = BASE_MS + (start_index + i) * MS
        prev = rows[-1]["close"] if rows else float(close)
        rows.append({
            "open_ms": open_ms,
            "open": prev,
            "high": max(prev, float(close)) + 0.5,
            "low": min(prev, float(close)) - 0.5,
            "close": float(close),
            "volume": 1.0,
        })
    return rows


def candles_from_closes(closes, start_index=0):
    rows = rows_from_closes(closes, start_index=start_index)
    return [
        Candle(r["open_ms"], r["open"], r["high"], r["low"], r["close"], r["volume"])
        for r in rows
    ]


def _identity(rows, kind=SOURCE_KIND_OFFLINE_SNAPSHOT, label="offline-test"):
    return build_source_identity(kind, label, rows)


class TestGridAndBoundaries:
    def test_valid_series_passes(self):
        rows = rows_from_closes(GOOD_CLOSES)
        asof = BASE_MS + 20 * MS
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=asof, source_identity=_identity(rows)
        )
        assert set_.ok
        assert set_.reason == "OK"
        assert len(set_.candles) == 20
        assert TIMEFRAME_5M == "5m"

    def test_closed_iff_open_plus_300k_le_asof_t_minus_1ms(self):
        candles = candles_from_closes(GOOD_CLOSES)
        # candle index 19 closes exactly at BASE + 20*MS
        result = validate_5m_candles(
            candles, evaluation_asof_ms=BASE_MS + 20 * MS - 1
        )
        assert result.ok
        assert len(result.closed_candles) == 19
        assert len(result.dropped_unclosed) == 1

    def test_closed_iff_open_plus_300k_le_asof_exact_close(self):
        candles = candles_from_closes(GOOD_CLOSES)
        result = validate_5m_candles(
            candles, evaluation_asof_ms=BASE_MS + 20 * MS
        )
        assert result.ok
        assert len(result.closed_candles) == 20
        assert result.dropped_unclosed == ()

    def test_closed_iff_open_plus_300k_le_asof_t_plus_1ms(self):
        candles = candles_from_closes(GOOD_CLOSES)
        result = validate_5m_candles(
            candles, evaluation_asof_ms=BASE_MS + 20 * MS + 1
        )
        assert result.ok
        assert len(result.closed_candles) == 20

    def test_off_grid_open_rejected(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[3]
        candles[3] = Candle(
            c.timestamp + 60_000, c.open, c.high, c.low, c.close, c.volume
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_UNALIGNED_CANDLE

    def test_non_integer_timestamp_rejected(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[2]
        candles[2] = Candle(
            c.timestamp + 0.5, c.open, c.high, c.low, c.close, c.volume
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_TIMESTAMP

    def test_empty_series(self):
        result = validate_5m_candles([], evaluation_asof_ms=BASE_MS)
        assert not result.ok
        assert result.reason == REASON_EMPTY_SERIES

    def test_no_closed_candles_at_asof(self):
        candles = candles_from_closes(GOOD_CLOSES)
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS)
        assert not result.ok
        assert result.reason == REASON_NO_CLOSED_CANDLES

    def test_missing_latest_closed_bucket(self):
        candles = candles_from_closes(GOOD_CLOSES[:15])
        result = validate_5m_candles(
            candles, evaluation_asof_ms=BASE_MS + 20 * MS
        )
        assert not result.ok
        assert result.reason == REASON_MISSING_LATEST_CLOSED_CANDLE

    def test_insufficient_history(self):
        candles = candles_from_closes(
            GOOD_CLOSES[: MIN_CLOSED_CANDLES_RSI_SEED - 1]
        )
        result = validate_5m_candles(
            candles, evaluation_asof_ms=BASE_MS + 14 * MS
        )
        assert not result.ok
        assert result.reason == REASON_INSUFFICIENT_HISTORY


class TestOrderAndDuplicates:
    def test_duplicate_identical_candle(self):
        candles = candles_from_closes(GOOD_CLOSES)
        candles.insert(5, candles[5])
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_DUPLICATE_CANDLE

    def test_conflicting_candle(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[5]
        candles.insert(
            5, Candle(c.timestamp, c.open + 1.0, c.high, c.low, c.close, c.volume)
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_CONFLICTING_CANDLE

    def test_out_of_order_candle(self):
        candles = candles_from_closes(GOOD_CLOSES)
        candles[4], candles[6] = candles[6], candles[4]
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_OUT_OF_ORDER_CANDLE

    def test_gap_rejected(self):
        candles = candles_from_closes(GOOD_CLOSES)
        del candles[7]
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_CANDLE_GAP

    def test_closed_row_hidden_after_future_suffix_rejected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        future = rows_from_closes([99.0], start_index=25)  # future bucket
        late_closed = rows_from_closes([98.0], start_index=21)  # closed but late
        asof = BASE_MS + 22 * MS
        payload = rows + future + late_closed
        set_ = provide_5m_candles(
            payload, evaluation_asof_ms=asof, source_identity=_identity(payload)
        )
        assert not set_.ok
        assert set_.reason == REASON_OUT_OF_ORDER_CANDLE


class TestOhlcvValidity:
    def test_low_above_high(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[6]
        candles[6] = Candle(
            c.timestamp, c.open, c.high, c.high + 1.0, c.close, c.volume
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_OHLC

    def test_open_outside_range(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[6]
        candles[6] = Candle(
            c.timestamp, c.high + 1.0, c.high, c.low, c.close, c.volume
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_OHLC

    def test_non_positive_price(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[6]
        candles[6] = Candle(c.timestamp, c.open, c.high, 0.0, 0.0, c.volume)
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_OHLC

    def test_negative_volume(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[6]
        candles[6] = Candle(
            c.timestamp, c.open, c.high, c.low, c.close, -1.0
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_VOLUME

    def test_non_finite_close_rejected(self):
        candles = candles_from_closes(GOOD_CLOSES)
        c = candles[6]
        candles[6] = Candle(
            c.timestamp, c.open, c.high, c.low, math.nan, c.volume
        )
        result = validate_5m_candles(candles, evaluation_asof_ms=BASE_MS + 20 * MS)
        assert not result.ok
        assert result.reason == REASON_INVALID_OHLC

    def test_nan_close_in_raw_row_fails_at_parse_boundary(self):
        rows = rows_from_closes(GOOD_CLOSES)
        rows[6]["close"] = math.nan
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS,
            source_identity=_identity(rows),
        )
        assert not set_.ok
        assert set_.reason == REASON_ROW_PARSE_ERROR

    def test_non_integer_open_ms_in_raw_row_fails_at_parse_boundary(self):
        rows = rows_from_closes(GOOD_CLOSES)
        rows[2]["open_ms"] = BASE_MS + 2 * MS + 0.5
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS,
            source_identity=_identity(rows),
        )
        assert not set_.ok
        assert set_.reason == REASON_ROW_PARSE_ERROR


class TestSourceProvenance:
    def test_payload_hash_tamper_detected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        identity = _identity(rows)
        rows[6]["close"] += 1.0  # tamper AFTER identity was minted
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS, source_identity=identity
        )
        assert not set_.ok
        assert set_.reason == REASON_SOURCE_PAYLOAD_MISMATCH

    def test_row_count_mismatch_detected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        identity = _identity(rows)
        set_ = provide_5m_candles(
            rows[:-1], evaluation_asof_ms=BASE_MS + 20 * MS,
            source_identity=identity,
        )
        assert not set_.ok
        assert set_.reason == REASON_SOURCE_PAYLOAD_MISMATCH

    def test_unknown_source_kind_rejected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        identity = _identity(rows)
        tampered = type(identity)(
            source_kind="MYSTERY_FEED",
            source_label=identity.source_label,
            fetched_at_ms=identity.fetched_at_ms,
            row_count=identity.row_count,
            payload_hash=identity.payload_hash,
        )
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS, source_identity=tampered
        )
        assert not set_.ok
        assert set_.reason == REASON_UNKNOWN_SOURCE_KIND

    def test_non_hash_payload_rejected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        identity = _identity(rows)
        bad = type(identity)(
            source_kind=identity.source_kind,
            source_label=identity.source_label,
            fetched_at_ms=identity.fetched_at_ms,
            row_count=identity.row_count,
            payload_hash="not-a-hash",
        )
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS, source_identity=bad
        )
        assert not set_.ok
        assert set_.reason == REASON_INVALID_SOURCE_IDENTITY

    def test_empty_label_rejected(self):
        rows = rows_from_closes(GOOD_CLOSES)
        identity = _identity(rows)
        bad = type(identity)(
            source_kind=identity.source_kind,
            source_label=" ",
            fetched_at_ms=identity.fetched_at_ms,
            row_count=identity.row_count,
            payload_hash=identity.payload_hash,
        )
        set_ = provide_5m_candles(
            rows, evaluation_asof_ms=BASE_MS + 20 * MS, source_identity=bad
        )
        assert not set_.ok
        assert set_.reason == REASON_INVALID_SOURCE_IDENTITY

    def test_payload_hash_is_order_insensitive(self):
        rows = rows_from_closes(GOOD_CLOSES)
        shuffled = list(reversed(rows))
        assert source_payload_hash(rows) == source_payload_hash(shuffled)

    def test_future_suffix_is_dropped_and_reported_never_used(self):
        rows = rows_from_closes(GOOD_CLOSES)
        future = rows_from_closes([123.0, 124.0], start_index=20)
        payload = rows + future
        asof = BASE_MS + 20 * MS
        set_ = provide_5m_candles(
            payload, evaluation_asof_ms=asof, source_identity=_identity(payload)
        )
        assert set_.ok
        assert len(set_.candles) == 20
        assert len(set_.dropped_unclosed) == 2
        assert all(c.close in (123.0, 124.0) for c in set_.dropped_unclosed)

    def test_corrupt_future_rows_cannot_invalidate_history(self):
        rows = rows_from_closes(GOOD_CLOSES)
        future_bad = [{"open_ms": BASE_MS + 20 * MS, "open": "junk",
                       "high": None, "low": -3, "close": float("nan"),
                       "volume": "x"}]
        payload = rows + future_bad
        set_ = provide_5m_candles(
            payload, evaluation_asof_ms=BASE_MS + 20 * MS,
            source_identity=_identity(payload),
        )
        assert set_.ok
        assert len(set_.candles) == 20
