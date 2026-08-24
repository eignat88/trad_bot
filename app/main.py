from __future__ import annotations

import argparse

from app.config import load_settings
from app.exchange import BybitClient
from app.execution import PaperExchange
from app.market_data import MarketDataService
from app.models import StrategyDecision
from app.storage import JsonlRepository
from app.strategy import StrategyEngine


def run_once() -> None:
    settings = load_settings()
    if settings.trading_mode == "live" and not settings.live_trading_enabled:
        raise RuntimeError("live mode is disabled; set LIVE_TRADING_ENABLED=true explicitly")
    client = BybitClient(settings)
    service, strategy = MarketDataService(client, settings), StrategyEngine(settings)
    repository = JsonlRepository(settings.data_file, settings.rejection_file, settings.market_data_file)
    exchange = PaperExchange(settings, repository) if settings.trading_mode == "paper" else None
    for symbol in settings.symbols:
        snapshot = service.get_snapshot(symbol)
        repository.save_snapshot(snapshot)
        decision = strategy.evaluate(snapshot)
        print(f"{symbol}: {decision.state} - {decision.signal or decision.rejection_reason or 'NO_SIGNAL'}")
        if decision.rejection_reason:
            repository.save_rejection(symbol, snapshot.timestamp, decision)
        if exchange and decision.signal:
            trade, rejection = exchange.open(decision.signal, snapshot)
            print(f"paper: {trade.trade_id if trade else rejection}")
            if rejection:
                repository.save_rejection(symbol, snapshot.timestamp,
                    StrategyDecision(None, decision.signal.setup, rejection, decision.state))


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Price/OI research bot (paper by default)")
    parser.add_argument("--check-config", action="store_true", help="validate configuration without network calls")
    args = parser.parse_args()
    settings = load_settings()
    if args.check_config:
        print(f"Configuration valid: mode={settings.trading_mode}, category={settings.category}")
        return
    run_once()
