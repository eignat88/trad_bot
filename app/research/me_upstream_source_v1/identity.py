"""Issue #305 — deterministic upstream source event identity.

The ``source_event_key`` is the stable identity of ONE original
``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` / ``LONG`` detector event, derived
ONLY from the normalized original signal identity:

    normalized = "<scanner>|<symbol>|<direction>|<entry_timeframe>|<candle_ms>"
    source_event_key = SHA256("ME_UPSTREAM_SOURCE_V1|" + normalized)

Properties (requirement 4.2):

* the SAME original signal re-detected / re-delivered — after a restart, a
  scanner replay or a code redeploy — hashes to the SAME key (idempotent
  delivery);
* a genuinely new signal candle hashes to a NEW key;
* the key NEVER depends on wall-clock capture time, prices, process state or
  on the V3 / OOS-2 protocol version / protocol fingerprint;
* the normalized identity string is exactly the identity the V3 ledger
  records as ``lineage->>'source_identity'`` (same derivation of
  ``signal_candle_open_ms``), so the read-only coverage reconciler can match
  the two ledgers on it WITHOUT reusing the V3 ``event_key``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.scanners.models import SetupCandidate

from .protocol import (
    IDENTITY_NAMESPACE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)

#: 5-minute signal-candle grid (ms) used ONLY when a candidate arrives
#: without an explicit ``signal_candle_open_time``.  Mirrors the V3 identity
#: derivation so both ledgers normalize identically.
CANDLE_GRID_MS = 300_000


class IdentityError(ValueError):
    """Deterministic identity failure (INVALID_* / MISSING_* reason code)."""


def _utc_ms(value: datetime, *, field: str) -> int:
    if not isinstance(value, datetime):
        raise IdentityError(f"INVALID_{field}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise IdentityError(f"NAIVE_{field}")
    utc = value.astimezone(timezone.utc)
    return int(utc.timestamp() * 1000)


def signal_candle_open_ms(candidate: SetupCandidate) -> int:
    """Stable open-time (ms) of the signal candle for ``candidate``.

    Same derivation as the V3 contour identity so the normalized identity is
    byte-identical across the two ledgers.
    """
    explicit = int(candidate.signal_candle_open_time or 0)
    if explicit > 0:
        return explicit
    if candidate.detected_at is None:
        raise IdentityError("MISSING_DETECTED_AT")
    detected_ms = _utc_ms(candidate.detected_at, field="DETECTED_AT")
    # Align down to the closed 5m grid: deterministic across re-derivations.
    return detected_ms - (detected_ms % CANDLE_GRID_MS)


def normalized_identity(candidate: SetupCandidate) -> str:
    """Normalized original signal identity (the cross-ledger match key)."""
    if candidate.scanner_name != SOURCE_SCANNER:
        raise IdentityError("INVALID_SOURCE_SCANNER")
    if candidate.direction != SOURCE_DIRECTION:
        raise IdentityError("INVALID_SOURCE_DIRECTION")
    if not candidate.symbol:
        raise IdentityError("MISSING_SYMBOL")
    return (
        f"{candidate.scanner_name}|{candidate.symbol}|{candidate.direction}|"
        f"{str(candidate.entry_timeframe or '5m')}|{signal_candle_open_ms(candidate)}"
    )


def source_event_key(candidate: SetupCandidate) -> str:
    """Deterministic upstream event key for ONE original detector event."""
    return identity_key_from_normalized(normalized_identity(candidate))


def identity_key_from_normalized(normalized: str) -> str:
    """Key from a normalized identity string (used by tests / reconciler)."""
    if not normalized or not normalized.strip():
        raise IdentityError("MISSING_SOURCE_IDENTITY")
    payload = f"{IDENTITY_NAMESPACE}|{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalized_identity_values(
    *,
    scanner: str,
    symbol: str,
    direction: str,
    entry_timeframe: str,
    signal_candle_open_ms: int,
) -> str:
    """Normalized identity from raw ledger column values (V3 side match)."""
    return (
        f"{scanner}|{symbol}|{direction}|"
        f"{str(entry_timeframe or '5m')}|{int(signal_candle_open_ms)}"
    )
