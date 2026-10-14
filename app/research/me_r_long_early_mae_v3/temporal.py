"""Closed-candle temporal and source-delay validation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .protocol import (
    MAX_SOURCE_DELAY_SECONDS,
    parse_utc,
    source_available_at,
)


@dataclass(frozen=True)
class SourceTemporal:
    source_candle_close_at: datetime
    source_available_at: datetime
    detected_at: datetime
    delay_seconds: int


def validate_detection_temporal(
    source_candle_close_at: datetime,
    detected_at: datetime,
    fetch_completed_at: datetime,
) -> SourceTemporal:
    close_at = parse_utc(source_candle_close_at)
    detected = parse_utc(detected_at)
    fetch_completed = parse_utc(fetch_completed_at)
    if close_at > detected:
        raise ValueError("SOURCE_CANDLE_NOT_CLOSED")
    if fetch_completed > detected:
        raise ValueError("DETECTION_BEFORE_FETCH_COMPLETION")
    delay = int((detected - close_at).total_seconds())
    if delay > MAX_SOURCE_DELAY_SECONDS:
        raise ValueError("SOURCE_DELAY_EXCEEDS_300_SECONDS")
    # Actual availability is proven by completed source fetch/detection time;
    # the 300-second bound is a rejection ceiling, not an artificial eligibility delay.
    available = min(detected, source_available_at(close_at))
    return SourceTemporal(close_at, available, detected, delay)


def verify_source_timeframes(context, detected_at: datetime) -> tuple[bool, str]:
    detected = parse_utc(detected_at)
    for name, minutes in (
        ("candles_5m", 5),
        ("candles_15m", 15),
        ("candles_1h", 60),
    ):
        candles = getattr(context, name, None)
        if not candles:
            return False, f"{name.upper()}_MISSING"
        latest_close = max(c.close_at for c in candles)
        if latest_close > detected:
            return False, f"{name.upper()}_NOT_CLOSED"
    return True, "ALL_SOURCE_TIMEFRAMES_CLOSED"
