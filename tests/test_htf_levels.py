from datetime import datetime, timezone

from app.config import Settings
from app.models import Candle
from app.scanners.context_builder import _find_levels, build_market_context
from app.scanners.liquidity_reversal import LiquidityReversalScanner
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels


def candles(count, start=100):
    return [Candle(i + 1, start + i, start + i + 2, start + i - 2, start + i + 1, 10) for i in range(count)]


class FakeClient:
    def __init__(self):
        self.intervals = []

    def get_klines(self, symbol, interval, limit):
        self.intervals.append(interval)
        return candles(8 if interval == "D" else 200)


def test_context_loads_daily_candles_and_builds_nonzero_levels():
    client = FakeClient()
    ctx = build_market_context(client, "BTCUSDT", Settings())
    assert "D" in client.intervals
    assert ctx.levels.previous_day_high > 0
    assert ctx.levels.previous_day_low > 0
    assert ctx.levels.previous_week_high > 0
    assert ctx.levels.previous_week_low > 0


def test_known_daily_weekly_levels():
    daily = [
        Candle(1, 10, 12, 8, 11, 1), Candle(2, 11, 15, 9, 12, 1),
        Candle(3, 12, 14, 7, 13, 1), Candle(4, 13, 16, 10, 14, 1),
        Candle(5, 14, 18, 11, 15, 1), Candle(6, 15, 17, 12, 16, 1),
    ]
    levels = _find_levels(daily)
    assert (levels.previous_day_high, levels.previous_day_low) == (18, 11)
    assert (levels.previous_week_high, levels.previous_week_low) == (18, 7)


def test_liquidity_reversal_skips_unavailable_htf_levels(caplog):
    caplog.set_level("INFO")
    c = tuple(candles(40))
    ctx = MarketContext(
        symbol="BTCUSDT", candles_5m=c, candles_15m=c,
        candles_1h=c, candles_4h=c, indicators=IndicatorSnapshot(),
        market_regime="RANGE", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    assert LiquidityReversalScanner().scan(ctx) == []
    assert "HTF levels unavailable" in caplog.text
