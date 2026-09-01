from datetime import datetime, timedelta, timezone

from app.backtest import HistoricalMarketEvent, ScannerBacktestEngine
from app.config import Settings
from app.models import Candle
from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels, SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator


class DeterministicScanner:
    name = "DETERMINISTIC"

    def scan(self, ctx):
        if ctx.candles_5m[-1].close != 100:
            return []
        return [SetupCandidate(
            scanner_name=self.name,
            scanner_version="test",
            symbol=ctx.symbol,
            direction="LONG",
            entry_timeframe="5m",
            reference_price=100,
            entry_zone_low=99,
            entry_zone_high=101,
            invalidation_price=90,
            target_1=110,
            score=90,
            reasons=("DETERMINISTIC_SETUP",),
            features={"trend_alignment": True, "htf_context": True},
        )]


def context(at, price):
    candle = Candle(int(at.timestamp() * 1000), price, price, price, price, 10)
    return MarketContext(
        symbol="BTCUSDT", candles_5m=(candle,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="RANGE", levels=MarketLevels(), evaluated_at=at,
    )


def test_scanner_to_execution_backtest_is_deterministic():
    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"DETERMINISTIC": DeterministicScanner()}
    settings = Settings(taker_fee=0, slippage_percent=0)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        HistoricalMarketEvent(context(start, 100), 100),
        HistoricalMarketEvent(context(start + timedelta(minutes=5), 110), 110),
    ]

    first = ScannerBacktestEngine(settings, orchestrator).run(events)

    # A fresh orchestrator proves no mutable run state leaked into the result.
    orchestrator2 = ScannerOrchestrator(enabled_scanners=[])
    orchestrator2.scanners = {"DETERMINISTIC": DeterministicScanner()}
    second = ScannerBacktestEngine(settings, orchestrator2).run(events)

    assert len(first["closed_trades"]) == 1
    assert first["repository"].closed[0]["exit_reason"] == "TAKE_PROFIT_1"
    assert first["repository"].closed[0]["pnl_usdt"] == second["repository"].closed[0]["pnl_usdt"]
    assert first["account"] == second["account"]
