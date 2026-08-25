from __future__ import annotations

from app.models import Candle
from app.scanners.models import SwingPoint


def find_swing_highs(candles: list[Candle], lookback: int = 5) -> list[SwingPoint]:
    swings: list[SwingPoint] = []
    for i in range(lookback, len(candles) - lookback):
        high = candles[i].high
        is_swing = all(
            candles[i - j].high <= high and candles[i + j].high <= high
            for j in range(1, lookback + 1)
        )
        if is_swing:
            swings.append(SwingPoint(candles[i].timestamp, high, "high", i))
    return swings


def find_swing_lows(candles: list[Candle], lookback: int = 5) -> list[SwingPoint]:
    swings: list[SwingPoint] = []
    for i in range(lookback, len(candles) - lookback):
        low = candles[i].low
        is_swing = all(
            candles[i - j].low >= low and candles[i + j].low >= low
            for j in range(1, lookback + 1)
        )
        if is_swing:
            swings.append(SwingPoint(candles[i].timestamp, low, "low", i))
    return swings


def find_order_blocks(candles: list[Candle], lookback: int = 5) -> list[dict]:
    obs: list[dict] = []
    for i in range(lookback + 1, len(candles)):
        body_prev = candles[i - 1].close - candles[i - 1].open
        body_curr = candles[i].close - candles[i].open
        avg_range = sum(c.high - c.low for c in candles[i - lookback:i]) / lookback

        if body_prev < 0 and body_curr > 0 and body_curr > 2 * abs(body_prev):
            if candles[i].high - candles[i].low > avg_range * 0.8:
                obs.append({
                    "type": "bullish",
                    "high": candles[i - 1].high,
                    "low": candles[i - 1].low,
                    "timestamp": candles[i - 1].timestamp,
                    "index": i - 1,
                })

        if body_prev > 0 and body_curr < 0 and abs(body_curr) > 2 * body_prev:
            if candles[i].high - candles[i].low > avg_range * 0.8:
                obs.append({
                    "type": "bearish",
                    "high": candles[i].high,
                    "low": candles[i].low,
                    "timestamp": candles[i].timestamp,
                    "index": i,
                })
    return obs


def detect_choch(candles: list[Candle], swing_lookback: int = 5) -> str | None:
    if len(candles) < swing_lookback * 3:
        return None
    highs = find_swing_highs(candles, swing_lookback)
    lows = find_swing_lows(candles, swing_lookback)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last_close = candles[-1].close
    if last_close > highs[-1].price:
        if lows[-1].price < lows[-2].price:
            return "bullish_choch"
    if last_close < lows[-1].price:
        if highs[-1].price > highs[-2].price:
            return "bearish_choch"
    return None


def detect_displacement(candles: list[Candle], n: int = 3, threshold: float = 1.5) -> str | None:
    if len(candles) < n + 1:
        return None
    recent = candles[-n:]
    avg_range = sum(c.high - c.low for c in candles[-20:]) / min(20, len(candles))
    avg_vol = sum(c.volume for c in candles[-20:]) / min(20, len(candles))
    total_move = recent[-1].close - recent[0].open
    total_range = max(c.high for c in recent) - min(c.low for c in recent)
    avg_recent_vol = sum(c.volume for c in recent) / n
    if total_range > avg_range * threshold and avg_recent_vol > avg_vol:
        if total_move > 0:
            return "bullish_displacement"
        if total_move < 0:
            return "bearish_displacement"
    return None


def detect_liquidity_sweep(candles: list[Candle], level: float, is_high: bool) -> bool:
    if not candles:
        return False
    last = candles[-1]
    if is_high:
        return last.high > level and last.close < level
    return last.low < level and last.close > level


def detect_rejection(candle: Candle, direction: str) -> bool:
    body = abs(candle.close - candle.open)
    if direction == "bullish":
        lower_wick = min(candle.open, candle.close) - candle.low
        return lower_wick > 2 * body and body > 0
    upper_wick = candle.high - max(candle.open, candle.close)
    return upper_wick > 2 * body and body > 0


def find_nearest_swing_high(candles: list[Candle], max_offset: int = 20) -> float | None:
    highs = find_swing_highs(candles, lookback=3)
    if not highs:
        return None
    return highs[-1].price


def find_nearest_swing_low(candles: list[Candle], max_offset: int = 20) -> float | None:
    lows = find_swing_lows(candles, lookback=3)
    if not lows:
        return None
    return lows[-1].price
