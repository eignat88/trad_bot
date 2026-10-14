"""Fail-closed closed-candle context for research detector V3."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import Candle
from app.scanners.context_builder import (
    _build_indicators,
    _classify_market_regime,
)

INTERVALS_MS = {
    "candles_5m": 300_000,
    "candles_15m": 900_000,
    "candles_1h": 3_600_000,
}


@dataclass(frozen=True)
class ClosedCandle:
    open_at: datetime
    close_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class DetectionContext:
    symbol: str
    candles_5m: tuple[ClosedCandle, ...]
    candles_15m: tuple[ClosedCandle, ...]
    candles_1h: tuple[ClosedCandle, ...]
    indicators: Any
    market_regime: str
    evaluated_at: datetime
    source_fetch_started_at: datetime
    source_fetch_completed_at: datetime | None = None

    @property
    def signal_candle(self) -> ClosedCandle:
        return self.candles_5m[-1]


def parse_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NAIVE_UTC_TIMESTAMP")
    return value.astimezone(timezone.utc)


def _timestamp_ms(candle: Any) -> int:
    raw = candle.timestamp
    if isinstance(raw, datetime):
        if raw.tzinfo is None or raw.utcoffset() is None:
            raise ValueError("NAIVE_CANDLE_TIMESTAMP")
        value = int(raw.timestamp() * 1000)
    else:
        value = int(raw)
        if value < 10_000_000_000:
            value *= 1000
    return value


def _validate_finite(candle: ClosedCandle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("NONFINITE_CANDLE_DATA")
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        raise ValueError("NONPOSITIVE_CANDLE_PRICE")


def _closed_only(candles, interval_ms: int, asof: datetime) -> tuple[ClosedCandle, ...]:
    asof_ms = int(asof.timestamp() * 1000)
    selected = []
    seen_ms = set()
    for candle in candles:
        opened = _timestamp_ms(candle)
        if opened in seen_ms:
            raise ValueError("DUPLICATE_SOURCE_CANDLE")
        seen_ms.add(opened)
        if opened % interval_ms:
            raise ValueError("UNALIGNED_SOURCE_CANDLE")
        if opened + interval_ms > asof_ms:
            raise ValueError("FUTURE_OR_FORMING_SOURCE_CANDLE")
        selected.append(
            ClosedCandle(
                open_at=datetime.fromtimestamp(opened / 1000, timezone.utc),
                close_at=datetime.fromtimestamp(
                    (opened + interval_ms) / 1000, timezone.utc
                ),
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=float(getattr(candle, "volume", 0.0)),
            )
        )

    if not selected:
        raise ValueError("NO_CLOSED_SOURCE_CANDLES")
    if len({c.open_at for c in selected}) != len(selected):
        raise ValueError("DUPLICATE_SOURCE_CANDLE")
    selected.sort(key=lambda c: c.open_at)
    for candle in selected:
        _validate_finite(candle)
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise ValueError("INVALID_CANDLE_GEOMETRY")

    for previous, current in zip(selected, selected[1:]):
        if current.open_at != previous.close_at:
            raise ValueError("GAPPED_SOURCE_CANDLES")

    expected_open_ms = ((asof_ms // interval_ms) - 1) * interval_ms
    expected_open = datetime.fromtimestamp(expected_open_ms / 1000, timezone.utc)
    if selected[-1].open_at != expected_open:
        raise ValueError("STALE_SOURCE_CANDLES")
    return tuple(selected)


def build_closed_context(
    source: Any,
    asof: datetime,
    source_fetch_completed_at: datetime | None = None,
) -> DetectionContext:
    asof = parse_utc(asof)
    fetch_started = parse_utc(source.source_fetch_started_at)
    if fetch_started > asof:
        raise ValueError("SOURCE_FETCH_STARTED_AFTER_ASOF")

    completed = None if source_fetch_completed_at is None else parse_utc(source_fetch_completed_at)
    if completed is not None and completed > asof:
        raise ValueError("SOURCE_FETCH_COMPLETED_AFTER_ASOF")
    if completed is not None and completed < fetch_started:
        raise ValueError("SOURCE_FETCH_COMPLETED_BEFORE_START")

    closed = {
        name: _closed_only(getattr(source, name), interval_ms, asof)
        for name, interval_ms in INTERVALS_MS.items()
    }
    if (
        len(closed["candles_5m"]) < 18
        or len(closed["candles_15m"]) < 30
        or len(closed["candles_1h"]) < 20
    ):
        raise ValueError("INSUFFICIENT_CLOSED_CANDLES")

    indicators = _build_indicators(list(closed["candles_1h"]))
    for field in ("atr", "rsi", "ema20", "ema50", "ema200"):
        value = getattr(indicators, field)
        if not math.isfinite(float(value)):
            raise ValueError("NONFINITE_INDICATOR")
    regime = _classify_market_regime(indicators, closed["candles_1h"][-1].close)
    return DetectionContext(
        symbol=source.symbol,
        candles_5m=closed["candles_5m"],
        candles_15m=closed["candles_15m"],
        candles_1h=closed["candles_1h"],
        indicators=indicators,
        market_regime=regime,
        evaluated_at=asof,
        source_fetch_started_at=fetch_started,
        source_fetch_completed_at=completed,
    )


def to_frozen_v1_candles(candles: tuple[ClosedCandle, ...]) -> list[Candle]:
    return [
        Candle(
            timestamp=int(c.open_at.timestamp() * 1000),
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
        )
        for c in candles
    ]
