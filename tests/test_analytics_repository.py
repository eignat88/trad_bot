"""Unit tests for analytics repository."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, date
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from app.analytics.repository import AnalyticsRepository
from app.analytics.models import (
    AnalysisRun,
    AnalysisStageRun,
    DataQualityResult,
    Candle,
    Maturity,
    RunStatus,
    StageStatus,
    Severity,
    QualityStatus,
)


class TestAnalyticsRepository:
    """Tests for AnalyticsRepository."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_conn = MagicMock()
        self.repository = AnalyticsRepository(self.mock_conn)

    def test_create_analysis_run(self):
        """Test creating an analysis run."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = None
        
        result = self.repository.create_analysis_run(run)
        
        assert result.run_id == run.run_id
        assert result.business_date == run.business_date
        self.mock_conn.commit.assert_called_once()

    def test_get_analysis_run(self):
        """Test getting an analysis run by ID."""
        run_id = uuid4()
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = (
            str(run_id),
            date(2026, 9, 12),
            "Europe/Sofia",
            datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            datetime.now(timezone.utc),
            "4 hours",
            "PROVISIONAL",
            "CREATED",
            "1.0.0",
            "{}",
            None,
            None,
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            None,
            None,
        )
        
        result = self.repository.get_analysis_run(run_id)
        
        assert result is not None
        assert result.run_id == run_id

    def test_get_analysis_run_not_found(self):
        """Test getting a non-existent analysis run."""
        run_id = uuid4()
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = None
        
        result = self.repository.get_analysis_run(run_id)
        
        assert result is None

    def test_get_analysis_run_by_date(self):
        """Test getting an analysis run by business date."""
        business_date = date(2026, 9, 12)
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = (
            str(uuid4()),
            business_date,
            "Europe/Sofia",
            datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            datetime.now(timezone.utc),
            "4 hours",
            "PROVISIONAL",
            "CREATED",
            "1.0.0",
            "{}",
            None,
            None,
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            None,
            None,
        )
        
        result = self.repository.get_analysis_run_by_date(business_date)
        
        assert result is not None
        assert result.business_date == business_date

    def test_update_analysis_run(self):
        """Test updating an analysis run."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            status=RunStatus.SUCCEEDED,
            maturity=Maturity.PROVISIONAL,
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = None
        
        result = self.repository.update_analysis_run(run)
        
        assert result.status == RunStatus.SUCCEEDED
        self.mock_conn.commit.assert_called_once()

    def test_create_stage_run(self):
        """Test creating a stage run."""
        stage_run = AnalysisStageRun(
            run_id=uuid4(),
            stage_name="candle_reconciliation",
            attempt=1,
            status=StageStatus.RUNNING,
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = [1]
        
        result = self.repository.create_stage_run(stage_run)
        
        assert result.stage_run_id == 1
        assert result.stage_name == "candle_reconciliation"
        self.mock_conn.commit.assert_called_once()

    def test_update_stage_run(self):
        """Test updating a stage run."""
        stage_run = AnalysisStageRun(
            stage_run_id=1,
            run_id=uuid4(),
            stage_name="candle_reconciliation",
            status=StageStatus.SUCCEEDED,
            input_rows=100,
            output_rows=95,
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = None
        
        result = self.repository.update_stage_run(stage_run)
        
        assert result.status == StageStatus.SUCCEEDED
        self.mock_conn.commit.assert_called_once()

    def test_create_quality_result(self):
        """Test creating a quality result."""
        result = DataQualityResult(
            run_id=uuid4(),
            stage_name="quality_gate",
            check_name="postgresql_availability",
            severity=Severity.BLOCKING,
            status=StageStatus.SUCCEEDED,
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = [1]
        
        created_result = self.repository.create_quality_result(result)
        
        assert created_result.quality_result_id == 1
        self.mock_conn.commit.assert_called_once()

    def test_get_quality_results(self):
        """Test getting quality results for a run."""
        run_id = uuid4()
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchall.return_value = [
            (
                1,
                str(run_id),
                "quality_gate",
                "postgresql_availability",
                None,
                None,
                "BLOCKING",
                "SUCCEEDED",
                None,
                None,
                None,
                None,
                datetime.now(timezone.utc),
                None,
            ),
        ]
        
        results = self.repository.get_quality_results(run_id)
        
        assert len(results) == 1
        assert results[0].check_name == "postgresql_availability"

    def test_insert_candle(self):
        """Test inserting a candle."""
        candle = Candle(
            instrument_id=1,
            timeframe="5",
            open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000.0,
        )
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = None
        
        result = self.repository.insert_candle(candle)
        
        assert result is True
        self.mock_conn.commit.assert_called_once()

    def test_insert_candles_batch(self):
        """Test inserting a batch of candles."""
        candles = [
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                open=100.0,
                high=105.0,
                low=99.0,
                close=103.0,
                volume=1000.0,
            ),
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 10, tzinfo=timezone.utc),
                open=103.0,
                high=108.0,
                low=102.0,
                close=107.0,
                volume=1200.0,
            ),
        ]
        
        # Mock cursor - need to mock the cursor properly
        mock_cursor = MagicMock()
        self.mock_conn.cursor.return_value = mock_cursor
        
        # First candle: doesn't exist, then insert succeeds
        mock_cursor.fetchone.side_effect = [None, None]
        # Second candle: doesn't exist, then insert succeeds
        mock_cursor.fetchone.side_effect = [None, None]
        
        inserted, updated, rejected = self.repository.insert_candles_batch(candles)
        
        # Both candles should be inserted (not updated)
        assert inserted == 2
        assert updated == 0
        assert rejected == 0

    def test_get_candle_coverage(self):
        """Test getting candle coverage gaps."""
        from datetime import timedelta
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchall.return_value = [
            (
                datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
                timedelta(hours=1),
            ),
        ]
        
        gaps = self.repository.get_candle_coverage(
            instrument_id=1,
            timeframe="5",
            from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        )
        
        assert len(gaps) == 1
        assert gaps[0]["gap_start"] == datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc)

    def test_get_watermark(self):
        """Test getting watermark."""
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = [
            datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
        ]
        
        watermark = self.repository.get_watermark(instrument_id=1, timeframe="5")
        
        assert watermark is not None
        assert watermark.instrument_id == 1
        assert watermark.timeframe == "5"

    def test_get_existing_final_run(self):
        """Test getting existing FINAL run."""
        business_date = date(2026, 9, 12)
        
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = (
            str(uuid4()),
            business_date,
            "Europe/Sofia",
            datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            datetime.now(timezone.utc),
            "4 hours",
            "FINAL",
            "SUCCEEDED",
            "1.0.0",
            "{}",
            None,
            None,
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            None,
            None,
        )
        
        run = self.repository.get_existing_final_run(business_date)
        
        assert run is not None
        assert run.maturity == Maturity.FINAL

    def test_acquire_advisory_lock(self):
        """Test acquiring advisory lock."""
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = [True]
        
        result = self.repository.acquire_advisory_lock(lock_id=12345)
        
        assert result is True

    def test_release_advisory_lock(self):
        """Test releasing advisory lock."""
        # Mock cursor
        self.mock_conn.cursor.return_value.fetchone.return_value = [True]
        
        result = self.repository.release_advisory_lock(lock_id=12345)
        
        assert result is True