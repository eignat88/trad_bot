"""Restart-stable identity for closed-candle V3 observations."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from .protocol import EXPERIMENT_ID, SOURCE_SCANNER


def v3_source_key(
    symbol: str,
    direction: str,
    scanner_version: str,
    trigger_open_at: datetime,
) -> int:
    if not symbol or direction != "LONG" or not scanner_version:
        raise ValueError("INVALID_V3_SOURCE_IDENTITY")

    if trigger_open_at.tzinfo is None or trigger_open_at.utcoffset() is None:
        raise ValueError("NAIVE_TRIGGER_OPEN")

    opened = trigger_open_at.astimezone(timezone.utc)
    opened_ms = int(opened.timestamp() * 1000)

    if opened_ms % 300_000 or opened.microsecond % 1000:
        raise ValueError("UNALIGNED_TRIGGER_OPEN")

    payload = "|".join((
        EXPERIMENT_ID,
        SOURCE_SCANNER,
        scanner_version,
        symbol,
        direction,
        str(opened_ms),
    ))

    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") % (2**31 - 1)
    return -(value + 1)
