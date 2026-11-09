"""Restart-stable identity + lineage for ME_LONG_V2_INVERSE_SHORT_V1.

The ``event_key`` is the deterministic identity of ONE original
``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector event *within this contour*.
It is derived only from stable signal attributes (scanner identity + symbol +
source direction + entry timeframe + the OPEN time of the signal candle), so:

  * the same source signal re-delivered after a restart or a scanner replay
    hashes to the SAME key (duplicate delivery is idempotent);
  * a genuinely new signal candle hashes to a NEW key;
  * the key never depends on wall-clock capture time, prices or process state.

Every contour position carries ``position_key = <namespace>:<event_key>`` —
the namespace guarantees the contour's SHORT state can never collide with the
shared ``open_trades[symbol]`` map of the production Paper engine or with any
other strategy's state.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.scanners.models import SetupCandidate

from .protocol import (
    POSITION_NAMESPACE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    STRATEGY_ID,
)

# 5-minute signal-candle grid (ms) used only when a candidate arrives without
# an explicit signal_candle_open_time.
CANDLE_GRID_MS = 300_000


def _utc_ms(value: datetime, *, field: str) -> int:
    if not isinstance(value, datetime):
        raise ValueError(f"INVALID_{field}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"NAIVE_{field}")
    utc = value.astimezone(timezone.utc)
    return int(utc.timestamp() * 1000)


def signal_candle_open_ms(candidate: SetupCandidate) -> int:
    """Stable open-time (ms) of the signal candle for ``candidate``."""
    explicit = int(candidate.signal_candle_open_time or 0)
    if explicit > 0:
        return explicit
    if candidate.detected_at is None:
        raise ValueError("MISSING_DETECTED_AT")
    detected_ms = _utc_ms(candidate.detected_at, field="DETECTED_AT")
    # Align down to the closed 5m grid: deterministic across re-derivations.
    return detected_ms - (detected_ms % CANDLE_GRID_MS)


def source_identity(candidate: SetupCandidate) -> str:
    """Human-readable source identity (mirrors the production dedup key)."""
    return (
        f"{candidate.scanner_name}|{candidate.symbol}|{candidate.direction}|"
        f"{candidate.entry_timeframe}|{signal_candle_open_ms(candidate)}"
    )


def event_key(candidate: SetupCandidate) -> str:
    """Deterministic contour event key for ONE original V2 source event.

    Raises ``ValueError`` for an unusable identity (wrong scanner/direction,
    missing symbol or an unparseable signal time) — callers treat that as an
    INVALID_EVENT, never as a silent drop.
    """
    if candidate.scanner_name != SOURCE_SCANNER:
        raise ValueError("INVALID_SOURCE_SCANNER")
    if candidate.direction != SOURCE_DIRECTION:
        raise ValueError("INVALID_SOURCE_DIRECTION")
    if not candidate.symbol:
        raise ValueError("MISSING_SYMBOL")

    payload = "|".join((
        STRATEGY_ID,
        SOURCE_SCANNER,
        str(candidate.scanner_version or ""),
        str(candidate.symbol),
        SOURCE_DIRECTION,
        str(candidate.entry_timeframe or "5m"),
        str(signal_candle_open_ms(candidate)),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def position_key(event_key_value: str) -> str:
    """Namespaced position identity: ``<strategy namespace>:<event_key>``."""
    if not event_key_value:
        raise ValueError("MISSING_EVENT_KEY")
    return f"{POSITION_NAMESPACE}:{event_key_value}"
