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


@dataclass(frozen=True)
class EntryDecision:
    state: str
    reason: str | None
    entry: VirtualEntry | None = None


def next_boundary(eligible_at: datetime) -> datetime:
    value = parse_utc(eligible_at)
    boundary = (int(value.timestamp() // STEP_SECONDS) + 1) * STEP_SECONDS
    return datetime.fromtimestamp(boundary, timezone.utc)


def decide_next_open(
    candles: list[ClosedCandle],
    entry_eligible_at: datetime,
    detected_at: datetime,
    activation_at: datetime,
    asof: datetime,
) -> EntryDecision:
    eligible = max(
        parse_utc(entry_eligible_at),
        parse_utc(detected_at),
        parse_utc(activation_at),
    )
    observed = parse_utc(asof)
    expiry = eligible + timedelta(minutes=ENTRY_EXPIRY_MINUTES)

    if observed < eligible:
        return EntryDecision("ENTRY_PENDING", "NOT_YET_ELIGIBLE")
    entry_at = next_boundary(eligible)
    if entry_at >= expiry:
        return EntryDecision("ENTRY_NOT_FILLED", "EXPIRED_BEFORE_NEXT_OPEN")

    window_end = entry_at + timedelta(minutes=5)
    window, error = validate_window(candles, entry_at, window_end)
    if error:
        return EntryDecision("INCOMPLETE_COVERAGE", error)
    if not window:
        return EntryDecision("INCOMPLETE_COVERAGE", "NO_CLOSED_CANDLES")

    candle = window[0]
    if candle.open_at != entry_at:
        return EntryDecision("INCOMPLETE_COVERAGE", "SELECTED_ENTRY_CANDLE_MISSING")
    if candle.close_at > observed:
        return EntryDecision("ENTRY_PENDING", "SELECTED_ENTRY_CANDLE_UNCLOSED")
    if observed > expiry:
        return EntryDecision("ENTRY_NOT_FILLED", "EXPIRED_AFTER_ENTRY_WINDOW")
    entry_price, stop_price, target_price = execution_geometry(candle.open)
    return EntryDecision(
        "ENTRY_FILLED",
        None,
        VirtualEntry(
            entry_at=entry_at,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_distance=entry_price - stop_price,
            expiry_at=expiry,
        ),
    )


def evaluate_next_open(
    candles: list[ClosedCandle],
    entry_eligible_at: datetime,
    expiry_at: datetime,
    asof: datetime,
) -> VirtualEntry | None:
    decision = decide_next_open(
        candles,
        entry_eligible_at,
        entry_eligible_at,
        entry_eligible_at,
        asof,
    )
    return decision.entry


def _has_full_coverage(
    candles: list[ClosedCandle],
    start: datetime,
    asof: datetime,
) -> bool:
    first = next_boundary(start)
    elapsed = int((asof - first).total_seconds() // STEP_SECONDS)
    if elapsed <= 0:
        return False
    end = first + timedelta(minutes=5 * elapsed)
    window, error = validate_window(candles, first, end)
    return error is None and len(window) == elapsed
