"""Bounded, fail-closed Bybit 5m historical source for ME Close Location."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.scanners.models import Candle

CANDLE_MS = 300_000
PAGE_LIMIT = 200


class HistoricalSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalWindow:
    symbol: str
    start_ms: int
    end_ms: int
    candles: tuple[Candle, ...]


def fetch_historical_5m(
    client,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    asof_ms: int | None = None,
) -> HistoricalWindow:
    if not symbol or start_ms > end_ms:
        raise ValueError("Invalid historical window")

    if start_ms % CANDLE_MS or end_ms % CANDLE_MS:
        raise ValueError("Window boundaries must align to 5m opens")

    if asof_ms is None:
        asof_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if asof_ms < 0:
        raise ValueError("Invalid asof_ms")

    # Every requested candle must have fully closed.
    if end_ms + CANDLE_MS > asof_ms:
        raise HistoricalSourceError("WINDOW_NOT_FULLY_CLOSED")

    by_open = {}
    cursor = start_ms

    while cursor <= end_ms:
        page_end = min(
            end_ms,
            cursor + (PAGE_LIMIT - 1) * CANDLE_MS,
        )

        payload = client._public_get(
            "/v5/market/kline",
            category="linear",
            symbol=symbol,
            interval="5",
            start=cursor,
            end=page_end,
            limit=PAGE_LIMIT,
        )

        if payload.get("retCode") != 0:
            raise HistoricalSourceError("BYBIT_API_ERROR")

        raw = payload.get("result", {}).get("list", [])
        if not isinstance(raw, list):
            raise HistoricalSourceError("INVALID_API_RESPONSE")

        for r in raw:
            if len(r) < 6:
                raise HistoricalSourceError("INVALID_CANDLE_ROW")

            try:
                timestamp = int(r[0])
                o, h, l, c = (float(v) for v in r[1:5])
                volume = float(r[5])
            except (TypeError, ValueError, OverflowError):
                raise HistoricalSourceError("INVALID_CANDLE_VALUE")

            if timestamp < cursor or timestamp > page_end:
                raise HistoricalSourceError("CANDLE_OUT_OF_WINDOW")

            if timestamp % CANDLE_MS:
                raise HistoricalSourceError("CANDLE_NOT_ALIGNED")

            if (
                not all(math.isfinite(v) and v > 0 for v in (o, h, l, c))
                or h < max(o, c)
                or l > min(o, c)
                or l > h
                or not math.isfinite(volume)
                or volume < 0
            ):
                raise HistoricalSourceError("INVALID_OHLC")

            if timestamp in by_open:
                raise HistoricalSourceError("DUPLICATE_CANDLE")

            by_open[timestamp] = Candle(
                timestamp=timestamp,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=volume,
            )

        cursor = page_end + CANDLE_MS

    expected = list(range(start_ms, end_ms + CANDLE_MS, CANDLE_MS))

    if any(timestamp not in by_open for timestamp in expected):
        raise HistoricalSourceError("INCOMPLETE_CANDLE_COVERAGE")

    return HistoricalWindow(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        candles=tuple(by_open[t] for t in expected),
    )
