from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.exchange.bybit_client import BybitClient
from app.indicators import atr_wilder, bollinger_bands, ema, rsi_wilder, simple_ma, volume_ratio
from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels


def _build_indicators(candles: list[Candle]) -> IndicatorSnapshot:
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    atr_val = atr_wilder(candles, 14) if len(candles) > 14 else 0
    rsi_val = rsi_wilder(closes, 14) if len(closes) > 14 else 50.0
    ema20_val = ema(closes, 20) if len(closes) >= 20 else closes[-1]
    ema50_val = ema(closes, 50) if len(closes) >= 50 else closes[-1]
    ema200_val = ema(closes, 200) if len(closes) >= 200 else closes[-1]
    vol_sma = sum(volumes[-20:]) / min(20, len(volumes))
    bb_upper, bb_mid, bb_lower = (0.0, 0.0, 0.0)
    if len(closes) >= 20:
        bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20)
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0
    return IndicatorSnapshot(
        atr=atr_val, rsi=rsi_val, ema20=ema20_val, ema50=ema50_val,
        ema200=ema200_val, bb_upper=bb_upper, bb_lower=bb_lower,
        bb_width=bb_width, volume_sma=vol_sma,
    )


def _find_levels(candles_1d: list[Candle] | None) -> MarketLevels:
    if not candles_1d or len(candles_1d) < 5:
        return MarketLevels()
    prev_day = candles_1d[-2] if len(candles_1d) >= 2 else None
    prev_week_high = max(c.high for c in candles_1d[-5:])
    prev_week_low = min(c.low for c in candles_1d[-5:])
    return MarketLevels(
        previous_day_high=prev_day.high if prev_day else 0,
        previous_day_low=prev_day.low if prev_day else 0,
        previous_week_high=prev_week_high,
        previous_week_low=prev_week_low,
    )


def build_market_context(
    client: BybitClient,
    symbol: str,
    settings: Settings,
) -> MarketContext:
    candles_5m = client.get_klines(symbol, "5", 200)
    candles_15m = client.get_klines(symbol, "15", 200)
    candles_1h = client.get_klines(symbol, "60", 200)
    candles_4h = client.get_klines(symbol, "240", 200)
    indicators = _build_indicators(candles_1h)
    levels = _find_levels(None)
    return MarketContext(
        symbol=symbol,
        candles_5m=tuple(candles_5m),
        candles_15m=tuple(candles_15m),
        candles_1h=tuple(candles_1h),
        candles_4h=tuple(candles_4h),
        indicators=indicators,
        market_regime=None,
        levels=levels,
        evaluated_at=datetime.now(timezone.utc),
    )
