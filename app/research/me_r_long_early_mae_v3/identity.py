"""Stable research source identity from instrument and signal candle."""
from __future__ import annotations

import hashlib
import uuid


def source_identity(symbol: str, signal_candle_open_ms: int) -> str:
    if not symbol or signal_candle_open_ms <= 0:
        raise ValueError("INVALID_SOURCE_IDENTITY_INPUT")
    payload = f"ME_R_LONG_EARLY_MAE_EXIT_OOS_V3|{symbol}|{signal_candle_open_ms}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    value = int.from_bytes(digest[:16], "big")
    return str(uuid.UUID(int=value, version=4))
