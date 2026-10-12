"""Strict 5m candle coverage for prospective virtual entry."""

from __future__ import annotations

from datetime import datetime, timedelta

from .candles import ClosedCandle


def check_entry_coverage(
    candles: list[ClosedCandle],
    detected_at: datetime,
    expiry_at: datetime,
    asof: datetime,
) -> tuple[bool, str | None]:
    """Validate the full observable 5m entry window.

    Starts with the first 5m candle whose open is >= detected_at.
    A missing or duplicated candle never proves ENTRY_NOT_FILLED.
    """
    if asof < expiry_at:
        return False, "ENTRY_WINDOW_NOT_MATURE"

    step = timedelta(minutes=5)

    # First complete 5m candle beginning no earlier than detection.
    seconds = detected_at.timestamp()
    step_seconds = 300
    aligned_seconds = (
        (int(seconds) // step_seconds) * step_seconds
    )
    start = datetime.fromtimestamp(
        aligned_seconds, tz=detected_at.tzinfo
    )
    if start < detected_at:
        start += step

    # Last full candle must close no later than expiry.
    expected_opens = []
    current = start
    while current + step <= expiry_at:
        expected_opens.append(current)
        current += step

    if not expected_opens:
        return False, "NO_COMPLETE_ENTRY_CANDLES"

    observed = {}
    for candle in candles:
        if candle.open_at < start or candle.open_at > expected_opens[-1]:
            continue
        if candle.close_at > asof:
            continue
        if candle.open_at in observed:
            return False, "DUPLICATE_ENTRY_CANDLE"
        if candle.close_at - candle.open_at != step:
            return False, "INVALID_ENTRY_CANDLE_DURATION"
        observed[candle.open_at] = candle

    if any(open_at not in observed for open_at in expected_opens):
        return False, "INCOMPLETE_ENTRY_COVERAGE"

    return True, None
