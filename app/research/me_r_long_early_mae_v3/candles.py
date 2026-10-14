"""Strict completed 5m candle windows for V3 outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .protocol import parse_utc


@dataclass(frozen=True)
class ClosedCandle:
    open_at: datetime
    close_at: datetime
    open: float
    high: float
    low: float
    close: float


def validate_window(
    candles: list[ClosedCandle],
    start: datetime,
    end: datetime,
    expected_minutes: int | None = None,
) -> tuple[list[ClosedCandle], str | None]:
    start = parse_utc(start)
    end = parse_utc(end)
    if end <= start:
        return [], "INVALID_WINDOW"
    step = timedelta(minutes=5)
    observed = {}
    for candle in candles:
        open_at = parse_utc(candle.open_at)
        close_at = parse_utc(candle.close_at)
        if close_at <= start or open_at >= end:
            continue
        if close_at - open_at != step:
            return [], "INVALID_CANDLE_DURATION"
        if close_at > end or open_at < start:
            return [], "WINDOW_BOUNDARY_CROSSES_ENDPOINT"
        if open_at in observed:
            return [], "DUPLICATE_CANDLE_TIMESTAMP"
        observed[open_at] = candle
    if not observed:
        return [], "NO_CLOSED_CANDLES"
    opens = sorted(observed)
    if opens[0] != start:
        return [], "INCOMPLETE_WINDOW_START"
    for previous, current in zip(opens, opens[1:]):
        if current - previous != step:
            return [], "MISSING_CANDLE_GAP"
    selected = [observed[open_at] for open_at in opens]
    if selected[-1].close_at != end:
        return [], "INCOMPLETE_WINDOW_END"
    if expected_minutes is not None:
        elapsed = (end - start).total_seconds() / 60
        if abs(elapsed - expected_minutes) > 1e-9:
            return [], "INCOMPLETE_HORIZON"
    return selected, None
