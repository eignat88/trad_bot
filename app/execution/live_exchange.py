from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db.repository import ScannerRepository
from app.paper.readiness import assess_readiness
from app.exchange import BybitClient
from app.models import Side, TradeSignal
from app.risk import PositionSizer


class LiveExchange:
    """Explicitly gated live adapter; order acceptance is not treated as a fill."""
    def __init__(
        self,
        client: BybitClient,
        settings: Settings,
        repository: ScannerRepository | None = None,
    ):
        if settings.trading_mode != "live" or not settings.live_trading_enabled:
            raise RuntimeError("LiveExchange requires both live mode and LIVE_TRADING_ENABLED=true")

        owns_repository = repository is None
        repo = repository or ScannerRepository(
            host="localhost", port=5432, database="trad_bot", user="postgres",
        )
        try:
            readiness = assess_readiness(repo.get_paper_forward_summary(), settings)
        finally:
            if owns_repository:
                repo.close()
        if not readiness.eligible:
            reasons = "; ".join(readiness.failed_checks)
            raise RuntimeError(f"LiveExchange blocked by paper forward-test gate: {reasons}")
        self.client, self.settings = client, settings

    def submit(self, signal: TradeSignal, balance: float) -> dict[str, Any]:
        quantity, _ = PositionSizer.calculate(balance, self.settings.risk_per_trade,
            signal.entry, signal.stop, self.settings.max_symbol_exposure)
        side = "Buy" if signal.side == Side.LONG else "Sell"
        return self.client.create_order(signal.symbol, side, quantity)

    @staticmethod
    def confirmed_fill(order_status: dict[str, Any]) -> bool:
        """Only a separately fetched order status may confirm execution."""
        return order_status.get("orderStatus") == "Filled"
