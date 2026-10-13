"""Fail-closed V3 temporal provenance for source V2 signals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .entry_policy import next_executable_entry_at

FIVE_MINUTES_MS = 300_000


@dataclass(frozen=True)
class TemporalSnapshot:
    status: str
    trigger_open_at: datetime | None
    trigger_close_at: datetime | None
    execution_entry_at: datetime | None
    reason: str


def verify_trigger_candle(
    candles_5m: Any,
    detected_at: datetime,
) -> TemporalSnapshot:
    if detected_at.tzinfo is None:
        return TemporalSnapshot(
            "TEMPORAL_UNVERIFIED", None, None, None,
            "NAIVE_DETECTION_TIME",
        )

    if not candles_5m:
        return TemporalSnapshot(
            "TEMPORAL_UNVERIFIED", None, None, None,
            "NO_SOURCE_CANDLES",
        )

    # V2 explicitly uses candles_5m[-1].
    candle = candles_5m[-1]

    try:
        open_ms = int(candle.timestamp)
    except (TypeError, ValueError, OverflowError):
        return TemporalSnapshot(
            "TEMPORAL_UNVERIFIED", None, None, None,
            "INVALID_TRIGGER_TIMESTAMP",
        )

    if open_ms % FIVE_MINUTES_MS:
        return TemporalSnapshot(
            "TEMPORAL_UNVERIFIED", None, None, None,
            "UNALIGNED_TRIGGER_CANDLE",
        )

    opened = datetime.fromtimestamp(
        open_ms / 1000, tz=timezone.utc
    )
    closed = opened + timedelta(minutes=5)
    detected = detected_at.astimezone(timezone.utc)

    if closed > detected:
        return TemporalSnapshot(
            "TEMPORAL_UNVERIFIED", opened, closed, None,
            "TRIGGER_CANDLE_NOT_CLOSED",
        )

    return TemporalSnapshot(
        "TEMPORAL_VALIDATED",
        opened,
        closed,
        next_executable_entry_at(detected),
        "SOURCE_CANDLE_CLOSED_BEFORE_DETECTION",
    )


def verify_source_timeframes(
    ctx: Any,
    detected_at: datetime,
) -> tuple[bool, str]:
    """Verify that every source timeframe ends with a closed candle.

    Fail closed: V3 cannot certify a source computed using
    still-forming candles.
    """
    if detected_at.tzinfo is None or detected_at.utcoffset() is None:
        return False, "NAIVE_DETECTION_TIME"

    detected = detected_at.astimezone(timezone.utc)

    for attribute, minutes in (
        ("candles_5m", 5),
        ("candles_15m", 15),
        ("candles_1h", 60),
    ):
        candles = getattr(ctx, attribute, None)
        if not candles:
            return False, f"{attribute.upper()}_MISSING"

        try:
            open_ms = int(candles[-1].timestamp)
            step_ms = minutes * 60_000
            if open_ms % step_ms:
                return False, f"{attribute.upper()}_UNALIGNED"

            close_at = datetime.fromtimestamp(
                open_ms / 1000, tz=timezone.utc
            ) + timedelta(minutes=minutes)
        except (ValueError, TypeError, OverflowError, OSError):
            return False, f"{attribute.upper()}_INVALID_TIMESTAMP"

        if close_at > detected:
            return False, f"{attribute.upper()}_NOT_CLOSED"

    return True, "ALL_SOURCE_TIMEFRAMES_CLOSED"
