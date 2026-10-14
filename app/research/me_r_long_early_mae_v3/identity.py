"""Stable research source identity from experiment and actual signal candle."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from .protocol import EXPERIMENT_ID, parse_utc


def source_identity(
    symbol: str,
    source_identity: str,
    signal_candle_open_at: datetime,
) -> str:
    if not symbol or not source_identity:
        raise ValueError("INVALID_SOURCE_IDENTITY_INPUT")
    opened = parse_utc(signal_candle_open_at)
    open_ms = int(opened.timestamp() * 1000)
    if open_ms % 300_000:
        raise ValueError("UNALIGNED_SIGNAL_CANDLE_OPEN")
    payload = "|".join((
        EXPERIMENT_ID,
        source_identity,
        symbol,
        str(open_ms),
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return str(uuid.UUID(int=int.from_bytes(digest[:16], "big"), version=4))
