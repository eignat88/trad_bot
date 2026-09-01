"""Fast Position Monitor — checks SL/TP/trailing/breakeven independently.

Runs in a background thread at high frequency (default 10 s) to minimise
STOP_LOSS_GAP events caused by slow monitoring.  The main paper-runner
cycle (every 300 s) handles entry scanning and account snapshots; this
module handles ONLY exit monitoring.

Thread safety
-------------
The monitor shares ``engine.open_trades`` and ``engine.balance`` with the
main entry-checking thread.  Access is synchronised via
``engine.trading_lock`` — a ``threading.Lock`` added to the engine for
this purpose.  Both threads must acquire the lock before reading or
mutating shared state.

Lifecycle
---------
1. ``start()`` launches a daemon thread.
2. The thread loops: fetch prices -> acquire lock -> check_exits -> release lock.
3. ``stop()`` sets a shutdown flag and joins the thread.
4. ``heartbeat`` property returns live status for the dashboard.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.paper.engine import PaperTradingEngine, PaperTradeRecord

logger = logging.getLogger(__name__)


class PositionMonitor:
    """Fast position monitor running in a background daemon thread.

    The monitor calls ``engine.check_exits()`` every *interval_seconds*
    while holding ``engine.trading_lock`` so that concurrent entry checks
    in the main thread are properly serialised.
    """

    def __init__(
        self,
        engine: PaperTradingEngine,
        price_fetcher: Callable[[list[str]], dict[str, float]],
        funding_fetcher: Callable[[list[str]], dict[str, float]] | None = None,
        interval_seconds: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self._price_fetcher = price_fetcher
        self._funding_fetcher = funding_fetcher
        self._interval = interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        # Thread management
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()

        # Diagnostics (written only by the monitor thread)
        self._last_check: datetime | None = None
        self._last_check_duration_ms: float = 0.0
        self._total_checks: int = 0
        self._total_closes: int = 0

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitor thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="position-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "position monitor started: interval=%ds positions=%d",
            self._interval, len(self.engine.open_trades),
        )

    def stop(self) -> None:
        """Signal the monitor to stop and wait for the thread to finish."""
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2 + 5)
            self._thread = None
        logger.info("position monitor stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_check(self) -> datetime | None:
        return self._last_check

    @property
    def last_check_duration_ms(self) -> float:
        return self._last_check_duration_ms

    @property
    def total_checks(self) -> int:
        return self._total_checks

    @property
    def total_closes(self) -> int:
        return self._total_closes

    @property
    def stop_gap_count_24h(self) -> int:
        """Return the engine's STOP_LOSS_GAP count for the last 24 hours."""
        return self.engine.stop_gap_count_24h

    @property
    def last_stop_gap(self) -> dict[str, Any] | None:
        """Return the most recent engine STOP_LOSS_GAP event details."""
        return self.engine.last_stop_gap

    @property
    def heartbeat(self) -> dict[str, Any]:
        """Return position monitor health status for dashboard."""
        now = self._clock()
        last_check_age = None
        if self._last_check:
            last_check_age = (now - self._last_check).total_seconds()

        return {
            "status": "RUNNING" if self.is_running else "STOPPED",
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "last_check_age_sec": (
                round(last_check_age, 1) if last_check_age is not None else None
            ),
            "last_check_duration_ms": round(self._last_check_duration_ms, 1),
            "total_checks": self._total_checks,
            "total_closes": self._total_closes,
            "stop_gap_24h": self.stop_gap_count_24h,
            "last_stop_gap": self.last_stop_gap,
            "open_positions": len(self.engine.open_trades),
            "interval_seconds": self._interval,
        }

    def run_once(self) -> list[PaperTradeRecord]:
        """Run a single check cycle (for testing or manual invocation)."""
        return self._check_positions()

    def get_diagnostics(self) -> dict[str, Any]:
        """Return comprehensive diagnostic data for dashboard/debugging."""
        return {
            "heartbeat": self.heartbeat,
            "stop_gap_events_24h": self.engine.get_stop_gap_diagnostics(),
            "engine_state": {
                "open_positions": len(self.engine.open_trades),
                "balance": self.engine.balance,
                "gap_loss_halt": self.engine._gap_loss_halt,
                "daily_loss_usdt": self.engine._daily_loss_usdt,
                "consecutive_losses": self.engine._consecutive_losses,
            },
        }

    # ------------------------------------------------------------------
    # INTERNAL — background thread
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Background thread main loop."""
        while not self._shutdown.is_set():
            try:
                self._check_positions()
            except Exception:
                logger.exception(
                    "position monitor: unhandled error in check cycle"
                )
            # Wait for the interval or until shutdown is signalled
            self._shutdown.wait(timeout=self._interval)

    def _check_positions(self) -> list[PaperTradeRecord]:
        """Check all open positions for SL/TP/trailing/expiry.

        Acquires ``engine.trading_lock`` to safely read and mutate shared state.
        """
        start_time = time.monotonic()
        self._total_checks += 1

        # Snapshot open symbols outside the lock
        open_symbols = list(self.engine.open_trades.keys())
        if not open_symbols:
            self._last_check = self._clock()
            self._last_check_duration_ms = (
                time.monotonic() - start_time
            ) * 1000
            return []

        # Fetch prices outside the lock (pure network I/O)
        try:
            prices = self._price_fetcher(open_symbols)
        except Exception:
            logger.exception("position monitor: failed to fetch prices")
            self._last_check = self._clock()
            self._last_check_duration_ms = (
                time.monotonic() - start_time
            ) * 1000
            return []

        if not prices:
            self._last_check = self._clock()
            self._last_check_duration_ms = (
                time.monotonic() - start_time
            ) * 1000
            return []

        # Fetch funding rates outside the lock
        funding_rates = None
        if self._funding_fetcher:
            try:
                funding_rates = self._funding_fetcher(open_symbols)
            except Exception:
                logger.warning(
                    "position monitor: failed to fetch funding rates"
                )

        # --- ACQUIRE LOCK for engine state mutation ---
        lock = getattr(self.engine, "trading_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            closed = self.engine.check_exits(prices, funding_rates)
        finally:
            if lock is not None:
                lock.release()
        # --- RELEASE LOCK ---

        self._total_closes += len(closed)
        self._last_check = self._clock()
        self._last_check_duration_ms = (
            time.monotonic() - start_time
        ) * 1000

        if closed:
            logger.info(
                "position monitor: closed %d positions in %.1fms",
                len(closed),
                self._last_check_duration_ms,
            )

        return closed
