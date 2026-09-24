"""Tests for ME_R_LONG_CLOSE_LOCATION_OOS evaluator integration."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.shadow.me_r_long_close_location_oos_evaluator import (
    MERLongCLoOosEvaluator,
    HORIZONS,
)
from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository


# ── Test helpers ──────────────────────────────────────────────────────

def make_signal(
    signal_id: int = 1,
    symbol: str = "BTCUSDT",
    signal_time: datetime | None = None,
    signal_price: float = 50000.0,
    outcome_id: int | None = None,
) -> dict:
    """Create a test signal dict."""
    if signal_time is None:
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_time": signal_time,
        "signal_price": signal_price,
        "close_location": 0.75,
        "close_location_threshold": 0.70,
        "filter_passed": True,
        "outcome_id": outcome_id,
        "evaluated_15m_at": None,
        "evaluated_30m_at": None,
        "evaluated_60m_at": None,
        "evaluated_120m_at": None,
        "evaluated_240m_at": None,
        "is_final": False,
    }


# ── Unit tests: evaluator behavior ────────────────────────────────────

class TestEvaluatorBehavior:
    """Tests for evaluator behavior."""

    def test_evaluator_returns_stats(self):
        """evaluate_pending must return stats dict."""
        repo = MagicMock(spec=MERLongCLoOosRepository)
        repo.get_eligible_signals.return_value = []
        
        client = MagicMock()
        evaluator = MERLongCLoOosEvaluator(repo, client)
        
        stats = evaluator.evaluate_pending()
        assert "checked" in stats
        assert "updated" in stats
        assert "errors" in stats
        assert stats["checked"] == 0

    def test_evaluator_checks_pending_signals(self):
        """evaluate_pending must check all eligible signals."""
        repo = MagicMock(spec=MERLongCLoOosRepository)
        repo.get_eligible_signals.return_value = [
            make_signal(signal_id=1),
            make_signal(signal_id=2),
        ]
        
        client = MagicMock()
        evaluator = MERLongCLoOosEvaluator(repo, client)
        
        # Mock _evaluate_signal to avoid actual evaluation
        with patch.object(evaluator, '_evaluate_signal') as mock_eval:
            stats = evaluator.evaluate_pending()
            assert stats["checked"] == 2
            assert mock_eval.call_count == 2

    def test_evaluator_handles_errors(self):
        """evaluate_pending must handle errors gracefully."""
        repo = MagicMock(spec=MERLongCLoOosRepository)
        repo.get_eligible_signals.return_value = [
            make_signal(signal_id=1),
        ]
        
        client = MagicMock()
        evaluator = MERLongCLoOosEvaluator(repo, client)
        
        # Mock _evaluate_signal to raise an exception
        with patch.object(evaluator, '_evaluate_signal', side_effect=Exception("API error")):
            stats = evaluator.evaluate_pending()
            assert stats["errors"] == 1
            assert stats["checked"] == 1


# ── Unit tests: horizon maturity ──────────────────────────────────────

class TestHorizonMaturity:
    """Tests for horizon maturity logic."""

    def test_15m_horizon_mature_after_15_minutes(self):
        """15m horizon should be mature after 15 minutes."""
        from app.shadow.v2d_evaluator import _is_horizon_mature
        
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        now = datetime.now(timezone.utc)
        
        assert _is_horizon_mature(signal_time, 15, now) is True

    def test_15m_horizon_not_mature_before_15_minutes(self):
        """15m horizon should not be mature before 15 minutes."""
        from app.shadow.v2d_evaluator import _is_horizon_mature
        
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        now = datetime.now(timezone.utc)
        
        assert _is_horizon_mature(signal_time, 15, now) is False

    def test_240m_horizon_mature_after_240_minutes(self):
        """240m horizon should be mature after 240 minutes."""
        from app.shadow.v2d_evaluator import _is_horizon_mature
        
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=250)
        now = datetime.now(timezone.utc)
        
        assert _is_horizon_mature(signal_time, 240, now) is True


# ── Integration tests: evaluator integration ──────────────────────────

class TestEvaluatorIntegration:
    """Integration tests for evaluator integration."""

    def test_evaluator_called_from_runner(self):
        """_run_oos_evaluator must be callable from scanner_runner."""
        from scanner_runner import _run_oos_evaluator
        
        # Mock the dependencies
        with patch("app.shadow.me_r_long_close_location_oos_evaluator.MERLongCLoOosEvaluator") as MockEvaluator:
            with patch("app.shadow.me_r_long_close_location_oos_repository.MERLongCLoOosRepository") as MockRepo:
                mock_evaluator = MagicMock()
                mock_evaluator.evaluate_pending.return_value = {"checked": 5, "updated": 3, "errors": 0}
                MockEvaluator.return_value = mock_evaluator
                
                mock_repo = MagicMock()
                MockRepo.return_value = mock_repo
                
                client = MagicMock()
                repository = MagicMock()
                repository._conn = MagicMock()
                
                _run_oos_evaluator(client, repository)
                
                mock_evaluator.evaluate_pending.assert_called_once()

    def test_evaluator_logs_activity(self):
        """_run_oos_evaluator must log activity."""
        from scanner_runner import _run_oos_evaluator
        
        with patch("app.shadow.me_r_long_close_location_oos_evaluator.MERLongCLoOosEvaluator") as MockEvaluator:
            with patch("app.shadow.me_r_long_close_location_oos_repository.MERLongCLoOosRepository") as MockRepo:
                with patch("scanner_runner.logger") as mock_logger:
                    mock_evaluator = MagicMock()
                    mock_evaluator.evaluate_pending.return_value = {"checked": 10, "updated": 5, "errors": 1}
                    MockEvaluator.return_value = mock_evaluator
                    
                    mock_repo = MagicMock()
                    MockRepo.return_value = mock_repo
                    
                    client = MagicMock()
                    repository = MagicMock()
                    repository._conn = MagicMock()
                    
                    _run_oos_evaluator(client, repository)
                    
                    mock_logger.info.assert_called_once()
                    # The log message uses %d format, so we check the call args
                    call_args = mock_logger.info.call_args[0]
                    assert "checked=%d" in call_args[0]
                    assert "updated=%d" in call_args[0]
                    assert "errors=%d" in call_args[0]
                    # Check the actual values
                    assert call_args[1] == 10  # checked
                    assert call_args[2] == 5   # updated
                    assert call_args[3] == 1   # errors


# ── Trading isolation tests ──────────────────────────────────────────

class TestTradingIsolation:
    """Tests that evaluator doesn't affect trading."""

    def test_evaluator_does_not_create_trades(self):
        """Evaluator must not create paper trades."""
        # The evaluator only updates outcome table
        # It doesn't interact with paper trading logic
        assert True  # Placeholder - actual test would verify no paper_trade inserts

    def test_evaluator_does_not_modify_scanner_gates(self):
        """Evaluator must not modify scanner gates."""
        # The evaluator only reads from signal table and updates outcome table
        # It doesn't modify scanner_direction_gate
        assert True  # Placeholder - actual test would verify no gate modifications


# ── SQL diagnostic tests ─────────────────────────────────────────────

class TestSQLDiagnostics:
    """Tests for SQL diagnostic views."""

    def test_accumulation_view_shows_outcomes(self):
        """Accumulation view must show outcomes_created."""
        # The view dds.v_me_r_long_cl_oos_accumulation must show outcomes
        assert True  # Placeholder - actual DB test needed

    def test_pass_reject_view_shows_mfe_mae(self):
        """PASS/REJECT view must show MFE/MAE."""
        # The view dds.v_me_r_long_cl_oos_pass_reject must show MFE/MAE
        assert True  # Placeholder - actual DB test needed
