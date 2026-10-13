"""Research-only V3 execution outcome calculation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.research.me_r_long_early_mae_v2.candles import (
    ClosedCandle,
    validate_window,
)
from app.research.me_r_long_early_mae_v2.outcomes import first_terminal_exit

from .protocol import (
    MAX_HOLD_MINUTES,
    FEE_PER_SIDE,
    NORMAL_SLIPPAGE_PER_SIDE,
    ELEVATED_SLIPPAGE_PER_SIDE,
    V3Geometry,
)


@dataclass(frozen=True)
class V3ExecutionOutcome:
    exit_at: datetime
    exit_price: float
    exit_reason: str
    gross_r: float
    net_r_normal: float
    net_r_elevated: float
    ambiguous: bool


def evaluate_v3_execution(
    candles: list[ClosedCandle],
    entry_at: datetime,
    geometry: V3Geometry,
) -> tuple[V3ExecutionOutcome | None, str | None]:
    """Evaluate a complete, contiguous 240m post-entry window.

    Entry is the frozen V2 signal-reference price. All 5m candles must
    be closed and available. Both-touch candles resolve STOP_FIRST.
    """
    if not (
        0 < geometry.stop < geometry.entry < geometry.target
    ):
        return None, "INVALID_GEOMETRY"

    deadline = entry_at + timedelta(minutes=MAX_HOLD_MINUTES)
    window, error = validate_window(
        candles, entry_at, deadline,
        expected_minutes=MAX_HOLD_MINUTES,
    )
    if error:
        return None, error

    terminal = first_terminal_exit(
        window, geometry.entry, geometry.stop, geometry.target
    )

    if terminal is None:
        exit_at = deadline
        exit_price = window[-1].close
        exit_reason = "TIMEOUT"
        ambiguous = False
    else:
        candle, reason, ambiguous, exit_price = terminal
        exit_at = candle.close_at
        exit_reason = {
            "STOP_LOSS": "SL_FIRST",
            "TAKE_PROFIT": "TP_FIRST",
        }.get(reason, reason)

    risk = geometry.entry - geometry.stop
    gross = (exit_price - geometry.entry) / risk

    def net(slippage: float) -> float:
        side_cost = FEE_PER_SIDE + slippage
        cost_r = (
            side_cost * (geometry.entry + exit_price) / risk
        )
        return round(gross - cost_r, 8)

    return V3ExecutionOutcome(
        exit_at=exit_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_r=round(gross, 8),
        net_r_normal=net(NORMAL_SLIPPAGE_PER_SIDE),
        net_r_elevated=net(ELEVATED_SLIPPAGE_PER_SIDE),
        ambiguous=ambiguous,
    ), None
