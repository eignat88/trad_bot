"""Causally feasible NEXT_OPEN_5M virtual entry policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .candles import ClosedCandle, validate_window
from .protocol import ENTRY_EXPIRY_MINUTES, execution_geometry, parse_utc

STEP_SECONDS = 300


@dataclass(frozen=True)
class VirtualEntry:
    entry_at: datetime
    entry_price: float
    stop_price: float
    target_price: float
    risk_distance: float
    expiry_at: datetime


def next_boundary(eligible_at: datetime) -> datetime:
    value = parse_utc(eligible_at)
    seconds = value.timestamp()
    boundary = (int(seconds // STEP_SECONDS) + 1) * STEP_SECONDS
    return datetime.fromtimestamp(boundary, timezone.utc)


def evaluate_next_open(
    candles: list[ClosedCandle],
    entry_eligible_at: datetime,
    expiry_at: datetime,
    asof: datetime,
) -> VirtualEntry | None:
    eligible = parse_utc(entry_eligible_at)
    expiry = parse_utc(expiry_at)
    observed = parse_utc(asof)
    if observed <= eligible or eligible >= expiry:
        return None
    entry_at = next_boundary(eligible)
    if entry_at >= expiry:
        return None
    window_end = min(expiry, entry_at + timedelta(minutes=5))
    window, error = validate_window(candles, entry_at, window_end)
    if error or not window:
        return None
    entry_price, stop_price, target_price = execution_geometry(window[0].open)
    return VirtualEntry(
        entry_at=entry_at,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        risk_distance=entry_price - stop_price,
        expiry_at=expiry,
    )
