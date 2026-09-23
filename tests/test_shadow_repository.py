"""Tests for Shadow Signal Repository."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.scanners.atr_wick_rejection_short import WickRejectionSignal
from app.shadow.repository import ShadowSignalRepository


@pytest.fixture
def mock_conn():
    """Create a mock database connection."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def repo(mock_conn):
    """Create a ShadowSignalRepository instance."""
    return ShadowSignalRepository(conn=mock_conn)


class TestShadowSignalRepository:
    """Test cases for Shadow Signal Repository."""

    def test_save_signal_success(self, repo, mock_conn):
        """Test successful signal saving."""
        # Create a sample signal
        signal = WickRejectionSignal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0,
            high=101.5,
            low=99.8,
            close=100.1,
            volume=1500.0,
            atr=0.5,
            atr_pct=0.005,
            wick_size=1.4,
            wick_atr=2.8,
            upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0,
            stoch_rsi=0.7,
            bb_upper=102.0,
            bb_mid=100.0,
            bb_lower=98.0,
            bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0,
            ema_medium=99.0,
            ema_slow=98.0,
            ema_slope=-0.0002,
            volume_ratio=1.5,
            signal_version="1.0.0",
        )

        # Mock cursor to return a signal_id
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = (1,)

        # Save signal
        signal_id = repo.save_signal(signal)

        # Should return signal_id
        assert signal_id == 1
        mock_conn.commit.assert_called_once()

    def test_signal_exists_true(self, repo, mock_conn):
        """Test signal_exists returns True when signal exists."""
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = (1,)

        exists = repo.signal_exists("TESTUSDT", datetime.now(timezone.utc))

        assert exists is True

    def test_signal_exists_false(self, repo, mock_conn):
        """Test signal_exists returns False when signal doesn't exist."""
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = None

        exists = repo.signal_exists("TESTUSDT", datetime.now(timezone.utc))

        assert exists is False

    def test_get_signals_without_outcomes(self, repo, mock_conn):
        """Test getting signals without outcomes."""
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchall.return_value = [
            (
                1, "TESTUSDT", datetime.now(timezone.utc), 100.0,
                100.0, 101.5, 99.8, 100.1, 1500.0,
                0.5, 0.005,
                1.4, 2.8, 0.014, 0.06,
                65.0, 0.7,
                102.0, 100.0, 98.0, 0.04, 0.019,
                100.0, 99.0, 98.0, -0.0002,
                1.5, "1.0.0",
            )
        ]

        signals = repo.get_signals_without_outcomes()

        assert len(signals) == 1
        assert signals[0]["symbol"] == "TESTUSDT"
        assert signals[0]["signal_price"] == 100.0

    def test_save_outcome_success(self, repo, mock_conn):
        """Test successful outcome saving."""
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.fetchone.return_value = ("TESTUSDT",)

        success = repo.save_outcome(
            signal_id=1,
            mfe_60m=1.5,
            mae_60m=0.3,
            reached_minus_0_5=True,
            reached_minus_1_0=True,
        )

        assert success is True
        mock_conn.commit.assert_called_once()