"""Issue #296: research-only prospective source event contracts.

Review hardening (PR #303):
* required source features are validated by TYPE and VALUE, not only by key
  presence (``null``/``NaN``/``Infinity``/strings/booleans are rejected);
* RSI(14) must be a finite number inside ``0 <= rsi_14 <= 100``;
* ``rsi_delta_3`` must be a finite number (no extra bounds without a
  confirmed scanner contract);
* ``entry_confirmation_rsi_rising`` must be a real boolean — no implicit
  string/number coercion;
* mandatory identity/lineage fields must be non-empty and well-formed;
* original feature values are preserved verbatim — never recalculated.

Every rejection carries a deterministic ``reason_code`` (see the
``REASON_*`` constants). ``str(SourceValidationError)`` equals its code.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping


EXPERIMENT_ID = "ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1"
PROTOCOL_VERSION = "1.0.0"

SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
SOURCE_DIRECTION = "LONG"
SOURCE_ROUTE = "PRE_FILTER_SOURCE"

REQUIRED_LINEAGE_KEYS = frozenset({
    "source_identity",
    "source_signal_time",
    "source_signal_candle_open_time",
    "source_scanner",
    "source_direction",
    "source_features",
    "source_pipeline_fingerprint",
})

REQUIRED_FEATURES = frozenset({
    "rsi_14",
    "rsi_delta_3",
    "entry_confirmation_rsi_rising",
})

# Deterministic rejection reason codes.
REASON_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
REASON_NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
REASON_MISSING_SIGNAL_TIME = "MISSING_SIGNAL_TIME"
REASON_MISSING_CREATED_AT = "MISSING_CREATED_AT"
REASON_MISSING_CAPTURED_AT = "MISSING_CAPTURED_AT"
REASON_INVALID_EVENT_KEY = "INVALID_EVENT_KEY"
REASON_MISSING_SYMBOL = "MISSING_SYMBOL"
REASON_INVALID_SOURCE_SCANNER = "INVALID_SOURCE_SCANNER"
REASON_INVALID_SOURCE_DIRECTION = "INVALID_SOURCE_DIRECTION"
REASON_INVALID_CAPTURE_ROUTE = "INVALID_CAPTURE_ROUTE"
REASON_PRE_FREEZE_SIGNAL = "PRE_FREEZE_SIGNAL"
REASON_PRE_FREEZE_SOURCE_ROW = "PRE_FREEZE_SOURCE_ROW"
REASON_PRE_FREEZE_CAPTURE = "PRE_FREEZE_CAPTURE"
REASON_CAPTURE_BEFORE_SIGNAL = "CAPTURE_BEFORE_SIGNAL"
REASON_INVALID_LINEAGE = "INVALID_LINEAGE"
REASON_INCOMPLETE_LINEAGE = "INCOMPLETE_LINEAGE"
REASON_INVALID_FEATURES = "INVALID_FEATURES"
REASON_INCOMPLETE_FEATURES = "INCOMPLETE_FEATURES"
REASON_LINEAGE_SCANNER_MISMATCH = "LINEAGE_SCANNER_MISMATCH"
REASON_LINEAGE_DIRECTION_MISMATCH = "LINEAGE_DIRECTION_MISMATCH"
REASON_INVALID_LINEAGE_SIGNAL_TIME = "INVALID_LINEAGE_SIGNAL_TIME"
REASON_LINEAGE_SIGNAL_TIME_MISMATCH = "LINEAGE_SIGNAL_TIME_MISMATCH"
REASON_INVALID_CANDLE_IDENTITY = "INVALID_CANDLE_IDENTITY"
REASON_LINEAGE_CANDLE_MISMATCH = "LINEAGE_CANDLE_MISMATCH"
REASON_INVALID_ENTRY_TIMEFRAME = "INVALID_ENTRY_TIMEFRAME"
REASON_INVALID_LINEAGE_JSON = "INVALID_LINEAGE_JSON"
REASON_LINEAGE_IDENTITY_EMPTY = "LINEAGE_IDENTITY_EMPTY"
REASON_LINEAGE_IDENTITY_INVALID_TYPE = "LINEAGE_IDENTITY_INVALID_TYPE"
REASON_LINEAGE_FINGERPRINT_EMPTY = "LINEAGE_FINGERPRINT_EMPTY"
REASON_LINEAGE_FINGERPRINT_INVALID_TYPE = "LINEAGE_FINGERPRINT_INVALID_TYPE"
REASON_FEATURE_RSI14_INVALID_TYPE = "FEATURE_RSI14_INVALID_TYPE"
REASON_FEATURE_RSI14_NOT_FINITE = "FEATURE_RSI14_NOT_FINITE"
REASON_FEATURE_RSI14_OUT_OF_RANGE = "FEATURE_RSI14_OUT_OF_RANGE"
REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE = "FEATURE_RSI_DELTA_3_INVALID_TYPE"
REASON_FEATURE_RSI_DELTA_3_NOT_FINITE = "FEATURE_RSI_DELTA_3_NOT_FINITE"
REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE = (
    "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE"
)

#: RSI(14) is bounded by its definition.
RSI_MIN = 0.0
RSI_MAX = 100.0


class SourceValidationError(ValueError):
    """Source event must not enter the eligible research cohort."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class SourceIntegrityError(RuntimeError):
    """A durable source/ledger integrity contract was violated.

    Deterministic: the research contour fails closed on it (BLOCKED).
    """


