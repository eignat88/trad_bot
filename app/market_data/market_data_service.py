from __future__ import annotations

from bisect import bisect_right

from app.config import Settings
from app.exchange import BybitClient
from app.indicators import atr_wilder, percent_change, rsi_wilder, simple_ma, volume_ratio
from app.models import Candle, MarketSnapshot


class MarketDataService:
    def __init__(self, client: BybitClient, settings: Settings):
        self.client, self.settings = client, settings

    @staticmethod
    def _at_or_before(series: list[tuple[int, float]], timestamp: int) -> float:
        timestamps = [item[0] for item in series]
        index = bisect_right(timestamps, timestamp) - 1
        return series[index][1] if index >= 0 else 0.0

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        candles = self.client.get_klines(symbol, self.settings.timeframe, 200)
        oi = self.client.get_open_interest(symbol, "5min", 200)
        funding = self.client.get_funding_rate(symbol)
        return self.build_snapshot(symbol, candles, oi, funding)

    def build_snapshot(self, symbol: str, candles: list[Candle], oi: list[tuple[int, float]],
                       funding: float) -> MarketSnapshot:
        if len(candles) < max(self.settings.volume_period + 1, self.settings.ma_period,
                              self.settings.atr_period + 1, 13):
            raise ValueError("at least 21 chronological candles are required")
        if any(a.timestamp >= b.timestamp for a, b in zip(candles, candles[1:])):
            raise ValueError("candles must be strictly chronological")
        current = candles[-1]
        closes = [c.close for c in candles]
        current_oi = self._at_or_before(oi, current.timestamp)

        def price_change(minutes: int) -> float:
            target = current.timestamp - minutes * 60_000
            candidates = [c.close for c in candles if c.timestamp <= target]
            return percent_change(current.close, candidates[-1]) if candidates else 0.0

        def oi_change(minutes: int) -> float:
            previous = self._at_or_before(oi, current.timestamp - minutes * 60_000)
            return percent_change(current_oi, previous)

        atr = atr_wilder(candles, self.settings.atr_period)
        lookback = candles[-21:-1]
        return MarketSnapshot(
            symbol=symbol, timestamp=current.timestamp, open=current.open, high=current.high,
            low=current.low, close=current.close, volume=current.volume, open_interest=current_oi,
            funding_rate=funding, atr=atr, rsi=rsi_wilder(closes, self.settings.rsi_period),
            ma20=simple_ma(closes, self.settings.ma_period),
            price_change_5m=price_change(5), price_change_15m=price_change(15),
            price_change_30m=price_change(30), price_change_1h=price_change(60),
            oi_change_5m=oi_change(5), oi_change_15m=oi_change(15),
            oi_change_30m=oi_change(30), oi_change_1h=oi_change(60),
            volume_ratio=volume_ratio([c.volume for c in candles], self.settings.volume_period),
            atr_percent=atr / current.close * 100 if current.close else 0,
            local_high=max(c.high for c in lookback), local_low=min(c.low for c in lookback),
            previous_high=candles[-2].high, previous_low=candles[-2].low,
        )
