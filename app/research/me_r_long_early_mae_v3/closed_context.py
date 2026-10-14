"""Fail-closed closed-candle context for research detector V3."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

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


def parse_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NAIVE_UTC_TIMESTAMP")
    return value.astimezone(timezone.utc)


def _closed_only(candles, interval_ms: int, asof_ms: int) -> tuple[ClosedCandle, ...]:
    selected = []
    for candle in candles:
        raw_timestamp = candle.timestamp
        opened = (
            int(raw_timestamp)
            if isinstance(raw_timestamp, (int, float))
            else int(raw_timestamp.timestamp() * 1000)
        )
        if isinstance(raw_timestamp, (int, float)) and raw_timestamp < 10_000_000_000:
            opened *= 1000
        if opened % interval_ms:
            raise ValueError("UNALIGNED_SOURCE_CANDLE")
        if opened + interval_ms <= asof_ms:
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
                )
            )
    if len({c.open_at for c in selected}) != len(selected):
        raise ValueError("DUPLICATE_SOURCE_CANDLE")
    selected.sort(key=lambda c: c.open_at)
    for previous, current in zip(selected, selected[1:]):
        if current.open_at != previous.close_at:
            raise ValueError("GAPPED_SOURCE_CANDLES")
    if not selected:
        raise ValueError("NO_CLOSED_SOURCE_CANDLES")
    # A missing latest candle is detected by required count and contiguous
    # history checks above; do not fabricate a candle for an arbitrary ASOF
    # boundary. Staleness is represented by explicit coverage failure in
    # downstream windows rather than silently selecting an older candle.
    return tuple(selected)


def build_closed_context(source: Any, asof: datetime) -> DetectionContext:
    asof = parse_utc(asof)
    fetch_started = source.source_fetch_started_at
    if fetch_started is None or parse_utc(fetch_started) > asof:
        raise ValueError("INVALID_SOURCE_FETCH_TIME")
    asof_ms = int(asof.timestamp() * 1000)
    closed = {
        name: _closed_only(getattr(source, name), interval_ms, asof_ms)
        for name, interval_ms in INTERVALS_MS.items()
    }
    if (
        len(closed["candles_5m"]) < 18
        or len(closed["candles_15m"]) < 30
        or len(closed["candles_1h"]) < 20
    ):
        raise ValueError("INSUFFICIENT_CLOSED_CANDLES")
    indicators = _build_indicators(list(closed["candles_1h"]))
    regime = _classify_market_regime(indicators, closed["candles_1h"][-1].close)
    return DetectionContext(
        symbol=source.symbol,
        candles_5m=closed["candles_5m"],
        candles_15m=closed["candles_15m"],
        candles_1h=closed["candles_1h"],
        indicators=indicators,
        market_regime=regime,
        evaluated_at=asof,
        source_fetch_started_at=parse_utc(fetch_started),
    )
