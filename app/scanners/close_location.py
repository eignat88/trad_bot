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


CLOSE_LOCATION_THRESHOLD = 0.70


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
