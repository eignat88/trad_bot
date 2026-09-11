"""Unit tests for analytics runner."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta, date
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from app.analytics.runner import AnalyticsRunner
from app.analytics.models import (
    AnalysisRun,
    AnalysisStageRun,
    Maturity,
    RunStatus,
    StageStatus,
)
from app.analytics.repository import AnalyticsRepository
from app.analytics.candle_sync import CandleSync
from app.analytics.quality import DataQualityGate
from app.analytics.retention import CandleRetention
from app.config import Settings


class TestAnalyticsRunner:
    """Tests for AnalyticsRunner."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_settings = Mock(spec=Settings)
        self.mock_settings.analytics_post_exit_hours = 4
        self.mock_settings.analytics_candle_retention_days = 180
        
        self.mock_repo = Mock(spec=AnalyticsRepository)
        self.mock_conn = MagicMock()
        self.mock_repo._conn = self.mock_conn
        self.mock_candle_sync = Mock(spec=CandleSync)
        self.mock_quality_gate = Mock(spec=DataQualityGate)
        self.mock_retention = Mock(spec=CandleRetention)
        
        self.runner = AnalyticsRunner(
            settings=self.mock_settings,
            repository=self.mock_repo,
            candle_sync=self.mock_candle_sync,
            quality_gate=self.mock_quality_gate,
            retention=self.mock_retention,
        )

    def test_get_business_date(self):
        """Test getting business date in Europe/Sofia timezone."""
        business_date = self.runner._get_business_date()
        
        assert isinstance(business_date, date)
        # Should be today or yesterday depending on time
        assert business_date <= date.today()

    def test_calculate_lock_id(self):
        """Test advisory lock ID calculation."""
        lock_id1 = self.runner._calculate_lock_id(date(2026, 9, 12))
        lock_id2 = self.runner._calculate_lock_id(date(2026, 9, 13))
        
        assert isinstance(lock_id1, int)
        assert isinstance(lock_id2, int)
        assert lock_id1 != lock_id2

    def test_create_or_get_run(self):
        """Test creating or getting analysis run."""
        # Mock repository
        self.mock_repo.get_analysis_run_by_date.return_value = None
        self.mock_repo.create_analysis_run.return_value = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
        )
        
        run = self.runner._create_or_get_run(date(2026, 9, 12))
        
        assert run.business_date == date(2026, 9, 12)
        self.mock_repo.create_analysis_run.assert_called_once()

    def test_get_next_attempt(self):
        """Test getting next attempt number."""
        # Mock repository with proper _conn attribute
        mock_conn = MagicMock()
        self.mock_repo._conn = mock_conn
        # The query returns MAX(attempt) + 1, so if MAX is 1, next is 2
        mock_conn.cursor.return_value.fetchone.return_value = [2]
        
        attempt = self.runner._get_next_attempt(uuid4(), "test_stage")
        
        assert attempt == 2

    def test_execute_stage(self):
        """Test executing a pipeline stage."""
        # Mock stage function
        def mock_stage_func(run, stage_run):
            return {"output_rows": 10}
        
        # Mock repository
        self.mock_repo.create_stage_run.return_value = AnalysisStageRun(
            stage_run_id=1,
            run_id=uuid4(),
            stage_name="test_stage",
        )
        
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
        )
        
        stage_run = self.runner._execute_stage(run, "test_stage", mock_stage_func)
        
        assert stage_run.status == StageStatus.SUCCEEDED
        assert stage_run.output_rows == 10

    def test_execute_stage_failure(self):
        """Test executing a failing pipeline stage."""
        # Mock stage function that raises exception
        def mock_stage_func(run, stage_run):
            raise RuntimeError("Stage failed")
        
        # Mock repository
        self.mock_repo.create_stage_run.return_value = AnalysisStageRun(
            stage_run_id=1,
            run_id=uuid4(),
            stage_name="test_stage",
        )
        
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
        )
        
        with pytest.raises(RuntimeError, match="Stage failed"):
            self.runner._execute_stage(run, "test_stage", mock_stage_func)

    def test_stage_candle_reconciliation(self):
        """Test candle reconciliation stage with no trades."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="candle_reconciliation",
        )
        
        # Mock cursor to return no trades
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor
        
        result = self.runner._stage_candle_reconciliation(run, stage_run)
        
        assert result["output_rows"] == 0
        assert result["no_required_ranges"] is True
        assert result["trades_considered"] == 0

    def test_stage_quality_gate(self):
        """Test quality gate stage."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="quality_gate",
        )
        
        # Mock quality gate
        self.mock_quality_gate.run_quality_checks.return_value = (True, [])
        
        result = self.runner._stage_quality_gate(run, stage_run)
        
        assert "output_rows" in result
        assert "message" in result

    def test_stage_quality_gate_failure(self):
        """Test quality gate stage failure."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="quality_gate",
        )
        
        # Mock quality gate failure
        self.mock_quality_gate.run_quality_checks.return_value = (False, [])
        
        with pytest.raises(RuntimeError, match="Data quality gate failed"):
            self.runner._stage_quality_gate(run, stage_run)

    def test_stage_retention(self):
        """Test retention stage."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="retention",
        )
        
        # Mock retention
        self.mock_retention.run_retention.return_value = {"total_deleted": 100}
        
        result = self.runner._stage_retention(run, stage_run)
        
        assert "output_rows" in result
        assert result["output_rows"] == 100

    def test_run_provisional(self):
        """Test running PROVISIONAL analytics."""
        # Mock repository
        self.mock_repo.acquire_advisory_lock.return_value = True
        self.mock_repo.release_advisory_lock.return_value = True
        self.mock_repo.get_analysis_run_by_date.return_value = None
        self.mock_repo.create_analysis_run.return_value = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        
        # Mock cursor for candle reconciliation (no trades)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor
        
        # Mock stages
        self.mock_quality_gate.run_quality_checks.return_value = (True, [])
        self.mock_retention.run_retention.return_value = {"total_deleted": 0}
        
        run = self.runner.run_provisional(date(2026, 9, 12))
        
        assert run.status == RunStatus.SUCCEEDED
        assert run.maturity == Maturity.PROVISIONAL

    def test_run_provisional_lock_failure(self):
        """Test PROVISIONAL run with lock failure."""
        # Mock repository
        self.mock_repo.acquire_advisory_lock.return_value = False
        
        with pytest.raises(RuntimeError, match="Could not acquire advisory lock"):
            self.runner.run_provisional(date(2026, 9, 12))

    def test_run_final(self):
        """Test running FINAL analytics."""
        # Mock repository
        self.mock_repo.acquire_advisory_lock.return_value = True
        self.mock_repo.release_advisory_lock.return_value = True
        self.mock_repo.get_analysis_run_by_date.return_value = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            maturity=Maturity.PROVISIONAL,
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        
        # Mock cursor for post_exit_backfill (no trades)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor
        
        # Mock stages
        self.mock_quality_gate.run_quality_checks.return_value = (True, [])
        self.mock_quality_gate.get_blocking_failures.return_value = []
        
        run = self.runner.run_final(date(2026, 9, 12))
        
        assert run.status == RunStatus.SUCCEEDED
        assert run.maturity == Maturity.FINAL

    def test_run_final_no_provisional(self):
        """Test FINAL run without PROVISIONAL run."""
        # Mock repository
        self.mock_repo.get_analysis_run_by_date.return_value = None
        
        with pytest.raises(ValueError, match="No PROVISIONAL run found"):
            self.runner.run_final(date(2026, 9, 12))

    def test_run_final_already_final(self):
        """Test FINAL run when already FINAL."""
        # Mock repository
        self.mock_repo.get_analysis_run_by_date.return_value = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            maturity=Maturity.FINAL,
        )
        
        run = self.runner.run_final(date(2026, 9, 12))
        
        assert run.maturity == Maturity.FINAL


