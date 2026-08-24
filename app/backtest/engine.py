from __future__ import annotations

from collections.abc import Iterable

from app.config import Settings
from app.execution import PaperExchange
from app.models import MarketSnapshot, StrategyDecision, Trade
from app.strategy import StrategyEngine


class BacktestEngine:
    """Replays precomputed historical snapshots through the production strategy."""
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, snapshots: Iterable[MarketSnapshot]) -> tuple[list[Trade], list[StrategyDecision]]:
        strategy, exchange = StrategyEngine(self.settings), PaperExchange(self.settings)
        closed: list[Trade] = []
        decisions: list[StrategyDecision] = []
        for snapshot in sorted(snapshots, key=lambda value: value.timestamp):
            trade = exchange.update(snapshot)
            if trade:
                closed.append(trade)
            decision = strategy.evaluate(snapshot)
            decisions.append(decision)
            if decision.signal:
                exchange.open(decision.signal, snapshot)
        # Open positions are deliberately excluded: no fabricated terminal fill.
        return closed, decisions

    @staticmethod
    def split(snapshots: list[MarketSnapshot], train: float = 0.6,
              validation: float = 0.2) -> tuple[list[MarketSnapshot], list[MarketSnapshot], list[MarketSnapshot]]:
        if train <= 0 or validation <= 0 or train + validation >= 1:
            raise ValueError("invalid train/validation/OOS fractions")
        ordered = sorted(snapshots, key=lambda value: value.timestamp)
        train_end, validation_end = int(len(ordered) * train), int(len(ordered) * (train + validation))
        return ordered[:train_end], ordered[train_end:validation_end], ordered[validation_end:]
