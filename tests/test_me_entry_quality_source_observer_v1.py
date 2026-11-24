"""Issue #296: source contract unit tests.

Covers the PR #303 review hardening of RSI / source feature validation:
type checks (null / NaN / Infinity / string / boolean), RSI range,
strict confirmation flag, identity/lineage non-empty fields and
verbatim preservation of the original feature values.
"""

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
        "entry_timeframe": "5m",
        "signal_candle_open_time": 1795446600000,
        "lineage": lineage,
    }


def rejects(row, code, *, freeze_ts=None):
    with pytest.raises(SourceValidationError) as excinfo:
        validate_source_event(row, freeze_ts=freeze_ts or dt(0))
    assert excinfo.value.reason_code == code
    assert str(excinfo.value) == code


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


# ── Review hardening: RSI / feature value validation ────────────────


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"),
                                 float("-inf")])
def test_rsi_14_null_nan_infinity_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["rsi_14"] = bad

    rejects(row, "FEATURE_RSI14_NOT_FINITE")


@pytest.mark.parametrize("bad", ["68.0", "68", "", True, False, [68.0]])
def test_rsi_14_wrong_type_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["rsi_14"] = bad

    rejects(row, "FEATURE_RSI14_INVALID_TYPE")


@pytest.mark.parametrize("bad", [-0.1, -50, 100.1, 101])
def test_rsi_14_out_of_range_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["rsi_14"] = bad

    rejects(row, "FEATURE_RSI14_OUT_OF_RANGE")


@pytest.mark.parametrize("ok", [0, 0.0, 50, 68.0, 100, 100.0])
def test_rsi_14_boundaries_accepted(ok):
    row = candidate()
    row["lineage"]["source_features"]["rsi_14"] = ok

    validate_source_event(row, freeze_ts=dt(0))


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_rsi_delta_3_not_finite_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["rsi_delta_3"] = bad

    rejects(row, "FEATURE_RSI_DELTA_3_NOT_FINITE")


@pytest.mark.parametrize("bad", ["2.4", True, None])
def test_rsi_delta_3_wrong_type_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["rsi_delta_3"] = bad

    code = (
        "FEATURE_RSI_DELTA_3_NOT_FINITE"
        if bad is None
        else "FEATURE_RSI_DELTA_3_INVALID_TYPE"
    )
    rejects(row, code)


def test_rsi_delta_3_unbounded_within_finite_numbers():
    # No scanner contract constrains rsi_delta_3 further: any finite
    # number passes, including negative values.
    row = candidate()
    row["lineage"]["source_features"]["rsi_delta_3"] = -7.5

    validate_source_event(row, freeze_ts=dt(0))


@pytest.mark.parametrize("bad", ["true", "False", 1, 0, None, "yes"])
def test_confirmation_flag_non_boolean_rejected(bad):
    row = candidate()
    row["lineage"]["source_features"]["entry_confirmation_rsi_rising"] = bad

    rejects(row, "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE")


@pytest.mark.parametrize("ok", [True, False])
def test_confirmation_flag_real_boolean_accepted(ok):
    row = candidate()
    row["lineage"]["source_features"]["entry_confirmation_rsi_rising"] = ok

    validate_source_event(row, freeze_ts=dt(0))


def test_missing_required_feature_rejected():
    row = candidate()
    del row["lineage"]["source_features"]["rsi_delta_3"]

    rejects(row, "INCOMPLETE_FEATURES")


def test_original_feature_values_preserved_verbatim():
    # No recalculation or substitution: stored features equal the source.
    row = candidate()
    features = {
        "rsi_14": 68,                # int survives as int
        "rsi_delta_3": -3.25,
        "entry_confirmation_rsi_rising": False,
    }
    row["lineage"]["source_features"] = features

    event = validate_source_event(row, freeze_ts=dt(0))

    assert event.lineage["source_features"] == features
    assert event.lineage["source_features"]["rsi_14"] == 68
    assert type(event.lineage["source_features"]["rsi_14"]) is int


# ── Review hardening: identity / lineage formats ────────────────────


def test_empty_source_identity_rejected():
    row = candidate()
    row["lineage"]["source_identity"] = "   "

    rejects(row, "LINEAGE_IDENTITY_EMPTY")


def test_non_string_source_identity_rejected():
    row = candidate()
    row["lineage"]["source_identity"] = 123

    rejects(row, "LINEAGE_IDENTITY_INVALID_TYPE")


def test_empty_pipeline_fingerprint_rejected():
    row = candidate()
    row["lineage"]["source_pipeline_fingerprint"] = ""

    rejects(row, "LINEAGE_FINGERPRINT_EMPTY")


def test_non_string_pipeline_fingerprint_rejected():
    row = candidate()
    row["lineage"]["source_pipeline_fingerprint"] = None

    rejects(row, "LINEAGE_FINGERPRINT_INVALID_TYPE")


def test_missing_signal_time_rejected():
    row = candidate()
    del row["signal_time"]

    rejects(row, "MISSING_SIGNAL_TIME")


def test_missing_created_at_rejected():
    row = candidate()
    del row["created_at"]

    rejects(row, "MISSING_CREATED_AT")


def test_missing_captured_at_rejected():
    row = candidate()
    del row["captured_at"]

    rejects(row, "MISSING_CAPTURED_AT")


def test_empty_entry_timeframe_rejected():
    row = candidate()
    row["entry_timeframe"] = ""

    rejects(row, "INVALID_ENTRY_TIMEFRAME")


def test_boolean_candle_identity_rejected():
    row = candidate()
    row["lineage"]["source_signal_candle_open_time"] = True
    row["signal_candle_open_time"] = True

    rejects(row, "INVALID_CANDLE_IDENTITY")


def test_non_positive_candle_identity_rejected():
    row = candidate()
    row["lineage"]["source_signal_candle_open_time"] = 0
    row["signal_candle_open_time"] = 0

    rejects(row, "INVALID_CANDLE_IDENTITY")


def test_nan_hidden_in_extra_lineage_key_rejected():
    row = candidate()
    row["lineage"]["source_score"] = float("nan")

    rejects(row, "INVALID_LINEAGE_JSON")
