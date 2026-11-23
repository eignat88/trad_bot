"""Issue #296: source contract unit tests."""

from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_entry_quality_v1.contracts import (
    SOURCE_SCANNER,
    SourceValidationError,
    lineage_hash,
    validate_source_event,
)


UTC = timezone.utc


def dt(minute: int) -> datetime:
    return datetime(2026, 11, 23, 15, minute, tzinfo=UTC)


def candidate():
    signal = dt(10)

    lineage = {
        "source_identity": "test-source",
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": 1795446600000,
        "source_scanner": SOURCE_SCANNER,
        "source_direction": "LONG",
        "source_features": {
            "rsi_14": 68.0,
            "rsi_delta_3": 2.4,
            "entry_confirmation_rsi_rising": True,
        },
        "source_pipeline_fingerprint": "test-fingerprint",
    }

    return {
        "event_key": "a" * 64,
        "symbol": "ETHUSDT",
        "signal_time": signal,
        "created_at": signal + timedelta(seconds=1),
        "captured_at": signal + timedelta(milliseconds=100),
        "source_scanner": SOURCE_SCANNER,
        "source_direction": "LONG",
        "capture_route": "PRE_FILTER_SOURCE",
        "signal_candle_open_time": 1795446600000,
        "lineage": lineage,
    }


def test_valid_event():
    row = candidate()

    result = validate_source_event(row, freeze_ts=dt(0))

    assert result.symbol == "ETHUSDT"
    assert result.event_key == "a" * 64
    assert len(result.lineage_hash) == 64


def test_freeze_boundary():
    row = candidate()

    with pytest.raises(
        SourceValidationError,
        match="PRE_FREEZE_SIGNAL",
    ):
        validate_source_event(row, freeze_ts=row["signal_time"])


def test_old_source_row():
    row = candidate()
    row["created_at"] = dt(0) - timedelta(seconds=1)

    with pytest.raises(
        SourceValidationError,
        match="PRE_FREEZE_SOURCE_ROW",
    ):
        validate_source_event(row, freeze_ts=dt(0))


def test_incomplete_lineage():
    row = candidate()
    del row["lineage"]["source_features"]

    with pytest.raises(
        SourceValidationError,
        match="INCOMPLETE_LINEAGE",
    ):
        validate_source_event(row, freeze_ts=dt(0))


def test_wrong_source():
    row = candidate()
    row["source_scanner"] = "OTHER_SCANNER"

    with pytest.raises(
        SourceValidationError,
        match="INVALID_SOURCE_SCANNER",
    ):
        validate_source_event(row, freeze_ts=dt(0))


def test_lineage_hash_deterministic():
    a = {"rsi": 68, "symbol": "ETHUSDT"}
    b = {"symbol": "ETHUSDT", "rsi": 68}

    assert lineage_hash(a) == lineage_hash(b)


def test_lineage_signal_time_mismatch():
    row = candidate()
    row["lineage"]["source_signal_time"] = dt(11).isoformat()

    with pytest.raises(
        SourceValidationError,
        match="LINEAGE_SIGNAL_TIME_MISMATCH",
    ):
        validate_source_event(row, freeze_ts=dt(0))


def test_source_identity_preserved():
    row = candidate()
    row["source_setup_id"] = "SOURCE-TEST-001"
    row["source_scanner_version"] = "1.0.0"
    row["entry_timeframe"] = "5m"
    row["source_reference_price"] = 123.45

    event = validate_source_event(row, freeze_ts=dt(0))

    assert event.source_setup_id == "SOURCE-TEST-001"
    assert event.source_scanner == SOURCE_SCANNER
    assert event.source_scanner_version == "1.0.0"
    assert event.source_direction == "LONG"
    assert event.entry_timeframe == "5m"
    assert event.signal_candle_open_time == 1795446600000
    assert event.source_reference_price == 123.45
