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
        """Test candle reconciliation stage."""
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
        
        result = self.runner._stage_candle_reconciliation(run, stage_run)
        
        assert "output_rows" in result
        assert "message" in result

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
        )
        
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
        )
        
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