"""Close-location adverse filter for MOMENTUM_EXHAUSTION_REVERSE_LONG scanners.

close_location measures where the candle closed relative to its range:
  0.0 = close at low
  1.0 = close at high

For LONG signals, close_location >= threshold indicates buyers defending
the upper part of the candle range — a bullish confirmation.

This module is pure/stateless — no scanner state, no side effects.
All functions accept already-validated inputs and return plain values.
"""
from __future__ import annotations

from datetime import datetime, timezone

CLOSE_LOCATION_THRESHOLD = 0.70

# Project convention: Candle.timestamp is Unix milliseconds (int).
# Policy: >= 10^12 → milliseconds, otherwise → seconds.
_MS_THRESHOLD = 1_000_000_000_000


def normalize_candle_timestamp(value: object) -> str | None:
    """Convert a candle timestamp to a UTC ISO-8601 string.

    Handles every type observed in the project:

      * ``int`` — Unix milliseconds (Candle.timestamp) or Unix seconds
      * ``float`` — Unix timestamp (seconds or milliseconds)
      * ``datetime`` aware — converted directly
      * ``datetime`` naive — assumed UTC
      * ``str`` — returned as-is if already ISO, else None
      * ``None`` — returned as None

    Detection policy for numerics:
      ``>= 10^12 → milliseconds``  (matches Bybit 5-digit year convention)
      ``otherwise → seconds``

    Returns
    -------
    str | None
        UTC ISO-8601 string, e.g. ``"2026-09-23T07:05:00+00:00"``,
        or ``None`` for unrecognised / missing input.
    """
    if value is None:
        return None

    # --- datetime objects ---
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    # --- numeric: int / float ---
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts >= _MS_THRESHOLD:
            # Milliseconds → seconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OSError, ValueError, OverflowError):
            return None

    # --- string: pass through if it looks like ISO ---
    if isinstance(value, str):
        # Basic sanity: must contain a dash and a colon (2026-09-23T...)
        if "-" in value and ":" in value:
            return value
        return None

    return None


def calculate_close_location(candle_high: float, candle_low: float, candle_close: float) -> float | None:
    """Calculate close_location for a single candle.

    Returns a value in [0.0, 1.0] or None for zero-range candles.

    Formula:
        candle_range = high - low
        close_location = (close - low) / candle_range  if candle_range > 0
        close_location = None                           if candle_range == 0

    Parameters
    ----------
    candle_high : float
        Highest price of the candle.
    candle_low : float
        Lowest price of the candle.
    candle_close : float
        Close price of the candle.

    Returns
    -------
    float | None
        Value in [0.0, 1.0] or None when range is zero.
    """
    candle_range = candle_high - candle_low
    if candle_range <= 0:
        return None
    return (candle_close - candle_low) / candle_range


def close_location_passes(
    candle_high: float,
    candle_low: float,
    candle_close: float,
    threshold: float = CLOSE_LOCATION_THRESHOLD,
) -> tuple[bool, float | None]:
    """Evaluate close_location filter and return (passed, value).

    For LONG direction: pass when close_location >= threshold.

    Parameters
    ----------
    candle_high : float
        Highest price of the signal candle.
    candle_low : float
        Lowest price of the signal candle.
    candle_close : float
        Close price of the signal candle.
    threshold : float
        Minimum close_location to pass the filter.  Default 0.70 (frozen).

    Returns
    -------
    tuple[bool, float | None]
        (True, value) if filter passes; (False, value) if rejected or None.
        value is None for zero-range candles (always rejected).
    """
    value = calculate_close_location(candle_high, candle_low, candle_close)
    if value is None:
        return False, None
    return value >= threshold, value
