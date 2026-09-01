"""Tests for PositionMonitor — fast position monitoring with STOP_GAP diagnostics."""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.paper.position_monitor import PositionMonitor


@pytest.fixture
def settings() -> Settings:
    """Create test settings."""
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
    """Create a mock repository."""
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
    """Create a paper trading engine."""
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
    # Calculate risk_usdt from entry/stop if not provided
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
        target_1=entry_price + 2 * abs(entry_price - stop_price) if direction == "LONG" else entry_price - 2 * abs(entry_price - stop_price),
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


class TestPositionMonitorBasic:
    """Test basic PositionMonitor functionality."""

    def test_initialization(self, engine: PaperTradingEngine):
        """Test PositionMonitor can be initialized."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
            interval_seconds=10,
        )
        assert monitor.is_running is False
        assert monitor.last_check is None
        assert monitor.total_checks == 0
        assert monitor.total_closes == 0
        assert monitor.stop_gap_count_24h == 0

    def test_heartbeat_when_stopped(self, engine: PaperTradingEngine):
        """Test heartbeat when monitor is not running."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )
        heartbeat = monitor.heartbeat
        assert heartbeat["status"] == "STOPPED"
        assert heartbeat["last_check"] is None
        assert heartbeat["open_positions"] == 0

    def test_check_positions_no_open_trades(self, engine: PaperTradingEngine):
        """Test check_positions when there are no open trades."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )
        closed = monitor.check_positions()
        assert closed == []
        assert monitor.total_checks == 1

    def test_check_positions_with_open_trades(self, engine: PaperTradingEngine):
        """Test check_positions with open trades."""
        trade = _make_trade()
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(return_value={"BTCUSDT": 100500.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )
        closed = monitor.check_positions()
        assert len(closed) == 0  # Price is above entry, no exit
        assert monitor.total_checks == 1
        assert monitor.last_check is not None

    def test_check_positions_price_fetch_failure(self, engine: PaperTradingEngine):
        """Test check_positions when price fetch fails."""
        trade = _make_trade()
        engine.open_trades["BTCUSDT"] = trade

        price_fetcher = MagicMock(side_effect=Exception("API error"))
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )
        closed = monitor.check_positions()
        assert closed == []
        assert monitor.total_checks == 1


class TestStopLossGapDetection:
    """Test STOP_LOSS_GAP detection and diagnostics."""

    def test_gap_detected_long_position(self, engine: PaperTradingEngine):
        """Test STOP_LOSS_GAP detection for LONG position."""
        # Create a trade with stop at 99500
        # risk_usdt = abs(100000 - 99500) * 0.001 = 0.5
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        # Price gaps through stop to 99300 (below stop)
        price_fetcher = MagicMock(return_value={"BTCUSDT": 99300.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.check_positions()
        assert len(closed) == 1
        assert closed[0].status == "CLOSED"
        # The engine records the gap event, check engine's count
        assert engine.stop_gap_count_24h == 1
        assert engine.last_stop_gap is not None
        assert engine.last_stop_gap["symbol"] == "BTCUSDT"
        assert engine.last_stop_gap["direction"] == "LONG"
        assert engine.last_stop_gap["gap_pct"] > 0

    def test_gap_detected_short_position(self, engine: PaperTradingEngine):
        """Test STOP_LOSS_GAP detection for SHORT position."""
        # Create a SHORT trade with stop at 100500
        # risk_usdt = abs(100000 - 100500) * 0.001 = 0.5
        trade = _make_trade(
            direction="SHORT",
            entry_price=100000.0,
            stop_price=100500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        # Price gaps through stop to 100700 (above stop)
        price_fetcher = MagicMock(return_value={"BTCUSDT": 100700.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.check_positions()
        assert len(closed) == 1
        assert closed[0].status == "CLOSED"
        # The engine records the gap event
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

        # Price exactly at stop level
        price_fetcher = MagicMock(return_value={"BTCUSDT": 99500.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.check_positions()
        assert len(closed) == 1
        # Should be STOP_LOSS, not STOP_LOSS_GAP
        assert monitor.stop_gap_count_24h == 0

    def test_no_gap_when_profitable(self, engine: PaperTradingEngine):
        """Test no GAP when position is profitable."""
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
        )
        # Set target_1 higher than the price we'll check
        trade.target_1 = 102000.0
        engine.open_trades["BTCUSDT"] = trade

        # Price is above entry (profitable) but below target
        price_fetcher = MagicMock(return_value={"BTCUSDT": 101000.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        closed = monitor.check_positions()
        assert len(closed) == 0
        assert engine.stop_gap_count_24h == 0


class TestDiagnostics:
    """Test diagnostic data collection."""

    def test_gap_event_recorded_with_all_fields(self, engine: PaperTradingEngine):
        """Test that STOP_LOSS_GAP event contains all required diagnostic fields."""
        # risk_usdt = abs(100000 - 99500) * 0.001 = 0.5
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
            scanner_name="TREND_PULLBACK",
        )
        engine.open_trades["BTCUSDT"] = trade

        # Price gaps to 99200
        price_fetcher = MagicMock(return_value={"BTCUSDT": 99200.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        monitor.check_positions()

        # The engine records the gap event
        gap_event = engine.last_stop_gap
        assert gap_event is not None
        assert gap_event["symbol"] == "BTCUSDT"
        assert gap_event["scanner_name"] == "TREND_PULLBACK"
        assert gap_event["direction"] == "LONG"
        assert gap_event["entry_price"] == 100000.0
        assert gap_event["stop_price"] == 99500.0
        assert gap_event["observed_price"] == 99200.0
        assert gap_event["gap_pct"] > 0
        assert gap_event["total_r"] < 0
        assert gap_event["timestamp"] is not None

    def test_multiple_gaps_tracked(self, engine: PaperTradingEngine):
        """Test that multiple STOP_LOSS_GAP events are tracked."""
        # First trade
        trade1 = _make_trade(
            symbol="BTCUSDT",
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
        )
        engine.open_trades["BTCUSDT"] = trade1

        price_fetcher = MagicMock(return_value={"BTCUSDT": 99200.0})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        monitor.check_positions()
        # The engine records the gap event
        assert engine.stop_gap_count_24h == 1

        # Second trade
        trade2 = _make_trade(
            symbol="ETHUSDT",
            direction="LONG",
            entry_price=3000.0,
            stop_price=2985.0,
        )
        engine.open_trades["ETHUSDT"] = trade2

        price_fetcher2 = MagicMock(return_value={"ETHUSDT": 2970.0})
        monitor2 = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher2,
        )

        monitor2.check_positions()
        # Both gaps are tracked in the engine
        assert engine.stop_gap_count_24h == 2

    def test_old_events_cleaned_up(self, engine: PaperTradingEngine):
        """Test that events older than 24 hours are cleaned up."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        # Manually add an old event (25 hours ago)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        monitor._stop_gap_events.append({
            "symbol": "BTCUSDT",
            "timestamp": old_time,
        })
        monitor._stop_gap_count_24h = 1

        # Add a recent event
        recent_time = datetime.now(timezone.utc).isoformat()
        monitor._stop_gap_events.append({
            "symbol": "ETHUSDT",
            "timestamp": recent_time,
        })

        # Cleanup should remove old event
        monitor._cleanup_old_stop_gap_events()
        assert monitor.stop_gap_count_24h == 1


