"""Closed-candle temporal and source-delay validation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .protocol import (
    MAX_SOURCE_DELAY_SECONDS,
    parse_utc,
    validate_measured_source_availability,
)


@dataclass(frozen=True)
class SourceTemporal:
    source_candle_close_at: datetime
    source_available_at: datetime
    detected_at: datetime
    source_fetch_completed_at: datetime | None
    delay_seconds: int


def validate_detection_temporal(
    source_candle_close_at: datetime,
    detected_at: datetime,
    fetch_completed_at: datetime | None,
) -> SourceTemporal:
    close_at = parse_utc(source_candle_close_at)
    detected = parse_utc(detected_at)
    completed = None if fetch_completed_at is None else parse_utc(fetch_completed_at)

    if completed is None:
        raise ValueError("SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED")
    if completed > detected:
        raise ValueError("DETECTION_BEFORE_FETCH_COMPLETION")
    delay = int((detected - close_at).total_seconds())
    if delay < 0:
        raise ValueError("SOURCE_CANDLE_NOT_CLOSED")
    if delay > MAX_SOURCE_DELAY_SECONDS:
        raise ValueError("SOURCE_DELAY_EXCEEDS_300_SECONDS")
    if completed is None:
        raise ValueError("SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED")
    available = completed
    return SourceTemporal(close_at, available, detected, completed, delay)


def require_fetch_completion_provenance(
    source_fetch_completed_at: datetime | None,
) -> datetime:
    if source_fetch_completed_at is None:
        raise ValueError("SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED")
    return parse_utc(source_fetch_completed_at)


def verify_source_timeframes(context, detected_at: datetime) -> tuple[bool, str]:
    detected = parse_utc(detected_at)
    for name in ("candles_5m", "candles_15m", "candles_1h"):
        candles = getattr(context, name, None)
        if not candles:
            return False, f"{name.upper()}_MISSING"
        latest_close = max(c.close_at for c in candles)
        if latest_close > detected:
            return False, f"{name.upper()}_NOT_CLOSED"
    return True, "ALL_SOURCE_TIMEFRAMES_CLOSED"
