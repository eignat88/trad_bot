"""Issue #296: research-only prospective source event contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
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


class SourceValidationError(ValueError):
    """Source event must not enter the eligible research cohort."""


def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SourceValidationError("INVALID_TIMESTAMP")

    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError("NAIVE_TIMESTAMP")

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
        raise SourceValidationError("INVALID_EVENT_KEY")

    try:
        int(key, 16)
    except ValueError:
        raise SourceValidationError("INVALID_EVENT_KEY") from None

    if not symbol:
        raise SourceValidationError("MISSING_SYMBOL")

    if row.get("source_scanner") != SOURCE_SCANNER:
        raise SourceValidationError("INVALID_SOURCE_SCANNER")

    if row.get("source_direction") != SOURCE_DIRECTION:
        raise SourceValidationError("INVALID_SOURCE_DIRECTION")

    if row.get("capture_route") != SOURCE_ROUTE:
        raise SourceValidationError("INVALID_CAPTURE_ROUTE")

    signal_time = as_utc(row["signal_time"])
    source_created = as_utc(row["created_at"])
    source_captured = as_utc(row["captured_at"])

    # Strict prospective boundary.
    if signal_time <= freeze:
        raise SourceValidationError("PRE_FREEZE_SIGNAL")

    if source_created < freeze:
        raise SourceValidationError("PRE_FREEZE_SOURCE_ROW")

    if source_captured < freeze:
        raise SourceValidationError("PRE_FREEZE_CAPTURE")

    # A source event cannot be captured before it exists.
    if source_captured < signal_time:
        raise SourceValidationError("CAPTURE_BEFORE_SIGNAL")

    lineage = row.get("lineage")

    if not isinstance(lineage, dict):
        raise SourceValidationError("INVALID_LINEAGE")

    if not REQUIRED_LINEAGE_KEYS.issubset(lineage):
        raise SourceValidationError("INCOMPLETE_LINEAGE")

    features = lineage.get("source_features")

    if not isinstance(features, dict):
        raise SourceValidationError("INVALID_FEATURES")

    if not REQUIRED_FEATURES.issubset(features):
        raise SourceValidationError("INCOMPLETE_FEATURES")

    if lineage.get("source_scanner") != SOURCE_SCANNER:
        raise SourceValidationError("LINEAGE_SCANNER_MISMATCH")

    if lineage.get("source_direction") != SOURCE_DIRECTION:
        raise SourceValidationError("LINEAGE_DIRECTION_MISMATCH")

    try:
        source_signal_time = as_utc(
            datetime.fromisoformat(lineage["source_signal_time"])
        )
    except (TypeError, ValueError, KeyError):
        raise SourceValidationError("INVALID_LINEAGE_SIGNAL_TIME") from None

    if source_signal_time != signal_time:
        raise SourceValidationError("LINEAGE_SIGNAL_TIME_MISMATCH")

    source_candle = lineage.get("source_signal_candle_open_time")
    row_candle = row.get("signal_candle_open_time")

    try:
        if int(source_candle) != int(row_candle):
            raise SourceValidationError("LINEAGE_CANDLE_MISMATCH")
    except (TypeError, ValueError):
        raise SourceValidationError("INVALID_CANDLE_IDENTITY") from None

    return ValidatedSourceEvent(
        event_key=key,
        symbol=symbol,
        signal_time=signal_time,
        source_created_at=source_created,
        source_captured_at=source_captured,
        freeze_ts=freeze,
        lineage=dict(lineage),
        lineage_hash=lineage_hash(lineage),
        source_setup_id=str(row.get("source_setup_id") or ""),
        source_scanner=str(row["source_scanner"]),
        source_scanner_version=str(
            row.get("source_scanner_version") or ""
        ),
        source_direction=str(row["source_direction"]),
        entry_timeframe=str(row.get("entry_timeframe") or "5m"),
        signal_candle_open_time=int(row["signal_candle_open_time"]),
        source_reference_price=row.get("source_reference_price"),
    )
