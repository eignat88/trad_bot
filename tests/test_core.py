from dataclasses import replace

import pytest

from app.backtest.metrics import calculate_metrics, grouped_report
from app.config import Settings
from app.exchange import BybitClient
from app.execution import PaperExchange
from app.indicators import atr_wilder, percent_change, rsi_wilder, volume_ratio
from app.market_data import MarketDataService
from app.models import Candle, MarketSnapshot, Setup, Side, TradeSignal
from app.strategy import StrategyEngine


def snapshot(**overrides):
    base = MarketSnapshot("BTCUSDT", 10_000_000, 100, 103, 99, 102, 200, 108, 0.004,
        3, 55, 100, price_change_15m=1.5, price_change_1h=3, oi_change_15m=8,
        volume_ratio=1.8, local_high=101, local_low=98, previous_high=101, previous_low=99)
    return replace(base, **overrides)


def test_wilder_indicators_and_ratios():
    closes = [44, 44.15, 43.9, 44.35, 44.8, 45, 44.7, 45.2, 45.5, 45.3,
              45.8, 46, 45.6, 46.2, 46.5, 46.1, 46.8]
    assert 0 < rsi_wilder(closes) < 100
    candles = [Candle(i, c, c + 1, c - 1, c, 10) for i, c in enumerate(closes)]
    assert atr_wilder(candles) == pytest.approx(2)
    assert percent_change(108, 100) == 8
    assert volume_ratio([10] * 20 + [18]) == 1.8


def test_bybit_klines_are_reversed_before_conversion():
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"retCode": 0, "result": {"list": [
                ["200", "2", "3", "1", "2", "4"], ["100", "1", "2", "0", "1", "3"]]}}
    class Session:
        def get(self, *args, **kwargs): return Response()
    rows = BybitClient(Settings(), Session()).get_klines("BTCUSDT")
    assert [row.timestamp for row in rows] == [100, 200]


def test_market_data_builds_changes_from_chronological_data():
    settings = Settings()
    service = MarketDataService(None, settings)  # type: ignore[arg-type]
    candles = [Candle(i * 300_000, 100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(30)]
    candles[-1] = replace(candles[-1], volume=20)
    oi = [(c.timestamp, 100 + i) for i, c in enumerate(candles)]
    result = service.build_snapshot("BTCUSDT", candles, oi, 0.01)
    assert result.timestamp == candles[-1].timestamp
    assert result.price_change_15m == pytest.approx((129 - 126) / 126 * 100)
    assert result.oi_change_15m == pytest.approx((129 - 126) / 126 * 100)
    assert result.volume_ratio == 2


def test_three_setups_are_stateful_and_fomo_blocks_long():
    engine = StrategyEngine(Settings())
    trend = engine.evaluate(snapshot())
    assert trend.signal and trend.signal.setup == Setup.TREND_START
    rejected = engine.evaluate(snapshot(price_change_1h=9, oi_change_1h=16))
    assert rejected.rejection_reason == "FOMO_FILTER"

    armed = engine.evaluate(snapshot(timestamp=20_000_000, price_change_15m=0,
        price_change_30m=0.5, oi_change_30m=12, local_high=103, local_low=97, close=100))
    assert armed.rejection_reason == "SETUP_ARMED_AWAIT_BREAKOUT"
    breakout = engine.evaluate(snapshot(timestamp=20_300_000, close=104, volume_ratio=1.3,
        price_change_15m=0, local_high=105))
    assert breakout.signal and breakout.signal.setup == Setup.OI_COMPRESSION

    detected = engine.evaluate(snapshot(timestamp=30_000_000, close=90, low=88,
        price_change_15m=-2, price_change_1h=-9, oi_change_15m=-5,
        oi_change_1h=-20, volume_ratio=2.5))
    assert detected.rejection_reason == "CAPITULATION_DETECTED_AWAIT_CONFIRMATION"
    confirmed = engine.evaluate(snapshot(timestamp=30_300_000, open=90, close=103, low=89,
        previous_high=102, oi_change_5m=-1, price_change_15m=0, volume_ratio=1))
    assert confirmed.signal and confirmed.signal.setup == Setup.CAPITULATION


def test_paper_position_sizing_fees_slippage_and_metrics():
    settings = Settings(initial_balance=10_000, max_symbol_exposure=1)
    exchange = PaperExchange(settings)
    signal = TradeSignal("BTCUSDT", Setup.TREND_START, Side.LONG, 100, 98, 104, .8, "test")
    trade, rejection = exchange.open(signal, snapshot(close=100))
    assert not rejection and trade
    assert trade.entry_price > 100 and trade.position_size > 0 and trade.fee > 0
    closed = exchange.update(snapshot(timestamp=10_300_000, low=99, high=105, close=104))
    assert closed and closed.pnl_usdt > 0 and closed.status == "CLOSED"
    metrics = calculate_metrics([closed])
    assert metrics["trades"] == 1 and metrics["expectancy"] > 0
    assert "BTCUSDT" in grouped_report([closed])["symbol"]


def test_live_orders_require_explicit_switch():
    with pytest.raises(RuntimeError, match="explicit switch"):
        BybitClient(Settings(trading_mode="paper")).create_order("BTCUSDT", "Buy", 1)
