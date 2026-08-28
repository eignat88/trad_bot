from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.db.repository import ScannerRepository
from app.paper.readiness import assess_readiness
from app.exchange import BybitClient
from app.models import TradeSignal

logger = logging.getLogger(__name__)


class LiveTradingBlocked(RuntimeError):
    """Fail-closed refusal to perform an unsafe live operation."""


class LiveExchange:
    """Live adapter guarded until protected execution is fully implemented.

    Setting TRADING_MODE=live or LIVE_TRADING_ENABLED=true is intentionally not
    sufficient.  The internal safety capability flag cannot be populated from
    environment/configuration, so operators cannot accidentally bypass it.
    """

    def __init__(
        self,
        client: BybitClient,
        settings: Settings,
        repository: ScannerRepository | None = None,
    ):
        self.client, self.settings = client, settings
        if settings.trading_mode != "live" or not settings.live_trading_enabled:
            self._block("live mode and explicit enablement are required")
        if not settings.live_safety_ready:
            self._block("protected order execution is not implemented")

        owns_repository = repository is None
        repo = repository or ScannerRepository(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            backend="postgres",
        )
        try:
            readiness = assess_readiness(repo.get_paper_forward_summary(), settings)
        finally:
            if owns_repository:
                repo.close()
        if not readiness.eligible:
            self._block("paper forward-test gate: " + "; ".join(readiness.failed_checks))

    @staticmethod
    def _block(reason: str) -> None:
        message = f"LIVE_TRADING_BLOCKED: {reason}"
        logger.critical(message)
        raise LiveTradingBlocked(message)

    def submit(self, signal: TradeSignal, balance: float) -> dict[str, Any]:
        """Reject all orders until the protected lifecycle exists.

        There is deliberately no market-order fallback.  Future implementation
        must validate instrument filters, attach SL/TP, confirm the exchange
        order id, and reconcile state before this method may call the client.
        """
        if not self.settings.live_safety_ready:
            self._block("protected order execution is not implemented")
        if balance <= 0:
            self._block("invalid account balance")
        if signal.entry <= 0 or signal.stop <= 0:
            self._block("valid entry and stop loss are mandatory")
        if signal.take_profit <= 0:
            self._block("take profit is mandatory")
        self._block("protected order execution is not implemented")

    @staticmethod
    def confirmed_fill(order_status: dict[str, Any]) -> bool:
        """Only a separately fetched status with an exchange id confirms fill."""
        return bool(order_status.get("orderId")) and order_status.get("orderStatus") == "Filled"
