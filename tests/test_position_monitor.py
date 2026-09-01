"""Tests for PositionMonitor — background thread with fast position monitoring."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

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
    repo.get_paper_trade_by_setup.return_value = None
    repo.save_paper_trade.return_value = 1
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
    risk_usdt: float | None = None,
    scanner_name: str = "TREND_PULLBACK",
    entered_at: datetime | None = None,
) -> PaperTradeRecord:
    """Create a test trade record."""
    if risk_usdt is None:
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
        entered_at=entered_at or datetime.now(timezone.utc),
        entry_market_price=entry_price,
        entry_slippage_cost=0.0,
        slippage_cost=0.0,
        funding_paid=0.0,
        funding_periods_charged=0,
    )


# ==================================================================
# Thread safety
# ==================================================================
class TestTradingLock:
    def test_engine_has_trading_lock(self, engine: PaperTradingEngine):
        assert hasattr(engine, "trading_lock")
        assert isinstance(engine.trading_lock, type(threading.Lock()))

    def test_engine_lock_is_usable(self, engine: PaperTradingEngine):
        with engine.trading_lock:
            pass


# ==================================================================
# Background thread lifecycle
# ==================================================================
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
        monitor.start()
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


# ==================================================================
# Core: multiple checks within 30-40s at 10s interval
# ==================================================================
class TestFastMonitoringFrequency:
    def test_multiple_checks_in_35_seconds(self, engine: PaperTradingEngine):
        """Verify that the background monitor performs multiple position
        checks within a 35-second window when paper_scan_interval=300.

        With interval_seconds=5 (fast for testing), we expect >=5 checks
        in 35 seconds -- proving the monitor runs independently of the
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

        assert len(check_times) >= 5, (
            f"Expected >= 5 checks in 35s at 5s interval, got {len(check_times)}"
        )

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

        assert len(check_times) >= 3, (
            f"Expected >= 3 checks in 12s, got {len(check_times)}"
        )


# ==================================================================
# Lock acquisition during check
# ==================================================================
class TestLockDuringCheck:
    def test_check_acquires_lock(self, engine: PaperTradingEngine):
        """Verify the position monitor acquires engine.trading_lock."""
        lock_was_held = threading.Event()
        original_acquire = engine.trading_lock.acquire

        def tracking_acquire(*args, **kwargs):
            result = original_acquire(*args, **kwargs)
            lock_was_held.set()
            return result

        class LockProxy:
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
        try:
            monitor._check_positions()
        except RuntimeError:
            pass

        assert engine.trading_lock.acquire(timeout=1)
        engine.trading_lock.release()


# ==================================================================
# Heartbeat and diagnostics
# ==================================================================
class TestHeartbeat:
    def test_heartbeat_updates_in_background(self, engine: PaperTradingEngine):
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=lambda s: {},
            interval_seconds=1,
        )
        monitor.start()
        time.sleep(3)
        hb_running = monitor.heartbeat
        assert hb_running["status"] == "RUNNING"
        assert hb_running["total_checks"] >= 2
        assert hb_running["last_check"] is not None

        monitor.stop()
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

    def test_heartbeat_updates_after_run_once(self, engine: PaperTradingEngine):
        """Test that heartbeat is updated after run_once."""
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=MagicMock(return_value={}),
        )
        monitor.run_once()
        heartbeat = monitor.heartbeat
        assert heartbeat["last_check"] is not None
        assert heartbeat["total_checks"] == 1
        assert heartbeat["open_positions"] == 0


