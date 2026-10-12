"""Closed-candle ASOF context for ME Close Location OOS only."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from math import isfinite

from app.scanners.context_builder import (
    _build_indicators,
    _classify_market_regime,
)

TF_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}


def _timestamp_ms(candle) -> int:
    ts = candle.timestamp

    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ValueError("Naive candle timestamp")
        return int(ts.timestamp() * 1000)

    if isinstance(ts, int) and not isinstance(ts, bool):
        return ts

    raise ValueError("Unsupported candle timestamp")


def _closed_candles(candles, asof_ms: int, timeframe: str):
    interval_ms = TF_MS[timeframe]
    result = []

    previous_ts = None

    for candle in candles:
        ts = _timestamp_ms(candle)

        if ts % interval_ms != 0:
            raise ValueError(
                f"{timeframe}: unaligned candle timestamp {ts}"
            )

        if previous_ts is not None:
            if ts <= previous_ts:
                raise ValueError(
                    f"{timeframe}: unordered or duplicate candles"
                )

            if ts - previous_ts != interval_ms:
                raise ValueError(
                    f"{timeframe}: missing candle between "
                    f"{previous_ts} and {ts}"
                )

        previous_ts = ts

        if ts + interval_ms <= asof_ms:
            result.append(candle)

    return tuple(result)


def build_me_clean_asof_context(ctx):
    """Return isolated closed-candle context or None."""

    decision_time = ctx.evaluated_at

    if not isinstance(decision_time, datetime):
        raise ValueError("Invalid evaluation timestamp")

    if decision_time.tzinfo is None:
        raise ValueError("Naive evaluation timestamp")

    asof_ms = int(decision_time.timestamp() * 1000)

    closed_5m = _closed_candles(
        ctx.candles_5m, asof_ms, "5m"
    )

    closed_15m = _closed_candles(
        ctx.candles_15m, asof_ms, "15m"
    )

    closed_1h = _closed_candles(
        ctx.candles_1h, asof_ms, "1h"
    )

    # Frozen V1 requires 15 x 5m and 30 x 15m.
    # Require at least 20 closed 1h bars to avoid EMA20
    # fallback based on insufficient history.
    if (
        len(closed_5m) < 15
        or len(closed_15m) < 30
        or len(closed_1h) < 20
    ):
        return None

    indicators = _build_indicators(list(closed_1h))

    if not (
        isfinite(float(indicators.rsi))
        and isfinite(float(indicators.atr))
    ):
        raise ValueError("Non-finite closed-1h indicators")

    regime = _classify_market_regime(
        indicators, float(closed_1h[-1].close)
    )

    return replace(
        ctx,
        candles_5m=closed_5m,
        candles_15m=closed_15m,
        candles_1h=closed_1h,
        indicators=indicators,
        market_regime=regime,
    )
