"""Baseline virtual execution with STOP_FIRST and dual cost outputs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .candles import ClosedCandle
from .protocol import (
    ELEVATED_SLIPPAGE_PER_SIDE,
    NORMAL_SLIPPAGE_PER_SIDE,
    round_trip_cost_r,
)


@dataclass(frozen=True)
class BaselineOutcome:
    status: str
    exit_at: datetime | None
    exit_price: float | None
    exit_reason: str | None
    gross_r: float | None
    net_r_normal: float | None
    net_r_elevated: float | None
    ambiguous: bool
    coverage_reason: str | None


def _cost(
    entry_price: float,
    exit_price: float,
    risk: float,
    slippage: float,
) -> float:
    return round_trip_cost_r(entry_price, exit_price, risk, slippage)


def outcome(
    entry_price: float,
    stop_price: float,
    exit_at: datetime,
    exit_price: float,
    exit_reason: str,
    ambiguous: bool = False,
) -> BaselineOutcome:
    risk = entry_price - stop_price
    gross = (exit_price - entry_price) / risk
    return BaselineOutcome(
        status="EVALUATED",
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_r=round(gross, 8),
        net_r_normal=round(gross - _cost(entry_price, exit_price, risk, NORMAL_SLIPPAGE_PER_SIDE), 8),
        net_r_elevated=round(gross - _cost(entry_price, exit_price, risk, ELEVATED_SLIPPAGE_PER_SIDE), 8),
        ambiguous=ambiguous,
        coverage_reason=None,
    )


def first_terminal(
    candles: list[ClosedCandle],
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> tuple[ClosedCandle, str, bool, float] | None:
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


def evaluate_baseline(
    candles: list[ClosedCandle],
    entry_at: datetime,
    deadline: datetime,
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> BaselineOutcome:
    step = timedelta(minutes=5)
    duration = (deadline - entry_at).total_seconds()
    if duration <= 0 or duration % 300 != 0:
        return _coverage_error("INVALID_BASELINE_WINDOW")

    elapsed = int(duration // 300)
    window = []
    seen = set()

    for candle in candles:
        if candle.close_at <= entry_at or candle.open_at >= deadline:
            continue
        if candle.open_at in seen:
            return _coverage_error("DUPLICATE_CANDLE_TIMESTAMP")
        seen.add(candle.open_at)
        window.append(candle)

    window.sort(key=lambda c: c.open_at)
    expected_open = entry_at

    for candle in window:
        if candle.open_at != expected_open:
            return _coverage_error("INCOMPLETE_BASELINE_COVERAGE")
        if candle.close_at != expected_open + step:
            return _coverage_error("INCOMPLETE_BASELINE_COVERAGE")

        terminal = first_terminal(
            [candle], entry_price, stop_price, target_price
        )
        if terminal is not None:
            hit_candle, reason, ambiguous, exit_price = terminal
            return outcome(
                entry_price, stop_price, hit_candle.close_at,
                exit_price, reason, ambiguous,
            )

        expected_open += step

    if len(window) != elapsed:
        return _coverage_error("INCOMPLETE_BASELINE_COVERAGE")

    return outcome(
        entry_price, stop_price, deadline, window[-1].close,
        "FIXED_HORIZON_TIMEOUT",
    )


def _coverage_error(reason: str) -> BaselineOutcome:
    return BaselineOutcome(
        status="INCOMPLETE_COVERAGE", exit_at=None, exit_price=None,
        exit_reason=None, gross_r=None, net_r_normal=None,
        net_r_elevated=None, ambiguous=False, coverage_reason=reason,
    )
