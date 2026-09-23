"""Tests for Shadow Signal Evaluator."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Candle
from app.shadow.evaluator import ShadowSignalEvaluator


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
    repo.get_signals_without_outcomes.return_value = []
    repo.save_outcome.return_value = True
    return repo


@pytest.fixture
def evaluator(mock_client, mock_repo):
    """Create a ShadowSignalEvaluator instance."""
    return ShadowSignalEvaluator(client=mock_client, repo=mock_repo)


class TestShadowSignalEvaluator:
    """Test cases for Shadow Signal Evaluator."""

    def test_calculate_mfe_mae_short_signal(self, evaluator):
        """Test MFE/MAE calculation for SHORT signal."""
        # Create candles after signal
        candles = [
            Candle(
                timestamp=1000 + i * 300000,  # 5m intervals
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
        mfe, mae = evaluator._calculate_mfe_mae(candles, entry_price, 60)

        # MFE should be positive (price dropped)
        assert mfe is not None
        assert mfe > 0  # Price dropped, good for SHORT

        # MAE should be positive (price rose)
        assert mae is not None
        assert mae > 0  # Price rose, bad for SHORT

    def test_check_target_achievement(self, evaluator):
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

        results = evaluator._check_target_achievement(candles, signal_price)

        # Should have reached -0.5% target
        assert results["reached_minus_0_5"] is True

        # Should have reached -1.0% target
        assert results["reached_minus_1_0"] is True

        # Should not have reached -1.5% target
        assert results["reached_minus_1_5"] is False

    def test_evaluate_signal_success(self, evaluator, mock_client):
        """Test successful signal evaluation."""
        # Create signal time in the past
        signal_time = datetime.now(timezone.utc).replace(
            minute=datetime.now(timezone.utc).minute - 10
        )

        # Mock client to return candles AFTER signal time
        signal_ts = int(signal_time.timestamp() * 1000)
        mock_client.get_klines.return_value = [
            Candle(
                timestamp=signal_ts + (i + 1) * 300000,  # After signal time
                open=100.0 - (i * 0.1),
                high=100.5 - (i * 0.1),
                low=99.5 - (i * 0.1),
                close=100.0 - (i * 0.1),
                volume=1000.0,
            )
            for i in range(20)
        ]

        signal_data = {
            "signal_id": 1,
            "symbol": "TESTUSDT",
            "signal_time": signal_time,
            "signal_price": 100.0,
        }

        outcome = evaluator.evaluate_signal(signal_data)

        # Should return outcome
        assert outcome is not None
        assert "mfe_15m" in outcome
        assert "mae_15m" in outcome
        assert "reached_minus_0_5" in outcome

    def test_evaluate_pending_signals(self, evaluator, mock_repo):
        """Test evaluation of pending signals."""
        # Mock repo to return pending signals
        mock_repo.get_signals_without_outcomes.return_value = [
            {
                "signal_id": 1,
                "symbol": "TESTUSDT",
                "signal_time": datetime.now(timezone.utc),
                "signal_price": 100.0,
            }
        ]

        # Mock evaluate_signal to return outcome
        evaluator.evaluate_signal = MagicMock(return_value={"mfe_60m": 1.0})

        evaluated = evaluator.evaluate_pending_signals()

        # Should evaluate 1 signal
        assert evaluated == 1
        mock_repo.save_outcome.assert_called_once()