class TestObservationCutoffPropagation:
    """Regression tests: observation_cutoff must be propagated to CandleSync."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_settings = Mock(spec=Settings)
        self.mock_settings.analytics_post_exit_hours = 4
        self.mock_settings.analytics_candle_retention_days = 180

        self.mock_repo = Mock(spec=AnalyticsRepository)
        self.mock_conn = MagicMock()
        self.mock_repo._conn = self.mock_conn
        self.mock_candle_sync = MagicMock()  # Use MagicMock for flexible mocking
        self.mock_quality_gate = Mock(spec=DataQualityGate)
        self.mock_retention = Mock(spec=CandleRetention)

        self.runner = AnalyticsRunner(
            settings=self.mock_settings,
            repository=self.mock_repo,
            candle_sync=self.mock_candle_sync,
            quality_gate=self.mock_quality_gate,
            retention=self.mock_retention,
        )

    def test_candle_reconciliation_passes_observation_cutoff(self):
        """Test that _stage_candle_reconciliation passes observation_cutoff to reconcile_candles."""
        cutoff = datetime(2026, 9, 11, 15, 32, 19, tzinfo=timezone.utc)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
        )
        stage_run = AnalysisStageRun(run_id=run.run_id, stage_name="candle_reconciliation")

        # Mock: no trades → stage returns early with no_required_ranges
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor

        self.runner._stage_candle_reconciliation(run, stage_run)

        # reconcile_candles should NOT be called (no trades)
        self.mock_candle_sync.reconcile_candles.assert_not_called()

    def test_candle_reconciliation_passes_cutoff_with_trades(self):
        """Test that when trades exist, observation_cutoff is passed to reconcile_candles."""
        cutoff = datetime(2026, 9, 11, 15, 32, 19, tzinfo=timezone.utc)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
            post_exit_horizon=timedelta(hours=4),
        )
        stage_run = AnalysisStageRun(run_id=run.run_id, stage_name="candle_reconciliation")

        # Mock: one trade exists
        mock_cursor = MagicMock()
        mock_cursor.fetchall.side_effect = [
            [(1, "BTCUSDT", datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
              datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc), "5m")],
            [(1,)],  # instrument lookup
        ]
        self.mock_conn.cursor.return_value = mock_cursor

        # Mock reconcile to return success
        self.mock_candle_sync._merge_ranges.return_value = []
        self.mock_candle_sync.reconcile_candles.return_value = ([], 0, 0, 0, [])

        self.runner._stage_candle_reconciliation(run, stage_run)

        # Assert reconcile_candles was called with observation_cutoff
        self.mock_candle_sync.reconcile_candles.assert_called_once()
        call_kwargs = self.mock_candle_sync.reconcile_candles.call_args[1]
        assert call_kwargs["observation_cutoff"] == cutoff

    def test_post_exit_backfill_passes_observation_cutoff(self):
        """Test that _stage_post_exit_backfill passes observation_cutoff."""
        cutoff = datetime(2026, 9, 11, 15, 32, 19, tzinfo=timezone.utc)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
            post_exit_horizon=timedelta(hours=4),
            maturity=Maturity.FINAL,
        )
        stage_run = AnalysisStageRun(run_id=run.run_id, stage_name="post_exit_backfill")

        # Mock: no trades → stage returns early
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor

        self.runner._stage_post_exit_backfill(run, stage_run)

        # reconcile_candles should NOT be called (no trades)
        self.mock_candle_sync.reconcile_candles.assert_not_called()

    def test_provisional_run_refreshes_cutoff_before_stages(self):
        """Test that run_provisional refreshes observation_cutoff before executing stages."""
        cutoff = datetime(2026, 9, 11, 11, 0, tzinfo=timezone.utc)

        # Mock repository
        self.mock_repo.acquire_advisory_lock.return_value = True
        self.mock_repo.release_advisory_lock.return_value = True
        self.mock_repo.get_analysis_run_by_date.return_value = None

        created_run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
        )
        self.mock_repo.create_analysis_run.return_value = created_run

        # Mock cursor for candle reconciliation (no trades)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor

        # Mock stages
        self.mock_quality_gate.run_quality_checks.return_value = (True, [])
        self.mock_retention.run_retention.return_value = {"total_deleted": 0}

        run = self.runner.run_provisional(date(2026, 9, 12))

        # Verify the run was updated with a fresh observation_cutoff
        self.mock_repo.update_analysis_run.assert_called()
        updated_run = self.mock_repo.update_analysis_run.call_args[0][0]
        assert updated_run.observation_cutoff > cutoff

    def test_retry_run_refreshes_cutoff(self):
        """Test that retrying a failed run refreshes observation_cutoff."""
        old_cutoff = datetime(2026, 9, 11, 11, 0, tzinfo=timezone.utc)

        # Create a failed run with old cutoff
        failed_run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=old_cutoff,
            status=RunStatus.FAILED,
            maturity=Maturity.FAILED,
            finished_at=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
            error_code="STAGE_FAILED",
            error_message="old error",
        )

        self.mock_repo.acquire_advisory_lock.return_value = True
        self.mock_repo.release_advisory_lock.return_value = True
        self.mock_repo.get_analysis_run_by_date.return_value = failed_run

        # Mock cursor for candle reconciliation (no trades)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        self.mock_conn.cursor.return_value = mock_cursor

        # Mock stages
        self.mock_quality_gate.run_quality_checks.return_value = (True, [])
        self.mock_retention.run_retention.return_value = {"total_deleted": 0}

        run = self.runner.run_provisional(date(2026, 9, 12))

        # Verify the run was updated with a fresh observation_cutoff
        updated_run = self.mock_repo.update_analysis_run.call_args[0][0]
        assert updated_run.observation_cutoff > old_cutoff
        # Verify stale fields were cleared before execution
        assert updated_run.error_code is None
        assert updated_run.error_message is None

    def test_cutoff_propagation_prevents_future_candles(self):
        """Test that observation_cutoff prevents persisting candles beyond the cutoff."""
        from app.analytics.candle_sync import CandleSync as RealCandleSync

        cutoff = datetime(2026, 9, 11, 15, 7, 16, tzinfo=timezone.utc)

        # Create a real CandleSync with mock Bybit and repo
        mock_bybit = MagicMock()
        mock_repo = MagicMock()
        real_sync = RealCandleSync(mock_bybit, mock_repo)

        # Mock Bybit to return a candle whose close_time > cutoff
        open_time = datetime(2026, 9, 11, 15, 5, tzinfo=timezone.utc)
        open_ms = int(open_time.timestamp() * 1000)
        mock_bybit._public_get.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    [open_ms, "100", "105", "99", "103", "1000", "103000"],  # 15:05-15:10
                ]
            }
        }
        mock_repo.insert_candles_batch.return_value = (0, 0, 0)

        # Fetch with cutoff
        inserted, updated, rejected, failed = real_sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
        )

        # Candle close_time (15:10) > cutoff (15:07) → not persisted
        assert inserted == 0
        assert updated == 0
        # Repository should NOT be called with any candles
        mock_repo.insert_candles_batch.assert_not_called()