class TestHeartbeat:
    """Test heartbeat and monitoring status."""

    def test_heartbeat_updates_after_check(self, engine: PaperTradingEngine):
        """Test that heartbeat is updated after position check."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        monitor.check_positions()

        heartbeat = monitor.heartbeat
        assert heartbeat["status"] == "STOPPED"  # Not running continuously
        assert heartbeat["last_check"] is not None
        assert heartbeat["total_checks"] == 1
        assert heartbeat["open_positions"] == 0

    def test_diagnostics_include_engine_state(self, engine: PaperTradingEngine):
        """Test that diagnostics include engine state."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        diagnostics = monitor.get_diagnostics()
        assert "heartbeat" in diagnostics
        assert "stop_gap_events_24h" in diagnostics
        assert "engine_state" in diagnostics
        assert "open_positions" in diagnostics["engine_state"]
        assert "balance" in diagnostics["engine_state"]
        assert "gap_loss_halt" in diagnostics["engine_state"]


class TestEngineGateStatus:
    """Test enriched gate status in PaperTradingEngine."""

    def test_gate_status_default(self, engine: PaperTradingEngine):
        """Test default gate status."""
        gate = engine.gate_status
        assert gate["status"] == "OPEN"
        assert gate["reason"] is None
        assert gate["since"] is None
        assert gate["stop_gap_24h"] == 0

    def test_gate_status_blocked_after_gap(self, engine: PaperTradingEngine):
        """Test gate status changes to BLOCKED after severe gap."""
        # Create a trade that will trigger the gate
        # risk_usdt = abs(100000 - 99500) * 0.001 = 0.5
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        # Price gaps significantly (more than 1.2R loss)
        # 1.2R = 1.2 * 0.5 = 0.6 USDT loss
        # We need net_pnl < -0.6 USDT
        # gross_pnl = (exit_price - entry_price) * position_size
        # For exit_price = 99399: gross_pnl = (99399 - 100000) * 0.001 = -60.1 USDT
        # This is way more than -0.6 USDT, so it should trigger the gate
        closed = engine.check_exits({"BTCUSDT": 99399.0})

        assert len(closed) == 1
        assert engine._gap_loss_halt is True
        assert engine.gate_status["status"] == "BLOCKED"
        assert engine.gate_status["reason"] == "STOP_LOSS_GAP"
        assert engine.gate_status["since"] is not None

    def test_snapshot_includes_gate_status(self, engine: PaperTradingEngine):
        """Test that snapshot includes gate status."""
        snapshot = engine.snapshot()
        assert "gate" in snapshot
        assert "stop_gap_24h" in snapshot
        assert "last_stop_gap" in snapshot
        assert snapshot["gate"]["status"] == "OPEN"


