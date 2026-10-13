"""Historical 5m recovery for V3 using the existing SRR source."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    BybitHistoricalCandleSource,
    SOURCE_VALIDATED,
)

from .protocol import MAX_HOLD_MINUTES


def fetch_v3_execution_candles(
    source: Any,
    symbol: str,
    entry_at: datetime,
    asof: datetime,
):
    """Return raw candles and coverage status for the V3 execution window."""
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (entry_at, asof)
    ):
        return [], "NAIVE_TIMESTAMP"

    entry_at = entry_at.astimezone(timezone.utc)
    asof = asof.astimezone(timezone.utc)
    deadline = entry_at + timedelta(minutes=MAX_HOLD_MINUTES)

    if asof < deadline:
        return [], "IMMATURE_WINDOW"

    start_ms = int(entry_at.timestamp() * 1000)
    last_open_ms = int(
        (deadline - timedelta(minutes=5)).timestamp() * 1000
    )
    asof_ms = int(asof.timestamp() * 1000)

    result = source.fetch_5m_candles(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=last_open_ms,
        evaluation_asof_ms=asof_ms,
    )

    if result.status != SOURCE_VALIDATED:
        return [], result.reason_code or result.status

    return list(result.candles), None


def make_v3_historical_source(client: Any):
    return BybitHistoricalCandleSource(client)
