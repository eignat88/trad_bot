from __future__ import annotations

from collections.abc import Sequence

from app.models import Candle


def percent_change(current: float, previous: float) -> float:
    return ((current - previous) / previous * 100.0) if previous else 0.0


def simple_ma(values: Sequence[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        raise ValueError("insufficient values for moving average")
    return sum(values[-period:]) / period


def rsi_wilder(closes: Sequence[float], period: int = 14) -> float:
    """RSI with Wilder's initial SMA and recursive smoothing."""
    if period <= 0 or len(closes) <= period:
        raise ValueError("RSI requires period + 1 closes")
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr_wilder(candles: Sequence[Candle], period: int = 14) -> float:
    if period <= 0 or len(candles) <= period:
        raise ValueError("ATR requires period + 1 candles")
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def volume_ratio(volumes: Sequence[float], period: int = 20) -> float:
    """Compare current volume with N preceding completed observations."""
    if len(volumes) <= period:
        raise ValueError("volume ratio requires period + 1 values")
    average = sum(volumes[-period - 1:-1]) / period
    return volumes[-1] / average if average else 0.0


def ema(values: Sequence[float], period: int) -> float:
    """Exponential Moving Average."""
    if len(values) < period:
        raise ValueError(f"EMA requires at least {period} values")
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for val in values[period:]:
        result = (val - result) * multiplier + result
    return result


def bollinger_bands(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """Returns (upper, middle, lower) Bollinger Bands."""
    if len(values) < period:
        raise ValueError(f"Bollinger Bands require at least {period} values")
    window = values[-period:]
    middle = sum(window) / period
    variance = sum((v - middle) ** 2 for v in window) / period
    std = variance ** 0.5
    return middle + num_std * std, middle, middle - num_std * std
