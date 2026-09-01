"""Tests for PositionMonitor — background thread with fast position monitoring."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.paper.position_monitor import PositionMonitor


@pytest.fixture
def settings() -> Settings:
    return Settings(
        initial_balance=10000,
        risk_per_trade=0.005,
        max_open_positions=3,
        paper_max_loss_r_per_trade=1.2,
        paper_scan_interval=300,
        paper_consecutive_loss_cooldown_minutes=5,
    )


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_open_paper_trades.return_value = []
    repo.get_latest_paper_account_snapshot.return_value = None
    repo.get_paper_risk_state.return_value = {
        "daily_loss_usdt": 0.0,
        "consecutive_losses": 0,
        "cooldown_until": None,
    }
    return repo


@pytest.fixture
def engine(settings: Settings, mock_repo: MagicMock) -> PaperTradingEngine:
    return PaperTradingEngine(settings, mock_repo)


def _make_trade(
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    entry_price: float = 100000.0,
    stop_price: float = 99500.0,
    position_size: float = 0.001,
    scanner_name: str = "TREND_PULLBACK",
) -> PaperTradeRecord:
    risk_distance = abs(entry_price - stop_price)
    risk_usdt = risk_distance * position_size
    return PaperTradeRecord(
        trade_id=1,
        setup_id="test-setup-1",
        symbol=symbol,
        scanner_name=scanner_name,
        direction=direction,
        score=75.0,
        entry_price=entry_price,
        entry_fee=entry_price * position_size * 0.00055,
        stop_price=stop_price,
        target_1=None,
        target_2=None,
        position_size=position_size,
        risk_usdt=risk_usdt,
        balance_before=10000.0,
        market_regime="TREND_UP",
        entered_at=datetime.now(timezone.utc),
        entry_market_price=entry_price,
        entry_slippage_cost=0.0,
        slippage_cost=0.0,
        funding_paid=0.0,
        funding_periods_charged=0,
    )


# ------------------------------------------------------------------
# Thread safety: trading_lock exists
# ------------------------------------------------------------------
class TestTradingLock:
    def test_engine_has_trading_lock(self, engine: PaperTradingEngine):
        assert hasattr(engine, "trading_lock")
        assert isinstance(engine.trading_lock, type(threading.Lock()))

    def test_engine_lock_is_usable(self, engine: PaperTradingEngine):
        with engine.trading_lock:
            pass


# ------------------------------------------------------------------
# Background thread lifecycle
# ------------------------------------------------------------------
class TestBackgroundThread:
    def test_start_and_stop(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
            interval_seconds=1,
        )
        assert not monitor.is_running
        monitor.start()
        time.sleep(0.2)
        assert monitor.is_running
        monitor.stop()
        assert not monitor.is_running

    def test_start_is_idempotent(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
            interval_seconds=1,
        )
        monitor.start()
        t1 = monitor._thread
        monitor.start()  # second call should be a no-op
        t2 = monitor._thread
        assert t1 is t2
        monitor.stop()

    def test_daemon_thread(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
            interval_seconds=1,
        )
        monitor.start()
        time.sleep(0.1)
        assert monitor._thread.daemon is True
        monitor.stop()


# ------------------------------------------------------------------
# Core requirement: multiple checks within 30-40s at 10s interval
# ------------------------------------------------------------------
class TestFastMonitoringFrequency:
    def test_multiple_checks_in_35_seconds(self, engine: PaperTradingEngine):
        """Verify that the background monitor performs multiple position
        checks within a 35-second window when paper_scan_interval=300.

        With interval_seconds=5 (fast for testing), we expect >=5 checks
        in 35 seconds — proving the monitor runs independently of the
        300s paper cycle.
        """
        check_times: list[float] = []

        def tracking_fetcher(symbols):
            check_times.append(time.monotonic())
            return {"BTCUSDT": 100100.0}

        trade = _make_trade(entry_price=100000.0, stop_price=99500.0)
        engine.open_trades["BTCUSDT"] = trade

        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=tracking_fetcher,
            interval_seconds=5,
        )
        monitor.start()

        time.sleep(35)

        monitor.stop()

        # With 5s interval over 35s we expect at least 5 checks
        assert len(check_times) >= 5, (
            f"Expected >= 5 checks in 35s at 5s interval, got {len(check_times)}"
        )

        # Verify the interval between checks is roughly 5s (+/- 2s tolerance)
        if len(check_times) >= 2:
            gaps = [
                check_times[i + 1] - check_times[i]
                for i in range(len(check_times) - 1)
            ]
            for gap in gaps:
                assert 3.0 <= gap <= 8.0, (
                    f"Gap between checks was {gap:.1f}s, expected ~5s"
                )

    def test_paper_scan_interval_does_not_affect_monitor(
        self, engine: PaperTradingEngine,
    ):
        """The monitor should check positions every interval_seconds
        regardless of paper_scan_interval (300s)."""
        check_times: list[float] = []

        def tracking_fetcher(symbols):
            check_times.append(time.monotonic())
            return {"BTCUSDT": 100100.0}

        trade = _make_trade()
        engine.open_trades["BTCUSDT"] = trade

        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=tracking_fetcher,
            interval_seconds=3,
        )
        monitor.start()
        time.sleep(12)
        monitor.stop()

        # 12s / 3s = ~4 checks minimum
        assert len(check_times) >= 3, (
            f"Expected >= 3 checks in 12s, got {len(check_times)}"
        )


# ------------------------------------------------------------------
# Lock acquisition during check
# ------------------------------------------------------------------
class TestLockDuringCheck:
    def test_check_acquires_lock(self, engine: PaperTradingEngine):
        """Verify the position monitor acquires engine.trading_lock."""
        lock_was_held = threading.Event()

        original_acquire = engine.trading_lock.acquire

        def tracking_acquire(*args, **kwargs):
            result = original_acquire(*args, **kwargs)
            lock_was_held.set()
            return result

        # Replace the lock with a wrapper via __class__ proxy
        class LockProxy:
            """Thin proxy that forwards all calls to the real lock."""
            def __init__(self, real_lock):
                self._real = real_lock

            def acquire(self, *a, **kw):
                result = self._real.acquire(*a, **kw)
                lock_was_held.set()
                return result

            def release(self, *a, **kw):
                return self._real.release(*a, **kw)

            def __enter__(self):
                return self._real.__enter__()

            def __exit__(self, *a):
                return self._real.__exit__(*a)

        engine.trading_lock = LockProxy(engine.trading_lock)

        trade = _make_trade()
        engine.open_trades["BTCUSDT"] = trade

        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {"BTCUSDT": 100100.0},
            interval_seconds=1,
        )
        monitor.run_once()

        assert lock_was_held.is_set()

    def test_check_releases_lock_on_exception(self, engine: PaperTradingEngine):
        """Lock must be released even if check_exits raises."""
        def boom(*a, **kw):
            raise RuntimeError("test error")

        engine.check_exits = boom

        trade = _make_trade()
        engine.open_trades["BTCUSDT"] = trade

        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {"BTCUSDT": 100000.0},
        )
        # run_once catches nothing — the background _run_loop catches exceptions.
        # But we can verify via _check_positions which has the try/finally.
        try:
            monitor._check_positions()
        except RuntimeError:
            pass

        # Lock should be free — another acquire should succeed immediately
        assert engine.trading_lock.acquire(timeout=1)
        engine.trading_lock.release()


# ------------------------------------------------------------------
# Heartbeat and diagnostics
# ------------------------------------------------------------------
class TestHeartbeat:
    def test_heartbeat_updates_in_background(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
            interval_seconds=1,
        )
        monitor.start()
        time.sleep(3)
        # Read heartbeat while running
        hb_running = monitor.heartbeat
        assert hb_running["status"] == "RUNNING"
        assert hb_running["total_checks"] >= 2
        assert hb_running["last_check"] is not None

        monitor.stop()
        # Read heartbeat after stop
        hb_stopped = monitor.heartbeat
        assert hb_stopped["status"] == "STOPPED"

    def test_diagnostics_include_engine_state(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
        )
        diag = monitor.get_diagnostics()
        assert "engine_state" in diag
        assert "open_positions" in diag["engine_state"]
        assert "gap_loss_halt" in diag["engine_state"]
