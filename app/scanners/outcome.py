from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models import Candle
from app.scanners.models import SetupCandidate
from app.scanners.risk_geometry import validate_risk_geometry

OutcomeEvent = Literal["NO_ENTRY", "TP1", "TP2", "SL", "EXPIRED", "EXPIRED_BE", "OPEN"]


@dataclass(frozen=True)
class SignalOutcome:
    setup_id: str
    symbol: str
    scanner_name: str
    direction: str
    entry_touched: bool
    first_event: OutcomeEvent
    result_r: float
    mfe_r: float
    mae_r: float
    bars_to_entry: int | None
    bars_to_exit: int | None
    entry_price: float | None
    exit_price: float | None
    fee_slippage_adjusted_result_r: float


def _entry_price(candidate: SetupCandidate) -> float:
    # Conservative fill: LONG enters at the top of the zone, SHORT at the bottom.
    return (
        candidate.entry_zone_high
        if candidate.direction == "LONG"
        else candidate.entry_zone_low
    )


def _price_to_r(candidate: SetupCandidate, price: float, entry: float, risk: float) -> float:
    if candidate.direction == "LONG":
        return (price - entry) / risk
    return (entry - price) / risk


def evaluate_setup_outcome(
    candidate: SetupCandidate,
    candles: list[Candle] | tuple[Candle, ...],
    *,
    max_bars: int = 48,
    fee_slippage_r: float = 0.0,
) -> SignalOutcome:
    """Evaluate a setup on candles after the signal candle without look-ahead.

    If SL and TP are touched in the same candle after entry, SL wins. This is a
    deliberately conservative intrabar assumption for backtests/forward stats.
    """
    valid, reason = validate_risk_geometry(candidate)
    if not valid:
        raise ValueError(f"cannot evaluate invalid setup risk geometry: {reason}")
    if max_bars <= 0:
        raise ValueError("max_bars must be positive")

    future = [c for c in candles if c.timestamp > candidate.signal_candle_open_time]
    future = future[:max_bars]
    entry = _entry_price(candidate)
    risk = abs(entry - candidate.invalidation_price)
    if risk <= 0:
        raise ValueError("risk must be positive")

    entry_index: int | None = None
    exit_index: int | None = None
    event: OutcomeEvent = "NO_ENTRY"
    exit_price: float | None = None
    mfe_r = 0.0
    mae_r = 0.0

    for index, candle in enumerate(future, start=1):
        if entry_index is None:
            touched = candle.low <= candidate.entry_zone_high and candle.high >= candidate.entry_zone_low
            if not touched:
                continue
            entry_index = index
            event = "OPEN"

        if candidate.direction == "LONG":
            mfe_r = max(mfe_r, (candle.high - entry) / risk)
            mae_r = min(mae_r, (candle.low - entry) / risk)
            if candle.low <= candidate.invalidation_price:
                event = "SL"
                exit_price = candidate.invalidation_price
                exit_index = index
                break
            if candidate.target_2 is not None and candle.high >= candidate.target_2:
                event = "TP2"
                exit_price = candidate.target_2
                exit_index = index
                break
            if candle.high >= candidate.target_1:
                event = "TP1"
                exit_price = candidate.target_1
                exit_index = index
                break
        else:
            mfe_r = max(mfe_r, (entry - candle.low) / risk)
            mae_r = min(mae_r, (entry - candle.high) / risk)
            if candle.high >= candidate.invalidation_price:
                event = "SL"
                exit_price = candidate.invalidation_price
                exit_index = index
                break
            if candidate.target_2 is not None and candle.low <= candidate.target_2:
                event = "TP2"
                exit_price = candidate.target_2
                exit_index = index
                break
            if candle.low <= candidate.target_1:
                event = "TP1"
                exit_price = candidate.target_1
                exit_index = index
                break

    if entry_index is None:
        result_r = 0.0
        event = "NO_ENTRY"
    elif exit_price is None:
        # Check if expire_at_breakeven is enabled via features
        expire_at_breakeven = (candidate.features or {}).get("recommended_expiry_policy") == "BREAKEVEN"
        if expire_at_breakeven:
            result_r = 0.0
            event = "EXPIRED_BE"
            exit_price = entry
        else:
            last_close = future[-1].close if future else entry
            result_r = _price_to_r(candidate, last_close, entry, risk)
            event = "EXPIRED"
            exit_price = last_close
        exit_index = len(future) if future else entry_index
    else:
        result_r = _price_to_r(candidate, exit_price, entry, risk)

    adjusted = result_r - fee_slippage_r if entry_index is not None else 0.0
    return SignalOutcome(
        setup_id=str(candidate.setup_id),
        symbol=candidate.symbol,
        scanner_name=candidate.scanner_name,
        direction=candidate.direction,
        entry_touched=entry_index is not None,
        first_event=event,
        result_r=round(result_r, 6),
        mfe_r=round(mfe_r, 6),
        mae_r=round(mae_r, 6),
        bars_to_entry=entry_index,
        bars_to_exit=exit_index,
        entry_price=entry if entry_index is not None else None,
        exit_price=exit_price,
        fee_slippage_adjusted_result_r=round(adjusted, 6),
    )
