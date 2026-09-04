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


def adx(candles: Sequence[Candle], period: int = 14) -> float:
    """Average Directional Index (ADX) using Wilder's smoothing.

    Returns a value between 0 and 100 indicating trend strength.
    ADX > 25 typically indicates a trending market; ADX > 35 is strong trend.
    """
    if len(candles) <= period + 1:
        raise ValueError(f"ADX requires at least {period + 2} candles")

    # Compute True Range, +DM, -DM
    tr_list: list[float] = []
    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []

    for prev, curr in zip(candles, candles[1:]):
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        tr_list.append(tr)

        up_move = curr.high - prev.high
        down_move = prev.low - curr.low

        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    # Wilder's smoothing for TR, +DM, -DM
    atr_val = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm_list[:period]) / period
    minus_dm_smooth = sum(minus_dm_list[:period]) / period

    dx_list: list[float] = []

    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm_list[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm_list[i]) / period

        if atr_val == 0:
            continue

        plus_di = 100.0 * plus_dm_smooth / atr_val
        minus_di = 100.0 * minus_dm_smooth / atr_val
        di_sum = plus_di + minus_di

        if di_sum == 0:
            dx = 0.0
        else:
            dx = 100.0 * abs(plus_di - minus_di) / di_sum
        dx_list.append(dx)

    if not dx_list:
        return 0.0

    # ADX is the smoothed average of DX values
    adx_val = sum(dx_list[:period]) / period if len(dx_list) >= period else sum(dx_list) / len(dx_list)
    for dx in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val


def ema_slope(values: Sequence[float], period: int, lookback: int = 5) -> float:
    """Compute the slope of an EMA as the percentage change over `lookback` bars.

    Returns the percentage change of the EMA value from `lookback` bars ago to now.
    Positive = rising, negative = falling.

    Requires at least `period + lookback` values.
    """
    if len(values) < period + lookback:
        raise ValueError(f"EMA slope requires at least {period + lookback} values")

    current_ema = ema(values, period)
    past_ema = ema(values[:-lookback], period)

    if past_ema == 0:
        return 0.0

    return ((current_ema - past_ema) / past_ema) * 100.0
