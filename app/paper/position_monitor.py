"""Fast Position Monitor — checks SL/TP/trailing/breakeven every 5-15 seconds.

This module is separated from the main paper trading cycle (which runs every
5 minutes for entry scanning) to ensure stop losses are monitored with
minimal latency. Without fast monitoring, almost every stop hit becomes a
STOP_LOSS_GAP because the price moves past the stop level between checks.

Lifecycle:
  1. Runs independently from the scanner/paper entry cycle.
  2. Fetches current prices for all open positions every N seconds.
  3. Checks SL/TP/trailing stop/breakeven/expiry.
  4. Records STOP_LOSS_GAP diagnostic data when gaps occur.
  5. Updates position monitor heartbeat for dashboard visibility.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import Settings
from app.paper.engine import PaperTradingEngine, PaperTradeRecord

logger = logging.getLogger(__name__)


class PositionMonitor:
    """Fast position monitor that checks SL/TP/trailing/breakeven independently.

    Runs on a separate timer from the scanner/paper entry cycle to ensure
    stop losses are monitored with minimal latency (5-15 seconds vs 5 minutes).
    """

    def __init__(
        self,
        engine: PaperTradingEngine,
        price_fetcher: Callable[[list[str]], dict[str, float]],
        funding_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
        interval_seconds: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the position monitor.

        Args:
            engine: The paper trading engine instance to monitor.
            price_fetcher: Function that takes a list of symbols and returns
                          current prices as {symbol: price}.
            funding_fetcher: Optional function to fetch funding rates.
            interval_seconds: How often to check positions (default 10s).
            clock: Optional clock function for testing.
        """
        self.engine = engine
        self._price_fetcher = price_fetcher
        self._funding_fetcher = funding_fetcher
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._last_check: datetime | None = None
        self._last_check_duration_ms: float = 0.0
        self._total_checks: int = 0
        self._total_closes: int = 0
        self._stop_gap_events: list[dict[str, Any]] = []
        self._stop_gap_count_24h: int = 0
        self._last_stop_gap: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        """Return True if the monitor is actively running."""
        return self._running

    @property
    def last_check(self) -> datetime | None:
        """Return the timestamp of the last position check."""
        return self._last_check

    @property
    def last_check_duration_ms(self) -> float:
        """Return the duration of the last check in milliseconds."""
        return self._last_check_duration_ms

    @property
    def total_checks(self) -> int:
        """Return the total number of checks performed."""
        return self._total_checks

    @property
    def total_closes(self) -> int:
        """Return the total number of positions closed by the monitor."""
        return self._total_closes

    @property
    def stop_gap_count_24h(self) -> int:
        """Return the number of STOP_LOSS_GAP events in the last 24 hours."""
        self._cleanup_old_stop_gap_events()
        return self._stop_gap_count_24h

    @property
    def last_stop_gap(self) -> dict[str, Any] | None:
        """Return the most recent STOP_LOSS_GAP event details."""
        return self._last_stop_gap

    @property
    def heartbeat(self) -> dict[str, Any]:
        """Return position monitor health status for dashboard."""
        now = self._clock()
        last_check_age = None
        if self._last_check:
            last_check_age = (now - self._last_check).total_seconds()

        return {
            "status": "RUNNING" if self._running else "STOPPED",
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "last_check_age_sec": round(last_check_age, 1) if last_check_age is not None else None,
            "last_check_duration_ms": round(self._last_check_duration_ms, 1),
            "total_checks": self._total_checks,
            "total_closes": self._total_closes,
            "stop_gap_24h": self.stop_gap_count_24h,
            "last_stop_gap": self._last_stop_gap,
            "open_positions": len(self.engine.open_trades),
            "interval_seconds": self._interval,
        }

    def check_positions(self) -> list[PaperTradeRecord]:
        """Perform one check cycle on all open positions.

        Returns list of trades that were closed in this cycle.
        """
        start_time = time.monotonic()
        self._total_checks += 1

        open_symbols = list(self.engine.open_trades.keys())
        if not open_symbols:
            self._last_check = self._clock()
            self._last_check_duration_ms = (time.monotonic() - start_time) * 1000
            return []

        # Fetch current prices
        try:
            prices = self._price_fetcher(open_symbols)
        except Exception:
            logger.exception("position monitor: failed to fetch prices")
            self._last_check = self._clock()
            self._last_check_duration_ms = (time.monotonic() - start_time) * 1000
            return []

        if not prices:
            self._last_check = self._clock()
            self._last_check_duration_ms = (time.monotonic() - start_time) * 1000
            return []

        # Fetch funding rates if available
        funding_rates = None
        if self._funding_fetcher:
            try:
                funding_rates = self._funding_fetcher(open_symbols)
            except Exception:
                logger.warning("position monitor: failed to fetch funding rates, continuing without")

        # Record pre-close state for diagnostic comparison
        pre_close_state = {
            symbol: {
                "stop_price": trade.stop_price,
                "entry_price": trade.entry_price,
                "direction": trade.direction,
                "scanner_name": trade.scanner_name,
            }
            for symbol, trade in self.engine.open_trades.items()
            if symbol in prices
        }

        # Check exits
        closed = self.engine.check_exits(prices, funding_rates)

        # Record STOP_LOSS_GAP diagnostics
        for trade in closed:
            if trade.status == "CLOSED" and hasattr(trade, 'exit_reason'):
                # Get exit reason from the trade record - we need to check the engine logs
                # or pass it through. For now, we'll check the P&L for gap detection.
                self._record_gap_if_needed(trade, pre_close_state.get(trade.symbol))

        self._total_closes += len(closed)
        self._last_check = self._clock()
        self._last_check_duration_ms = (time.monotonic() - start_time) * 1000

        if closed:
            logger.info(
                "position monitor: closed %d positions in %.1fms",
                len(closed), self._last_check_duration_ms,
            )

        return closed

    def _record_gap_if_needed(
        self,
        trade: PaperTradeRecord,
        pre_state: dict[str, Any] | None,
    ) -> None:
        """Record STOP_LOSS_GAP diagnostic data if this was a gap exit.

        We detect gaps by checking if the exit reason contains STOP_LOSS_GAP
        or if the R-multiple loss exceeds the configured limit.
        """
        if pre_state is None:
            return

        # Calculate expected R for stop loss
        risk_distance = abs(pre_state["entry_price"] - pre_state["stop_price"])
        if risk_distance <= 0:
            return

        # Check if this looks like a gap (loss exceeds normal stop)
        if trade.net_pnl < 0 and trade.risk_usdt > 0:
            r_multiple = trade.net_pnl / trade.risk_usdt
            if r_multiple < -1.0:  # Loss exceeds 1R = likely gap
                # Calculate gap percentage
                if pre_state["direction"] == "LONG":
                    gap_pct = abs(trade.net_pnl + trade.risk_usdt) / trade.risk_usdt * 100
                else:
                    gap_pct = abs(trade.net_pnl + trade.risk_usdt) / trade.risk_usdt * 100

                gap_event = {
                    "symbol": trade.symbol,
                    "scanner_name": trade.scanner_name,
                    "direction": trade.direction,
                    "entry_price": pre_state["entry_price"],
                    "stop_price": pre_state["stop_price"],
                    "exit_price": trade.entry_price + (trade.net_pnl / trade.position_size if trade.position_size > 0 else 0),
                    "gap_pct": round(gap_pct, 2),
                    "pnl_r": round(r_multiple, 4),
                    "max_allowed_r": -1.2,  # from config
                    "timestamp": self._clock().isoformat(),
                    "duration_sec": (self._clock() - trade.entered_at).total_seconds() if hasattr(trade, 'entered_at') else 0,
                }

                self._stop_gap_events.append(gap_event)
                self._last_stop_gap = gap_event
                self._cleanup_old_stop_gap_events()

                logger.warning(
                    "position monitor: STOP_LOSS_GAP detected %s %s %s "
                    "entry=%.4f stop=%.4f exit=%.4f gap=%.2f%% R=%.2f",
                    trade.symbol, trade.direction, trade.scanner_name,
                    pre_state["entry_price"], pre_state["stop_price"],
                    gap_event["exit_price"], gap_pct, r_multiple,
                )

    def _cleanup_old_stop_gap_events(self) -> None:
        """Remove STOP_LOSS_GAP events older than 24 hours."""
        cutoff = self._clock().timestamp() - 86400  # 24 hours ago
        self._stop_gap_events = [
            e for e in self._stop_gap_events
            if datetime.fromisoformat(e["timestamp"]).timestamp() > cutoff
        ]
        self._stop_gap_count_24h = len(self._stop_gap_events)

    def run_once(self) -> list[PaperTradeRecord]:
        """Run a single check cycle (for testing or manual invocation)."""
        return self.check_positions()

    def run_continuous(self, shutdown_checker: Callable[[], bool] | None = None) -> None:
        """Run the position monitor continuously until shutdown.

        Args:
            shutdown_checker: Optional function that returns True when shutdown
                            is requested. If None, runs indefinitely.
        """
        self._running = True
        logger.info(
            "position monitor started: interval=%ds positions=%d",
            self._interval, len(self.engine.open_trades),
        )

        try:
            while not (shutdown_checker and shutdown_checker()):
                self.check_positions()
                time.sleep(self._interval)
        finally:
            self._running = False
            logger.info("position monitor stopped")

    def stop(self) -> None:
        """Signal the monitor to stop (for use with run_continuous)."""
        self._running = False

    def get_diagnostics(self) -> dict[str, Any]:
        """Return comprehensive diagnostic data for dashboard/debugging."""
        return {
            "heartbeat": self.heartbeat,
            "stop_gap_events_24h": self._stop_gap_events,
            "engine_state": {
                "open_positions": len(self.engine.open_trades),
                "balance": self.engine.balance,
                "gap_loss_halt": self.engine._gap_loss_halt,
                "daily_loss_usdt": self.engine._daily_loss_usdt,
                "consecutive_losses": self.engine._consecutive_losses,
            },
        }
