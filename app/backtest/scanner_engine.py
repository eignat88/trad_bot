"""Historical adapter for the production scanner → paper execution path."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.config import Settings
from app.paper.engine import PaperTradeRecord, PaperTradingEngine
from app.scanners.models import MarketContext, SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator


@dataclass(frozen=True)
class HistoricalMarketEvent:
    context: MarketContext
    execution_price: float
    funding_rate_percent: float = 0.0


class InMemoryPaperRepository:
    """Deterministic persistence adapter used only by scanner backtests/tests."""
    def __init__(self) -> None:
        self.opened: list[PaperTradeRecord] = []
        self.closed: list[dict] = []
        self._next_id = 1

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return {"daily_loss_usdt": 0.0, "consecutive_losses": 0}

    def get_latest_paper_account_snapshot(self):
        return None

    def save_paper_trade(self, trade: PaperTradeRecord):
        trade_id = self._next_id
        self._next_id += 1
        self.opened.append(trade)
        return trade_id

    def close_paper_trade(self, **values):
        self.closed.append(values)

    def update_paper_trade_funding(self, *_args):
        return None


class ScannerBacktestEngine:
    """Runs the same scanners and PaperTradingEngine used by paper runtime."""
    def __init__(
        self,
        settings: Settings,
        orchestrator: ScannerOrchestrator | None = None,
        candidate_filter: Callable[[list[SetupCandidate]], list[SetupCandidate]] | None = None,
    ) -> None:
        self.settings = settings
        self.orchestrator = orchestrator or ScannerOrchestrator()
        self.candidate_filter = candidate_filter or (lambda candidates: candidates)

    def run(self, events: list[HistoricalMarketEvent]) -> dict:
        ordered = sorted(events, key=lambda event: event.context.evaluated_at)
        if not ordered:
            return {"closed_trades": [], "account": None, "scanner_stats": []}

        clock_value = ordered[0].context.evaluated_at

        def clock() -> datetime:
            return clock_value

        repository = InMemoryPaperRepository()
        execution = PaperTradingEngine(self.settings, repository, clock=clock)
        closed: list[PaperTradeRecord] = []
        scanner_stats: list[dict] = []

        for event in ordered:
            clock_value = event.context.evaluated_at
            symbol = event.context.symbol
            prices = {symbol: event.execution_price}
            closed.extend(execution.check_exits(
                prices, {symbol: event.funding_rate_percent},
            ))
            candidates, stats = self.orchestrator.scan_all_with_stats(event.context)
            scanner_stats.append(stats)
            execution.check_entries(self.candidate_filter(candidates), prices)

        return {
            "closed_trades": closed,
            "account": execution.snapshot(),
            "scanner_stats": scanner_stats,
            "repository": repository,
        }
