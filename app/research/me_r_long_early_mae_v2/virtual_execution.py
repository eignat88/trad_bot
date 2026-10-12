"""Frozen virtual-fill rules for the ME_R_LONG early MAE V2 observer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .candles import ClosedCandle, first_touch
from .entry_coverage import check_entry_coverage
from .protocol import ENTRY_EXPIRY_MINUTES


@dataclass(frozen=True)
class VirtualFill:
    entry_available_at: datetime
    entry_at: datetime
    entry_price: float
    stop_price: float
    target_price: float
    risk_distance: float
    expiry_at: datetime


def evaluate_virtual_fill(
    source: dict[str, object],
    candles: list[ClosedCandle],
    asof: datetime,
) -> VirtualFill | None:
    """Create a deterministic virtual fill from completed candles only."""
    detected_at = source["detected_at"]
    available_at = source.get("source_available_at")
    if available_at is None:
        return None

    expiry_at = detected_at + timedelta(minutes=ENTRY_EXPIRY_MINUTES)
    if available_at >= expiry_at or asof < expiry_at:
        return None

    eligible_candles = [
        candle for candle in candles
        if candle.open_at >= available_at
        and candle.close_at <= asof
        and candle.close_at <= expiry_at
        and candle.close_at > candle.open_at
    ]

    touched = first_touch(
        eligible_candles,
        float(source["entry_zone_low"]),
        float(source["entry_zone_high"]),
        available_at,
    )
    if touched is None or touched.close_at > expiry_at:
        return None

    complete, _ = check_entry_coverage(
        eligible_candles,
        available_at,
        touched.close_at,
        asof,
    )
    if not complete:
        return None

    entry_price = float(source["entry_zone_high"])
    stop_price = float(source["invalidation_price"])
    risk = entry_price - stop_price
    if risk <= 0:
        return None

    return VirtualFill(
        entry_available_at=available_at,
        entry_at=touched.close_at,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=float(source["target_price"]),
        risk_distance=risk,
        expiry_at=expiry_at,
    )