def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SourceValidationError(REASON_INVALID_TIMESTAMP)

    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError(REASON_NAIVE_TIMESTAMP)

    return value.astimezone(timezone.utc)


def lineage_hash(lineage: Mapping[str, Any]) -> str:
    """Deterministic SHA256 of the original JSON lineage."""

    payload = json.dumps(
        lineage,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    """True only for real, finite numbers — never bool/str/None/NaN/Inf."""

    if isinstance(value, bool):
        return False

    if isinstance(value, int):
        return True

    if isinstance(value, float):
        return math.isfinite(value)

    if isinstance(value, Decimal):
        return value.is_finite()

    return False


def _require_finite_number(
    value: Any,
    *,
    invalid_type_code: str,
    not_finite_code: str,
) -> float:
    """Validate a numeric source measurement without coercing its type."""

    if value is None:
        # A missing measurement is not a finite number.
        raise SourceValidationError(not_finite_code)

    if isinstance(value, bool) or not isinstance(
        value, (int, float, Decimal)
    ):
        # Covers strings, booleans and containers.
        raise SourceValidationError(invalid_type_code)

    if not _is_finite_number(value):
        # NaN and ±Infinity.
        raise SourceValidationError(not_finite_code)

    return float(value)


def _validate_rsi_14(value: Any) -> None:
    number = _require_finite_number(
        value,
        invalid_type_code=REASON_FEATURE_RSI14_INVALID_TYPE,
        not_finite_code=REASON_FEATURE_RSI14_NOT_FINITE,
    )

    if not RSI_MIN <= number <= RSI_MAX:
        raise SourceValidationError(REASON_FEATURE_RSI14_OUT_OF_RANGE)


def _validate_rsi_delta_3(value: Any) -> None:
    # Finite number only. No upper/lower bounds: no confirmed scanner
    # contract constrains rsi_delta_3 further (Issue #296 review).
    _require_finite_number(
        value,
        invalid_type_code=REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE,
        not_finite_code=REASON_FEATURE_RSI_DELTA_3_NOT_FINITE,
    )


def _validate_confirmation_flag(value: Any) -> None:
    # The V3 source stores a real JSON boolean. Strings ("true") and
    # numbers (1/0) are NOT accepted — no implicit coercion.
    if not isinstance(value, bool):
        raise SourceValidationError(
            REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE
        )


def _validate_required_features(features: Mapping[str, Any]) -> None:
    """Type/value validation of required source features.

    Original values are checked only — never recalculated or replaced.
    """

    _validate_rsi_14(features["rsi_14"])
    _validate_rsi_delta_3(features["rsi_delta_3"])
    _validate_confirmation_flag(features["entry_confirmation_rsi_rising"])


def _validate_identity_fields(lineage: Mapping[str, Any]) -> None:
    """Mandatory identity/lineage fields: non-empty and well-formed."""

    identity = lineage.get("source_identity")

    if not isinstance(identity, str):
        raise SourceValidationError(REASON_LINEAGE_IDENTITY_INVALID_TYPE)

    if not identity.strip():
        raise SourceValidationError(REASON_LINEAGE_IDENTITY_EMPTY)

    fingerprint = lineage.get("source_pipeline_fingerprint")

    if not isinstance(fingerprint, str):
        raise SourceValidationError(
            REASON_LINEAGE_FINGERPRINT_INVALID_TYPE
        )

    if not fingerprint.strip():
        raise SourceValidationError(REASON_LINEAGE_FINGERPRINT_EMPTY)


def _lineage_hash_checked(lineage: Mapping[str, Any]) -> str:
    """Hash the lineage; non-JSON-safe payloads are deterministic rejects."""

    try:
        return lineage_hash(lineage)
    except (TypeError, ValueError):
        raise SourceValidationError(REASON_INVALID_LINEAGE_JSON) from None


def _require_timestamp(row: Mapping[str, Any], key: str, code: str) -> datetime:
    raw = row.get(key)

    if raw is None:
        raise SourceValidationError(code)

    return as_utc(raw)


@dataclass(frozen=True)
class ValidatedSourceEvent:
    event_key: str
    symbol: str
    signal_time: datetime
    source_created_at: datetime
    source_captured_at: datetime
    freeze_ts: datetime
    lineage: dict[str, Any]
    lineage_hash: str
    source_setup_id: str
    source_scanner: str
    source_scanner_version: str
    source_direction: str
    entry_timeframe: str
    signal_candle_open_time: int
    source_reference_price: Any


def validate_source_event(
    row: Mapping[str, Any],
    *,
    freeze_ts: datetime,
) -> ValidatedSourceEvent:

    freeze = as_utc(freeze_ts)

    key = str(row.get("event_key") or "").strip()
    symbol = str(row.get("symbol") or "").strip()

    if len(key) != 64:
        raise SourceValidationError(REASON_INVALID_EVENT_KEY)

    try:
        int(key, 16)
    except ValueError:
        raise SourceValidationError(REASON_INVALID_EVENT_KEY) from None

    if not symbol:
        raise SourceValidationError(REASON_MISSING_SYMBOL)

    if row.get("source_scanner") != SOURCE_SCANNER:
        raise SourceValidationError(REASON_INVALID_SOURCE_SCANNER)

    if row.get("source_direction") != SOURCE_DIRECTION:
        raise SourceValidationError(REASON_INVALID_SOURCE_DIRECTION)

    if row.get("capture_route") != SOURCE_ROUTE:
        raise SourceValidationError(REASON_INVALID_CAPTURE_ROUTE)

    signal_time = _require_timestamp(
        row, "signal_time", REASON_MISSING_SIGNAL_TIME
    )
    source_created = _require_timestamp(
        row, "created_at", REASON_MISSING_CREATED_AT
    )
    source_captured = _require_timestamp(
        row, "captured_at", REASON_MISSING_CAPTURED_AT
    )

    # Strict prospective boundary.
    if signal_time <= freeze:
        raise SourceValidationError(REASON_PRE_FREEZE_SIGNAL)

    if source_created < freeze:
        raise SourceValidationError(REASON_PRE_FREEZE_SOURCE_ROW)

    if source_captured < freeze:
        raise SourceValidationError(REASON_PRE_FREEZE_CAPTURE)

    # A source event cannot be captured before it exists.
    if source_captured < signal_time:
        raise SourceValidationError(REASON_CAPTURE_BEFORE_SIGNAL)

    lineage = row.get("lineage")

    if not isinstance(lineage, dict):
        raise SourceValidationError(REASON_INVALID_LINEAGE)

    if not REQUIRED_LINEAGE_KEYS.issubset(lineage):
        raise SourceValidationError(REASON_INCOMPLETE_LINEAGE)

    features = lineage.get("source_features")

    if not isinstance(features, dict):
        raise SourceValidationError(REASON_INVALID_FEATURES)

    if not REQUIRED_FEATURES.issubset(features):
        raise SourceValidationError(REASON_INCOMPLETE_FEATURES)

    # Value-level validation. Original values stay untouched in the
    # lineage snapshot below.
    _validate_required_features(features)
    _validate_identity_fields(lineage)

    if lineage.get("source_scanner") != SOURCE_SCANNER:
        raise SourceValidationError(REASON_LINEAGE_SCANNER_MISMATCH)

    if lineage.get("source_direction") != SOURCE_DIRECTION:
        raise SourceValidationError(REASON_LINEAGE_DIRECTION_MISMATCH)

    try:
        source_signal_time = as_utc(
            datetime.fromisoformat(lineage["source_signal_time"])
        )
    except (TypeError, ValueError):
        raise SourceValidationError(
            REASON_INVALID_LINEAGE_SIGNAL_TIME
        ) from None

    if source_signal_time != signal_time:
        raise SourceValidationError(REASON_LINEAGE_SIGNAL_TIME_MISMATCH)

    entry_timeframe = row.get("entry_timeframe")

    if not isinstance(entry_timeframe, str) or not entry_timeframe.strip():
        raise SourceValidationError(REASON_INVALID_ENTRY_TIMEFRAME)

    source_candle = lineage.get("source_signal_candle_open_time")
    row_candle = row.get("signal_candle_open_time")

    if isinstance(source_candle, bool) or isinstance(row_candle, bool):
        raise SourceValidationError(REASON_INVALID_CANDLE_IDENTITY)

    try:
        if int(source_candle) != int(row_candle):
            raise SourceValidationError(REASON_LINEAGE_CANDLE_MISMATCH)
    except (TypeError, ValueError):
        raise SourceValidationError(REASON_INVALID_CANDLE_IDENTITY) from None

    if int(row_candle) <= 0:
        raise SourceValidationError(REASON_INVALID_CANDLE_IDENTITY)

    return ValidatedSourceEvent(
        event_key=key,
        symbol=symbol,
        signal_time=signal_time,
        source_created_at=source_created,
        source_captured_at=source_captured,
        freeze_ts=freeze,
        # Verbatim copy of the original lineage: features are stored as
        # captured, without recalculation or substitution.
        lineage=dict(lineage),
        lineage_hash=_lineage_hash_checked(lineage),
        source_setup_id=str(row.get("source_setup_id") or ""),
        source_scanner=str(row["source_scanner"]),
        source_scanner_version=str(
            row.get("source_scanner_version") or ""
        ),
        source_direction=str(row["source_direction"]),
        entry_timeframe=entry_timeframe.strip(),
        signal_candle_open_time=int(row_candle),
        source_reference_price=row.get("source_reference_price"),
    )


def normalize_reason_code(exc: BaseException) -> str:
    """Deterministic reason_code for any validation-path exception."""

    if isinstance(exc, SourceValidationError):
        return exc.reason_code

    if isinstance(exc, KeyError):
        return f"MISSING_FIELD:{exc.args[0]}"

    if isinstance(exc, (TypeError, ValueError)):
        return "INVALID_FIELD_TYPE"

    return "UNEXPECTED_VALIDATION_ERROR"