class TestPositionMonitorIntegration:
    """Integration tests for PositionMonitor with PaperTradingEngine."""

    def test_monitor_prevents_new_entries_when_blocked(self, engine: PaperTradingEngine):
        """Test that position monitor doesn't affect entry logic."""
        # Create a trade that will trigger the gate
        # risk_usdt = abs(100000 - 99500) * 0.001 = 0.5
        trade = _make_trade(
            direction="LONG",
            entry_price=100000.0,
            stop_price=99500.0,
            position_size=0.001,
        )
        engine.open_trades["BTCUSDT"] = trade

        # Price gaps significantly (more than 1.2R loss)
        closed = engine.check_exits({"BTCUSDT": 99399.0})
        assert len(closed) == 1
        assert engine._gap_loss_halt is True

        # Verify entries are blocked
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

        # Mock the duplicate check
        engine.repo.get_paper_trade_by_setup = MagicMock(return_value=None)

        # This should be blocked by the gate
        opened = engine.check_entries([candidate], {"ETHUSDT": 3000.0})
        assert len(opened) == 0  # Blocked by gate

    def test_position_monitor_heartbeat_in_snapshot(self, engine: PaperTradingEngine):
        """Test that position monitor heartbeat is available."""
        price_fetcher = MagicMock(return_value={})
        monitor = PositionMonitor(
            engine=engine,
            price_fetcher=price_fetcher,
        )

        # Run a check
        monitor.check_positions()

        # Get diagnostics
        diagnostics = monitor.get_diagnostics()
        heartbeat = diagnostics["heartbeat"]

        assert heartbeat["total_checks"] == 1
        assert heartbeat["last_check"] is not None
        assert heartbeat["interval_seconds"] == 10
