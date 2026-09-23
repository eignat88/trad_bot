"""Tests for Shadow Signal Evaluator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Candle
from app.shadow.evaluator import (
    ShadowSignalEvaluator,
    _calculate_mfe_mae_for_window,
    _check_target_achievement,
    _is_horizon_mature,
    _is_eod_mature,
)


@pytest.fixture
def mock_client():
    """Create a mock BybitClient."""
    client = MagicMock()
    client.get_klines.return_value = []
    return client


@pytest.fixture
def mock_repo():
    """Create a mock ShadowSignalRepository."""
    repo = MagicMock()
    repo.get_eligible_signals.return_value = []
    repo.save_outcome_partial.return_value = True
    return repo


@pytest.fixture
def evaluator(mock_client, mock_repo):
    """Create a ShadowSignalEvaluator instance."""
    return ShadowSignalEvaluator(client=mock_client, repo=mock_repo)


class TestShadowSignalEvaluator:
    """Test cases for Shadow Signal Evaluator."""

    def test_calculate_mfe_mae_short_signal(self):
        """Test MFE/MAE calculation for SHORT signal."""
        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)

        # Create candles after signal
        candles = [
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0,
                high=100.5 + (i * 0.1),  # Price rising slightly
                low=99.5 - (i * 0.2),    # Price dropping more
                close=100.0 - (i * 0.1),
                volume=1000.0,
            )
            for i in range(12)  # 12 candles = 60 minutes
        ]

        entry_price = 100.0

        # Calculate MFE/MAE for 60m horizon
        mfe, mae = _calculate_mfe_mae_for_window(candles, entry_price, 60, signal_time)

        # MFE should be positive (price dropped)
        assert mfe is not None
        assert mfe > 0  # Price dropped, good for SHORT

        # MAE should be positive (price rose)
        assert mae is not None
        assert mae > 0  # Price rose, bad for SHORT

    def test_check_target_achievement(self):
        """Test target achievement checking."""
        # Create candles that hit specific targets
        candles = [
            Candle(
                timestamp=1000,
                open=100.0,
                high=100.5,
                low=99.4,  # Drops 0.6% from 100.0
                close=99.5,
                volume=1000.0,
            ),
            Candle(
                timestamp=1000 + 300000,
                open=99.5,
                high=99.8,
                low=98.9,  # Drops 1.1% from 100.0
                close=99.0,
                volume=1000.0,
            ),
        ]

        signal_price = 100.0

        results = _check_target_achievement(candles, signal_price)

        # Should have reached -0.5% target
        assert results["reached_minus_0_5"] is True

        # Should have reached -1.0% target
        assert results["reached_minus_1_0"] is True

        # Should not have reached -1.5% target
        assert results["reached_minus_1_5"] is False

    def test_evaluate_signal_incremental(self, evaluator, mock_client):
        """Test incremental signal evaluation via run_evaluation_cycle."""
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=20)

        # Mock repo to return eligible signal
        mock_repo = evaluator.repo
        mock_repo.get_eligible_signals.return_value = [
            {
                "signal_id": 1,
                "symbol": "TESTUSDT",
                "signal_time": signal_time,
                "signal_price": 100.0,
                "outcome_id": None,
                "evaluated_15m_at": None,
                "evaluated_30m_at": None,
                "evaluated_60m_at": None,
                "evaluated_120m_at": None,
                "evaluated_240m_at": None,
                "evaluated_eod_at": None,
                "is_final": False,
            }
        ]

        # Mock client to return candles AFTER signal time
        signal_ts = int(signal_time.timestamp() * 1000)
        mock_client.get_klines.return_value = [
            Candle(
                timestamp=signal_ts + (i + 1) * 300000,
                open=100.0 - (i * 0.1),
                high=100.5 - (i * 0.1),
                low=99.5 - (i * 0.1),
                close=100.0 - (i * 0.1),
                volume=1000.0,
            )
            for i in range(20)
        ]

        summary = evaluator.run_evaluation_cycle()

        # Should have created an outcome with 15m horizon
        assert summary["signals_checked"] == 1
        assert summary["outcomes_created"] == 1
        assert summary["horizons_updated"]["15m"] >= 1

    def test_run_evaluation_cycle_returns_correct_keys(self, evaluator, mock_repo):
        """Test that run_evaluation_cycle returns correct stat keys."""
        mock_repo.get_eligible_signals.return_value = []
        summary = evaluator.run_evaluation_cycle()

        assert "start_time" in summary
        assert "end_time" in summary
        assert "signals_checked" in summary
        assert "symbols_processed" in summary
        assert "outcomes_created" in summary
        assert "outcomes_updated" in summary
        assert "finalized" in summary
        assert "errors" in summary
        assert "horizons_updated" in summary
        assert summary["signals_checked"] == 0
