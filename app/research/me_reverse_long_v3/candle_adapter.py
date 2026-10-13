"""Strict closed 5m candle adapter for V3 research-only outcomes."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v2.candles import (
    ClosedCandle,
    validate_window,
)
from .protocol import MAX_HOLD_MINUTES

STEP = timedelta(minutes=5)
STEP_MS = 300_000


def build_v3_window(
    source_candles,
    entry_at: datetime,
    asof: datetime,
) -> tuple[list[ClosedCandle], str | None]:
    if entry_at.tzinfo is None or asof.tzinfo is None:
        return [], "NAIVE_TIMESTAMP"

    entry_at = entry_at.astimezone(timezone.utc)
    asof = asof.astimezone(timezone.utc)

    entry_ms = int(entry_at.timestamp() * 1000)
    if entry_at.microsecond % 1000 or entry_ms % STEP_MS:
        return [], "UNALIGNED_ENTRY_TIME"

    deadline = entry_at + timedelta(minutes=MAX_HOLD_MINUTES)
    if asof < deadline:
        return [], "IMMATURE_WINDOW"

    converted = []
    for candle in source_candles:
        opened = datetime.fromtimestamp(
            candle.timestamp / 1000, tz=timezone.utc
        )
        closed = opened + STEP

        if closed <= entry_at or opened >= deadline:
            continue

        values = (
            candle.open, candle.high, candle.low, candle.close
        )
        if not all(math.isfinite(float(v)) for v in values):
            return [], "NONFINITE_CANDLE"

        if (
            candle.low <= 0
            or candle.high < candle.low
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
        ):
            return [], "INVALID_OHLC"

        if closed > asof:
            return [], "UNCLOSED_CANDLE"

        converted.append(ClosedCandle(
            open_at=opened,
            close_at=closed,
            open=float(candle.open),
            high=float(candle.high),
            low=float(candle.low),
            close=float(candle.close),
        ))

    return validate_window(
        converted,
        entry_at,
        deadline,
        expected_minutes=MAX_HOLD_MINUTES,
    )
