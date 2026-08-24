from __future__ import annotations

from typing import Any

from app.config import Settings
from app.exchange import BybitClient
from app.models import Side, TradeSignal
from app.risk import PositionSizer


class LiveExchange:
    """Explicitly gated live adapter; order acceptance is not treated as a fill."""
    def __init__(self, client: BybitClient, settings: Settings):
        if settings.trading_mode != "live" or not settings.live_trading_enabled:
            raise RuntimeError("LiveExchange requires both live mode and LIVE_TRADING_ENABLED=true")
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
