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

    def test_get_eligible_signals(self, repo, mock_conn):
        """Test getting eligible signals for evaluation."""
        mock_cursor = mock_conn.cursor.return_value
        now = datetime.now(timezone.utc)
        mock_cursor.fetchall.return_value = [
            (
                1, "TESTUSDT", now, 100.0,
                None,  # outcome_id (new signal)
                None, None, None, None, None, None,  # evaluated_*_at
                False,  # is_final
            )
        ]

        signals = repo.get_eligible_signals()

        assert len(signals) == 1
        assert signals[0]["symbol"] == "TESTUSDT"
        assert signals[0]["signal_price"] == 100.0
        assert signals[0]["outcome_id"] is None
        assert signals[0]["is_final"] is False

    def test_save_outcome_success(self, repo, mock_conn):
        """Test successful outcome saving via legacy wrapper."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome(
            signal_id=1,
            mfe_60m=1.5,
            mae_60m=0.3,
            reached_minus_0_5=True,
            reached_minus_1_0=True,
        )

        assert success is True
        mock_conn.commit.assert_called()

    def test_save_outcome_partial_new_row_has_horizons(self, repo, mock_conn):
        """RC-fix: INSERT for new outcome must include horizon columns."""
        mock_cursor = mock_conn.cursor.return_value
        now = datetime.now(timezone.utc)

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=0.5, mae_15m=0.2, evaluated_15m_at=now,
            mfe_30m=0.8, mae_30m=0.3, evaluated_30m_at=now,
        )

        assert success is True
        # Verify the SQL contains dynamic columns in INSERT
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        assert "mfe_15m" in sql
        assert "evaluated_15m_at" in sql
        assert "mfe_30m" in sql
        # Verify EXCLUDED.* pattern used in ON CONFLICT UPDATE
        assert "EXCLUDED.mfe_15m" in sql
        assert "EXCLUDED.mfe_30m" in sql

    def test_save_outcome_partial_zero_values(self, repo, mock_conn):
        """mfe=0.0 and mae=0.0 are valid values, not None."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=0.0,
            mae_15m=0.0,
            mfe_60m=0.0,
        )

        assert success is True
        call_args = mock_cursor.execute.call_args
        # 0.0 values should appear in the params list
        params = call_args[0][1]
        assert 0.0 in params

    def test_save_outcome_partial_false_flag(self, repo, mock_conn):
        """reached_minus_0_5=False is a valid value."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            reached_minus_0_5=False,
        )

        assert success is True
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        assert "reached_minus_0_5" in sql
        params = call_args[0][1]
        assert False in params

    def test_save_outcome_partial_none_fields_not_written(self, repo, mock_conn):
        """None fields are not included in the SQL."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=0.5,
            mae_15m=0.2,
            evaluated_15m_at=datetime.now(timezone.utc),
            # All other fields left as None
        )

        assert success is True
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        # 15m fields should be present
        assert "mfe_15m" in sql
        # 120m fields should NOT be present (None = don't touch)
        assert "mfe_120m" not in sql
        assert "mfe_240m" not in sql
        assert "mfe_eod" not in sql

    def test_save_outcome_partial_is_final_true_writes(self, repo, mock_conn):
        """is_final=True is written to both INSERT and UPDATE."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=1.0,
            is_final=True,
        )

        assert success is True
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "is_final" in sql
        assert True in params

    def test_save_outcome_partial_is_final_false_not_written(self, repo, mock_conn):
        """is_final=False does NOT appear in SQL (never resets TRUE)."""
        mock_cursor = mock_conn.cursor.return_value

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=1.0,
            is_final=False,
        )

        assert success is True
        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        # is_final should not be in the INSERT columns
        # because False is not written (prevents reset of TRUE→FALSE)
        assert "is_final" not in sql

    def test_save_outcome_partial_no_columns_returns_true(self, repo, mock_conn):
        """When no non-None fields, returns True without executing SQL."""
        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
        )
        assert success is True
        mock_conn.cursor.assert_not_called()

    def test_save_outcome_partial_db_error_returns_false(self, repo, mock_conn):
        """Database errors cause rollback and return False."""
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.execute.side_effect = Exception("DB error")

        success = repo.save_outcome_partial(
            signal_id=1,
            symbol="TESTUSDT",
            mfe_15m=1.0,
        )

        assert success is False
        mock_conn.rollback.assert_called()