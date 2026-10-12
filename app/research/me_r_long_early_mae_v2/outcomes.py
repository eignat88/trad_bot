"""Frozen outcome computation for baseline and early-exit variants."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .candles import ClosedCandle, validate_window
from .protocol import round_trip_cost_r


@dataclass(frozen=True)
class Outcome:
    exit_at: datetime
    exit_price: float
    exit_reason: str
    gross_r: float
    net_r: float
    ambiguous: bool


def first_terminal_exit(
    candles: list[ClosedCandle],
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> tuple[ClosedCandle, str, bool, float] | None:
    """Apply frozen STOP_FIRST ordering within completed candles."""
    for candle in candles:
        stop_hit = candle.low <= stop_price
        target_hit = candle.high >= target_price
        if stop_hit and target_hit:
            return candle, "AMBIGUOUS_STOP_FIRST", True, stop_price
        if stop_hit:
            return candle, "STOP_LOSS", False, stop_price
        if target_hit:
            return candle, "TAKE_PROFIT", False, target_price
    return None


def outcome_from_exit(
    entry_price: float,
    stop_price: float,
    exit_at: datetime,
    exit_price: float,
    exit_reason: str,
    ambiguous: bool = False,
) -> Outcome:
    risk = entry_price - stop_price
    if risk <= 0:
        return Outcome(exit_at, exit_price, exit_reason, 0.0, 0.0, ambiguous)
    gross = (exit_price - entry_price) / risk
    net = gross - round_trip_cost_r(entry_price, exit_price, risk)
    return Outcome(
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_r=round(gross, 8),
        net_r=round(net, 8),
        ambiguous=ambiguous,
    )


def baseline_outcome(
    candles: list[ClosedCandle],
    entry_at: datetime,
    deadline: datetime,
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> Outcome | None:
    window, error = validate_window(candles, entry_at, deadline)
    if error:
        return None
    terminal = first_terminal_exit(window, entry_price, stop_price, target_price)
    if terminal is None:
        return outcome_from_exit(
            entry_price, stop_price, deadline, window[-1].close, "FIXED_HORIZON",
        )
    candle, reason, ambiguous, exit_price = terminal
    return outcome_from_exit(
        entry_price, stop_price, candle.close_at, exit_price, reason, ambiguous,
    )
