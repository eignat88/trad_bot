"""Closed-candle data model and integrity checks for the V2 evaluator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class ClosedCandle:
    open_at: datetime
    close_at: datetime
    open: float
    high: float
    low: float
    close: float


def parse_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def ensure_closed_candle(candle: ClosedCandle, asof: datetime) -> bool:
    """Return True only when a candle is closed and known by the ASOF time."""
    if candle.close_at > candle.open_at and candle.close_at <= asof:
        return True
    return False


def validate_window(
    candles: list[ClosedCandle],
    start: datetime,
    end: datetime,
    expected_minutes: int | None = None,
) -> tuple[list[ClosedCandle], str | None]:
    """Validate a contiguous completed-candle window.

    The window is strictly bounded by ``start`` and ``end``. A candle is
    accepted only when its close is no later than ``end`` and its open is
    no earlier than ``start``. Gaps and duplicates are never repaired.
    """
    if end < start:
        return [], "INVALID_WINDOW"

    selected: list[ClosedCandle] = []
    seen: set[datetime] = set()
    for candle in candles:
        if candle.close_at <= start or candle.open_at >= end:
            continue
        if candle.close_at - candle.open_at != timedelta(minutes=5):
            return [], "INVALID_CANDLE_DURATION"
        if candle.close_at > end or candle.open_at < start:
            return [], "WINDOW_BOUNDARY_CROSSES_ENTRY"
        key = candle.open_at
        if key in seen:
            return [], "DUPLICATE_CANDLE_TIMESTAMP"
        seen.add(key)
        selected.append(candle)

    selected.sort(key=lambda c: c.open_at)
    if not selected:
        return [], "NO_CLOSED_CANDLES"

    if selected[0].open_at != start:
        return [], "INCOMPLETE_WINDOW_START"

    if selected[-1].close_at != end:
        return [], "INCOMPLETE_WINDOW_END"

    for previous, current in zip(selected, selected[1:]):
        if previous.close_at != current.open_at:
            return [], "MISSING_CANDLE_GAP"

    if expected_minutes is not None:
        elapsed = (selected[-1].close_at - selected[0].open_at).total_seconds() / 60
        if abs(elapsed - expected_minutes) > 1e-9:
            return [], "INCOMPLETE_HORIZON"

    return selected, None


def first_touch(
    candles: list[ClosedCandle],
    entry_zone_low: float,
    entry_zone_high: float,
    entry_available_at: datetime,
) -> ClosedCandle | None:
    """Find the first completed candle that touches the frozen entry zone."""
    for candle in sorted(candles, key=lambda c: c.open_at):
        if candle.close_at <= entry_available_at:
            continue
        if candle.open_at < entry_available_at:
            continue
        if candle.low <= entry_zone_high and candle.high >= entry_zone_low:
            return candle
    return None
