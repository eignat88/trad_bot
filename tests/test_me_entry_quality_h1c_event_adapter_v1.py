"""Issue #308 / H1-C.3 — event adapter tests.

Covers: verbatim lineage preservation, exact UTC ms anchoring (incl.
offsets), naive timestamps rejected, ``signal_time``-as-ASOF contract
(never ``captured_at``), duplicate source events, invalid rows with
explicit reasons, immutable records, deterministic ordering.

Deterministic and offline: no PostgreSQL, no Bybit, no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_h1c_v1.event_adapter import (
    H1C_EVENT_ADAPTER_VERSION,
    REASON_DUPLICATE_SOURCE_EVENT,
    REASON_INVALID_CAPTURED_AT,
    REASON_INVALID_SIGNAL_TIME,
    REASON_MISSING_LINEAGE_HASH,
    REASON_MISSING_SOURCE_EVENT_KEY,
    REASON_MISSING_SOURCE_IDENTITY,
    REASON_MISSING_SYMBOL,
    REASON_ROW_PARSE_ERROR,
    SourceEventRecord,
    adapt_source_events,
    classification_asof_ms,
    parse_utc_ms,
)

DAY = 86_400_000
BASE_MS = 31 * DAY  # 1970-02-01T00:00:00Z


def _event_row(key="evt-1", **overrides):
    row = {
        "source_event_key": key,
        "source_identity": "ME_UPSTREAM_SOURCE_V1::scanner::v3",
        "symbol": "BTCUSDT",
        "signal_time": BASE_MS,
        "captured_at": BASE_MS + 2_000,
        "activation_freeze_ts": BASE_MS - DAY,
        "lineage_hash": "a" * 64,
        "source_fingerprint": "fp-1",
        "signal_candle_open_time": BASE_MS - 300_000,
        "entry_timeframe": "5",
    }
    row.update(overrides)
    return row


class TestParseUtcMs:
    def test_int_passthrough(self):
        assert parse_utc_ms(123456789) == 123456789

    def test_integral_float(self):
        assert parse_utc_ms(123456789.0) == 123456789

    def test_non_integral_float_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms(1.5)

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms(True)

    def test_aware_datetime_exact(self):
        dt = datetime(1970, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert parse_utc_ms(dt) == BASE_MS

    def test_aware_datetime_with_offset_same_instant(self):
        east = datetime(1970, 2, 1, 3, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        assert parse_utc_ms(east) == BASE_MS

    def test_aware_datetime_microsecond_exact(self):
        dt = datetime(1970, 2, 1, 0, 0, 0, 250000, tzinfo=timezone.utc)
        assert parse_utc_ms(dt) == BASE_MS + 250

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms(datetime(1970, 2, 1))

    def test_iso_zulu(self):
        assert parse_utc_ms("1970-02-01T00:00:00Z") == BASE_MS

    def test_iso_offset_same_instant(self):
        assert parse_utc_ms("1970-02-01T03:00:00+03:00") == BASE_MS

    def test_iso_naive_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms("1970-02-01T00:00:00")

    def test_iso_without_offset_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms("1970-02-01")

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            parse_utc_ms("not-a-timestamp")


class TestAdaptSourceEvents:
    def test_fields_preserved_verbatim(self):
        batch = adapt_source_events([_event_row()])
        assert len(batch.records) == 1
        record = batch.records[0]
        assert record.source_event_key == "evt-1"
        assert record.source_identity == "ME_UPSTREAM_SOURCE_V1::scanner::v3"
        assert record.signal_time_ms == BASE_MS
        assert record.activation_freeze_ts_ms == BASE_MS - DAY
        assert record.lineage_hash == "a" * 64
        assert record.symbol == "BTCUSDT"
        assert record.captured_at_ms == BASE_MS + 2_000

    def test_lineage_payload_exact_keys(self):
        record = adapt_source_events([_event_row()]).records[0]
        assert set(record.lineage_payload()) == {
            "source_event_key",
            "source_identity",
            "signal_time_ms",
            "activation_freeze_ts_ms",
            "lineage_hash",
        }

    def test_record_to_dict_roundtrip(self):
        record = adapt_source_events([_event_row()]).records[0]
        d = record.to_dict()
        assert d["source_event_key"] == "evt-1"
        assert d["entry_timeframe"] == "5"
        assert d["signal_candle_open_time_ms"] == BASE_MS - 300_000

    def test_record_is_immutable(self):
        record = adapt_source_events([_event_row()]).records[0]
        with pytest.raises(Exception):
            record.symbol = "ETHUSDT"

    def test_duplicate_source_event_rejected_explicitly(self):
        batch = adapt_source_events([_event_row(), _event_row()])
        assert len(batch.records) == 1
        assert len(batch.rejections) == 1
        assert batch.rejections[0].reason == REASON_DUPLICATE_SOURCE_EVENT
        assert batch.rejections[0].source_event_key == "evt-1"

    def test_distinct_keys_all_kept_in_order(self):
        batch = adapt_source_events(
            [_event_row("evt-1"), _event_row("evt-2"), _event_row("evt-3")]
        )
        assert [r.source_event_key for r in batch.records] == [
            "evt-1",
            "evt-2",
            "evt-3",
        ]

    def test_missing_key_rejected(self):
        row = _event_row()
        row.pop("source_event_key")
        batch = adapt_source_events([row])
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_MISSING_SOURCE_EVENT_KEY

    def test_missing_identity_rejected(self):
        batch = adapt_source_events([_event_row(source_identity="")])
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_MISSING_SOURCE_IDENTITY

    def test_missing_lineage_hash_rejected(self):
        batch = adapt_source_events([_event_row(lineage_hash="")])
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_MISSING_LINEAGE_HASH

    def test_missing_symbol_rejected(self):
        batch = adapt_source_events([_event_row(symbol="")])
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_MISSING_SYMBOL

    def test_naive_signal_time_rejected(self):
        batch = adapt_source_events(
            [_event_row(signal_time="1970-02-01T00:00:00")]
        )
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_INVALID_SIGNAL_TIME

    def test_naive_captured_at_rejected(self):
        batch = adapt_source_events(
            [_event_row(captured_at="1970-02-01T00:00:02")]
        )
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_INVALID_CAPTURED_AT

    def test_non_mapping_row_rejected(self):
        batch = adapt_source_events([42])
        assert batch.records == ()
        assert batch.rejections[0].reason == REASON_ROW_PARSE_ERROR

    def test_mixed_batch_deterministic(self):
        good = _event_row("evt-1")
        dup = _event_row("evt-1")
        bad = _event_row("evt-2", lineage_hash="")
        batch = adapt_source_events([good, dup, bad])
        assert [r.source_event_key for r in batch.records] == ["evt-1"]
        assert [r.reason for r in batch.rejections] == [
            REASON_DUPLICATE_SOURCE_EVENT,
            REASON_MISSING_LINEAGE_HASH,
        ]
        assert batch.rejections[0].row_index == 1
        assert batch.rejections[1].row_index == 2
        assert batch.source_row_count == 3
        assert batch.adapter_version == H1C_EVENT_ADAPTER_VERSION

    def test_rerun_identical(self):
        rows = [_event_row("evt-1"), _event_row("evt-2")]
        first = adapt_source_events(rows)
        second = adapt_source_events([_event_row("evt-1"), _event_row("evt-2")])
        assert first.to_dict() == second.to_dict()

    def test_optional_fields_default(self):
        row = _event_row()
        row.pop("source_fingerprint")
        row.pop("signal_candle_open_time")
        row.pop("entry_timeframe")
        record = adapt_source_events([row]).records[0]
        assert record.source_fingerprint == ""
        assert record.signal_candle_open_time_ms is None
        assert record.entry_timeframe is None


class TestClassificationAsof:
    def test_asof_is_signal_time_not_captured_at(self):
        record = adapt_source_events(
            [_event_row(signal_time=BASE_MS, captured_at=BASE_MS + 9 * DAY)]
        ).records[0]
        assert classification_asof_ms(record) == BASE_MS
        assert classification_asof_ms(record) != record.captured_at_ms

    def test_captured_at_never_changes_asof(self):
        a = adapt_source_events([_event_row(captured_at=BASE_MS + 1)]).records[0]
        b = adapt_source_events([_event_row(captured_at=BASE_MS + 9 * DAY)]).records[0]
        assert classification_asof_ms(a) == classification_asof_ms(b)
