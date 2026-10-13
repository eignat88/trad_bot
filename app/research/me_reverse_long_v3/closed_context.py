"""Independent closed-candle MarketContext for V3 research only.

Does not mutate the source MarketContext or production V2 scanner.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from app.scanners.context_builder import (
    _build_indicators,
    _classify_market_regime,
)
from app.scanners.models import MarketContext

INTERVALS_MS = {
    "candles_5m": 300_000,
    "candles_15m": 900_000,
    "candles_1h": 3_600_000,
    "candles_4h": 14_400_000,
}


def _closed_only(candles, interval_ms: int, asof_ms: int):
    selected = []

    for candle in candles:
        opened = int(candle.timestamp)

        if opened % interval_ms:
            raise ValueError("UNALIGNED_SOURCE_CANDLE")

        if opened + interval_ms <= asof_ms:
            selected.append(candle)

    if len({c.timestamp for c in selected}) != len(selected):
        raise ValueError("DUPLICATE_SOURCE_CANDLE")

    ordered = tuple(sorted(selected, key=lambda c: c.timestamp))

    if not ordered:
        raise ValueError("NO_CLOSED_SOURCE_CANDLES")

    for previous, current in zip(ordered, ordered[1:]):
        if int(current.timestamp) - int(previous.timestamp) != interval_ms:
            raise ValueError("GAPPED_SOURCE_CANDLES")

    expected_close = (asof_ms // interval_ms) * interval_ms
    actual_close = int(ordered[-1].timestamp) + interval_ms

    if actual_close != expected_close:
        raise ValueError("STALE_SOURCE_CANDLES")

    return ordered


def build_closed_v3_context(
    ctx: MarketContext,
) -> MarketContext:
    if ctx.evaluated_at.tzinfo is None:
        raise ValueError("NAIVE_CONTEXT_TIME")

    asof_ms = int(
        ctx.evaluated_at.astimezone(timezone.utc).timestamp() * 1000
    )

    closed = {
        name: _closed_only(
            getattr(ctx, name),
            interval_ms,
            asof_ms,
        )
        for name, interval_ms in INTERVALS_MS.items()
    }

    if (
        len(closed["candles_5m"]) < 18
        or len(closed["candles_15m"]) < 30
        or len(closed["candles_1h"]) < 20
    ):
        raise ValueError("INSUFFICIENT_CLOSED_CANDLES")

    indicators = _build_indicators(list(closed["candles_1h"]))

    return replace(
        ctx,
        **closed,
        indicators=indicators,
        market_regime=_classify_market_regime(
            indicators,
            closed["candles_1h"][-1].close,
        ),
    )
