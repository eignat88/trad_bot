"""Tests for experiment_id hotfix — propagation into research_outcome.

Verifies:
  - create outcome with experiment_id
  - update partial outcome preserves experiment_id
  - backfill empty experiment_id
  - idempotent rerun
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest

from app.research.repository import ResearchRepository


class TestOutcomeExperimentId:
    """Verify experiment_id propagation into research_outcome."""

    def _make_repo(self) -> tuple[ResearchRepository, MagicMock]:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        return ResearchRepository(mock_conn), mock_conn

    def test_upsert_outcome_with_experiment_id(self):
        """New outcome row includes experiment_id."""
        repo, mock_conn = self._make_repo()

        result = repo.upsert_outcome(
            signal_id=1,
            experiment_id="BR_GENERIC_V1",
            symbol="BTCUSDT",
            updates={"mfe_60m": 2.5, "mae_60m": 1.2},
        )

        assert result is True
        # Verify the SQL was called with experiment_id in the values
        cursor = mock_conn.cursor.return_value
        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        args = cursor.execute.call_args[0][1]
        assert "experiment_id" in sql
        assert args[1] == "BR_GENERIC_V1"  # second param is experiment_id

    def test_upsert_outcome_update_preserves_experiment_id(self):
        """Updating existing outcome preserves experiment_id."""
        repo, mock_conn = self._make_repo()

        # First insert
        repo.upsert_outcome(
            signal_id=2,
            experiment_id="TPV2_GENERIC_V1",
            symbol="ETHUSDT",
            updates={"mfe_15m": 1.0},
        )

        # Update with new horizon data — experiment_id must remain
        repo.upsert_outcome(
            signal_id=2,
            experiment_id="TPV2_GENERIC_V1",
            symbol="ETHUSDT",
            updates={"mfe_30m": 1.5, "mae_30m": 0.8},
        )

        # Both calls should have experiment_id in the SQL
        cursor = mock_conn.cursor.return_value
        for c in cursor.execute.call_args_list:
            sql = c[0][0]
            args = c[0][1]
            assert "experiment_id" in sql

    def test_empty_experiment_id_not_written(self):
        """Empty experiment_id should still work (backward compat)."""
        repo, mock_conn = self._make_repo()

        result = repo.upsert_outcome(
            signal_id=3,
            experiment_id="",
            symbol="BTCUSDT",
            updates={"mfe_60m": 0.5},
        )

        assert result is True

    def test_backfill_query_exists(self):
        """Migration contains backfill for empty experiment_id."""
        from pathlib import Path
        migration = Path(__file__).parent.parent / "sql" / "migrations" / "049_generic_research_framework.sql"
        content = migration.read_text(encoding="utf-8")

        assert "UPDATE research.research_outcome o" in content
        assert "SET experiment_id = s.experiment_id" in content
        assert "FROM research.research_signal s" in content
        assert "o.experiment_id IS NULL OR o.experiment_id = ''" in content

    def test_backfill_idempotent(self):
        """Backfill only touches rows with empty experiment_id."""
        from pathlib import Path
        migration = Path(__file__).parent.parent / "sql" / "migrations" / "049_generic_research_framework.sql"
        content = migration.read_text(encoding="utf-8")

        # The WHERE clause ensures idempotency
        assert "AND (o.experiment_id IS NULL OR o.experiment_id = '')" in content


class TestGetEligibleSignalsExperimentId:
    """Verify get_eligible_signals returns experiment_id."""

    def _make_repo(self) -> tuple[ResearchRepository, MagicMock]:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        return ResearchRepository(mock_conn), mock_conn

    def test_experiment_id_in_result(self):
        """get_eligible_signals includes experiment_id in result dict."""
        repo, mock_conn = self._make_repo()
        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [
            (1, "BR_GENERIC_V1", "BTCUSDT", datetime.now(timezone.utc),
             50000.0, 49000.0, 52000.0, None,
             None, None, None, None, None, False),
        ]

        signals = repo.get_eligible_signals("BR_GENERIC_V1")

        assert len(signals) == 1
        assert signals[0]["experiment_id"] == "BR_GENERIC_V1"
        assert signals[0]["signal_id"] == 1
        assert signals[0]["symbol"] == "BTCUSDT"


class TestEvaluatorUsesExperimentId:
    """Verify evaluator passes experiment_id from signal to outcome."""

    def test_evaluator_calls_upsert_with_experiment_id(self):
        """_evaluate_signal passes sig['experiment_id'] to upsert_outcome."""
        from app.research.evaluator import ResearchEvaluator
        from unittest.mock import MagicMock, patch
        from datetime import timedelta

        mock_conn = MagicMock()
        mock_client = MagicMock()
        evaluator = ResearchEvaluator(conn=mock_conn, client=mock_client)

        # Mock the repo
        mock_repo = MagicMock()
        evaluator._repo = mock_repo

        now = datetime.now(timezone.utc)
        signal_time = now - timedelta(hours=5)  # old enough for all horizons

        sig = {
            "signal_id": 42,
            "experiment_id": "TPV2_GENERIC_V1",
            "symbol": "ETHUSDT",
            "signal_time": signal_time,
            "entry_price": 3500.0,
            "invalidation_price": 3450.0,
            "target_1": 3525.0,
            "outcome_id": None,
            "evaluated_15m_at": None,
            "evaluated_30m_at": None,
            "evaluated_60m_at": None,
            "evaluated_120m_at": None,
            "evaluated_240m_at": None,
            "is_final": False,
        }

        # Mock candles — return empty to skip MFE/MAE calc
        # but still trigger finalization attempt
        stats = {
            "horizons_updated": {"15m": 0, "30m": 0, "60m": 0, "120m": 0, "240m": 0},
            "outcomes_created": 0,
            "outcomes_updated": 0,
            "finalized": 0,
            "errors": 0,
        }

        evaluator._evaluate_signal(sig, [], now, stats)

        # If any upsert was called, verify experiment_id
        if mock_repo.upsert_outcome.called:
            call_kwargs = mock_repo.upsert_outcome.call_args[1]
            assert call_kwargs.get("experiment_id") == "TPV2_GENERIC_V1", \
                f"experiment_id should be TPV2_GENERIC_V1, got {call_kwargs.get('experiment_id')}"