# ==================================================================
# STOP_LOSS_GAP detection (from main PR #31)
# ==================================================================
class TestStopLossGapDetection:
    def test_gap_detected_long_position(self, engine: PaperTradingEngine):
        """Test STOP_LOSS_GAP detection for LONG position."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(return_value={"BTCUSDT": 99300.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.run_once()
        assert len(closed) == 1
        assert closed[0].status == "CLOSED"
        assert engine.stop_gap_count_24h == 1
        assert engine.last_stop_gap is not None
        assert engine.last_stop_gap["symbol"] == "BTCUSDT"
        assert engine.last_stop_gap["direction"] == "LONG"
        assert engine.last_stop_gap["gap_pct"] > 0

        diagnostics = monitor.get_diagnostics()
        assert diagnostics["heartbeat"]["stop_gap_24h"] == 1
        assert diagnostics["heartbeat"]["last_stop_gap"] == engine.last_stop_gap
        assert diagnostics["stop_gap_events_24h"] == [engine.last_stop_gap]

    def test_gap_detected_short_position(self, engine: PaperTradingEngine):
        """Test STOP_LOSS_GAP detection for SHORT position."""
        trade = _make_trade(
            direction="SHORT",
            entry_price=100000.0,
            stop_price=100500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(return_value={"BTCUSDT": 100700.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.run_once()
        assert len(closed) == 1
        assert engine.stop_gap_count_24h == 1
        assert engine.last_stop_gap["direction"] == "SHORT"

    def test_no_gap_when_price_at_stop(self, engine: PaperTradingEngine):
        """Test no GAP when price exactly hits stop level."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
        )
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(return_value={"BTCUSDT": 99500.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.run_once()
        assert len(closed) == 1
        assert engine.stop_gap_count_24h == 0

    def test_no_gap_when_profitable(self, engine: PaperTradingEngine):
        """Test no GAP when position is profitable."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
        )
        trade.target_1 = 102000.0
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(return_value={"BTCUSDT": 101000.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.run_once()
        assert len(closed) == 0
        assert engine.stop_gap_count_24h == 0


# ==================================================================
# Engine gate status
# ==================================================================
class TestEngineGateStatus:
    def test_gate_status_default(self, engine: PaperTradingEngine):
        gate = engine.gate_status
        assert gate["status"] == "OPEN"
        assert gate["reason"] is None
        assert gate["since"] is None
        assert gate["stop_gap_24h"] == 0

    def test_gate_status_blocked_after_gap(self, engine: PaperTradingEngine):
        """Test gate status changes to BLOCKED after severe gap."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        closed = engine.check_exits({"BTCUSDT": 99399.0})
        assert len(closed) == 1
        assert engine._gap_loss_halt is True
        assert engine.gate_status["status"] == "BLOCKED"
        assert engine.gate_status["reason"] == "STOP_LOSS_GAP"
        assert engine.gate_status["since"] is not None

    def test_snapshot_includes_gate_status(self, engine: PaperTradingEngine):
        snapshot = engine.snapshot()
        assert "gate" in snapshot
        assert "stop_gap_24h" in snapshot
        assert "last_stop_gap" in snapshot
        assert snapshot["gate"]["status"] == "OPEN"


# ==================================================================
# Integration: monitor blocks entries when gate is triggered
# ==================================================================
class TestPositionMonitorIntegration:
    def test_monitor_prevents_new_entries_when_blocked(self, engine: PaperTradingEngine):
        """Test that entries are blocked when gate triggers."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        closed = engine.check_exits({"BTCUSDT": 99399.0})
        assert len(closed) == 1
        assert engine._gap_loss_halt is True

        from app.scanners.models import SetupCandidate
        candidate = SetupCandidate(
            setup_id="test-setup-2",
            scanner_name="TREND_PULLBACK",
            symbol="ETHUSDT",
            direction="LONG",
            score=75.0,
            entry_zone_low=2990.0,
            entry_zone_high=3010.0,
            invalidation_price=2985.0,
            target_1=3050.0,
            target_2=None,
            market_regime="TREND_UP",
            reference_price=3000.0,
        )

        engine.repo.get_paper_trade_by_setup = MagicMock(return_value=None)
        opened = engine.check_entries([candidate], {"ETHUSDT": 3000.0})
        assert len(opened) == 0  # Blocked by gate

    def test_diagnostics_full_flow(self, engine: PaperTradingEngine):
        """Test diagnostics include engine state after gap."""
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=MagicMock(return_value={}),
        )

        diagnostics = monitor.get_diagnostics()
        assert "heartbeat" in diagnostics
        assert "stop_gap_events_24h" in diagnostics
        assert "engine_state" in diagnostics
        assert "open_positions" in diagnostics["engine_state"]
        assert "balance" in diagnostics["engine_state"]
        assert "gap_loss_halt" in diagnostics["engine_state"]
