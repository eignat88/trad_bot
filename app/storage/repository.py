from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.models import MarketSnapshot, StrategyDecision, Trade
from app.storage.safe_jsonl import append_record


class JsonlRepository:
    def __init__(self, trade_file: str, rejection_file: str,
                 market_data_file: str = "data/market_snapshots.jsonl"):
        self.trade_file, self.rejection_file = Path(trade_file), Path(rejection_file)
        self.market_data_file = Path(market_data_file)

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        append_record(path, record)

    def save_trade(self, trade: Trade) -> None:
        self._append(self.trade_file, trade.to_dict())

    def save_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._append(self.market_data_file, asdict(snapshot))

    def save_rejection(self, symbol: str, timestamp: int, decision: StrategyDecision) -> None:
        self._append(self.rejection_file, {"timestamp": timestamp, "symbol": symbol,
            "setup": decision.rejected_setup.value if decision.rejected_setup else None,
            "result": "REJECTED", "reason": decision.rejection_reason, "state": decision.state})
