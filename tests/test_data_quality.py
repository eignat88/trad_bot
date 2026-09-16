"""Unit tests for data quality gate."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from app.analytics.quality import DataQualityGate
from app.analytics.models import (
    AnalysisRun,
    DataQualityResult,
    Maturity,
    Severity,
    StageStatus,
    QualityCheckStatus,
)
from app.analytics.repository import AnalyticsRepository


class TestDataQualityGate:
    """Tests for DataQualityGate."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_repo = Mock(spec=AnalyticsRepository)
        self.mock_conn = MagicMock()
        self.mock_repo._conn = self.mock_conn
        
        self.quality_gate = DataQualityGate(self.mock_repo)
        
        self.run = AnalysisRun(
            run_id=uuid4(),
            business_date=datetime.now(timezone.utc).date(),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            maturity=Maturity.PROVISIONAL,
        )

    def test_check_postgresql_availability(self):
        """Test PostgreSQL availability check."""
        # Mock successful query
        self.mock_conn.cursor.return_value.fetchone.return_value = [1]
        
        result = self.quality_gate._check_postgresql_availability(
            self.run.run_id, "quality_gate"
        )
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_postgresql_availability_failure(self):
        """Test PostgreSQL availability check failure."""
        # Mock failed query
        self.mock_conn.cursor.return_value.execute.side_effect = Exception("Connection failed")
        
        result = self.quality_gate._check_postgresql_availability(
            self.run.run_id, "quality_gate"
        )
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL

    def test_check_source_timestamps(self):
        """Test source timestamps check."""
        # Mock no future candles
        self.mock_conn.cursor.return_value.fetchone.return_value = [0]
        
        result = self.quality_gate._check_source_timestamps(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_source_timestamps_failure(self):
        """Test source timestamps check failure."""
        # Mock future candles found (first query returns 5, second returns 0)
        self.mock_conn.cursor.return_value.fetchone.side_effect = [[5], [0]]
        
        result = self.quality_gate._check_source_timestamps(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL
        assert result.actual_value["future_open_count"] == 5

    def test_check_duplicate_candles(self):
        """Test duplicate candles check."""
        # Mock no duplicates
        self.mock_conn.cursor.return_value.fetchone.return_value = [0]
        
        result = self.quality_gate._check_duplicate_candles(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_duplicate_candles_failure(self):
        """Test duplicate candles check failure."""
        # Mock duplicates found
        self.mock_conn.cursor.return_value.fetchone.return_value = [3]
        
        result = self.quality_gate._check_duplicate_candles(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL
        assert result.actual_value["duplicate_count"] == 3

    def test_check_ohlc_validation(self):
        """Test OHLC validation check."""
        # Mock no invalid candles
        self.mock_conn.cursor.return_value.fetchone.return_value = [0]
        
        result = self.quality_gate._check_ohlc_validation(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_ohlc_validation_failure(self):
        """Test OHLC validation check failure."""
        # Mock invalid candles found
        self.mock_conn.cursor.return_value.fetchone.return_value = [2]
        
        result = self.quality_gate._check_ohlc_validation(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL
        assert result.actual_value["invalid_candle_count"] == 2

    def test_check_closed_candle_intervals(self):
        """Test closed candle intervals check."""
        # Mock no invalid intervals
        self.mock_conn.cursor.return_value.fetchone.return_value = [0]
        
        result = self.quality_gate._check_closed_candle_intervals(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_closed_candle_intervals_failure(self):
        """Test closed candle intervals check failure."""
        # Mock invalid intervals found
        self.mock_conn.cursor.return_value.fetchone.return_value = [1]
        
        result = self.quality_gate._check_closed_candle_intervals(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL
        assert result.actual_value["invalid_interval_count"] == 1

    def test_check_post_exit_coverage(self):
        """Test post-exit coverage check."""
        # Mock no trades
        self.mock_conn.cursor.return_value.fetchone.return_value = [0]
        self.mock_conn.cursor.return_value.fetchall.return_value = []
        
        result = self.quality_gate._check_post_exit_coverage(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.PASS

    def test_check_post_exit_coverage_failure(self):
        """Test post-exit coverage check failure."""
        # Mock trades with missing coverage
        self.mock_conn.cursor.return_value.fetchone.return_value = [1]
        self.mock_conn.cursor.return_value.fetchall.return_value = [
            (1, datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc), "5", 1)
        ]
        
        # Mock missing coverage query
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [1],  # trade count
            [0],  # candle count (missing)
        ]
        
        result = self.quality_gate._check_post_exit_coverage(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.FAIL

    def test_check_lifecycle_timestamps(self):
        """Test lifecycle timestamps check."""
        # Set valid timestamps
        self.run.started_at = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        self.run.finished_at = datetime(2026, 9, 12, 6, 30, tzinfo=timezone.utc)
        
        result = self.quality_gate._check_lifecycle_timestamps(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.PASS

    def test_check_lifecycle_timestamps_failure(self):
        """Test lifecycle timestamps check failure."""
        # Set invalid timestamps
        self.run.started_at = datetime(2026, 9, 12, 6, 30, tzinfo=timezone.utc)
        self.run.finished_at = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        
        result = self.quality_gate._check_lifecycle_timestamps(self.run, "quality_gate")
        
        assert result.severity == Severity.BLOCKING
        assert result.status == QualityCheckStatus.FAIL

    def test_check_data_freshness(self):
        """Test data freshness check."""
        # Mock fresh data
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [datetime.now(timezone.utc) - timedelta(minutes=2)],  # latest candle
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.PASS

    def test_check_data_freshness_failure(self):
        """Test data freshness check failure."""
        # Mock stale data
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [datetime.now(timezone.utc) - timedelta(hours=2)],  # latest candle (2 hours old)
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.FAIL

    def test_check_data_freshness_uses_observation_cutoff(self):
        """Test that data freshness uses observation_cutoff instead of now().
        
        This is the key fix for the issue where FINAL runs were failing
        because they compared against current time instead of the analysis window.
        """
        # Set observation_cutoff to a specific time in the past
        observation_cutoff = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        self.run.observation_cutoff = observation_cutoff
        
        # Mock data that is fresh relative to observation_cutoff
        # Latest candle is 2 minutes before cutoff
        latest_candle = observation_cutoff - timedelta(minutes=2)
        
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [latest_candle],  # latest candle
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.PASS
        # Verify reference_time is observation_cutoff, not now()
        assert result.actual_value["reference_time"] == observation_cutoff.isoformat()

    def test_check_data_freshness_final_run_historical(self):
        """Test data freshness for a FINAL run with historical cutoff.
        
        This simulates the case where a FINAL run is executed hours/days after
        the analysis period. The check should use observation_cutoff, not now().
        """
        # Set observation_cutoff to a time in the past (simulating historical run)
        observation_cutoff = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        self.run.observation_cutoff = observation_cutoff
        self.run.maturity = Maturity.FINAL
        
        # Mock data that is fresh relative to observation_cutoff
        # Latest candle is 5 minutes before cutoff (well within 15-minute SLA)
        latest_candle = observation_cutoff - timedelta(minutes=5)
        
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [latest_candle],  # latest candle
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.PASS
        # Freshness should be 5 minutes (relative to cutoff), not hours/days
        assert result.actual_value["freshness_minutes"] == 5.0

    def test_check_data_freshness_final_run_gap_before_cutoff(self):
        """Test data freshness for a FINAL run with gap before cutoff.
        
        This simulates the case where there's a real data gap before the cutoff.
        The check should FAIL even though the gap is relative to cutoff.
        """
        # Set observation_cutoff
        observation_cutoff = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
        self.run.observation_cutoff = observation_cutoff
        self.run.maturity = Maturity.FINAL
        
        # Mock data with a real gap: latest candle is 30 minutes before cutoff
        # This exceeds the 15-minute SLA for 5m candles
        latest_candle = observation_cutoff - timedelta(minutes=30)
        
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [latest_candle],  # latest candle
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.FAIL
        assert result.actual_value["freshness_minutes"] == 30.0
        assert result.actual_value["sla_minutes"] == 15

    def test_check_data_freshness_provisional_run(self):
        """Test data freshness for a PROVISIONAL run.
        
        PROVISIONAL runs should also use observation_cutoff for consistency.
        """
        # Set observation_cutoff to recent time
        observation_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.run.observation_cutoff = observation_cutoff
        self.run.maturity = Maturity.PROVISIONAL
        
        # Mock fresh data relative to cutoff
        latest_candle = observation_cutoff - timedelta(minutes=2)
        
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [latest_candle],  # latest candle
            ["5", 100],  # primary timeframe
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.PASS

    def test_check_data_freshness_no_candles(self):
        """Test data freshness when no candles exist."""
        # Mock no candles
        self.mock_conn.cursor.return_value.fetchone.side_effect = [
            [None],  # no candles
        ]
        
        result = self.quality_gate._check_data_freshness(self.run, "quality_gate")
        
        assert result.severity == Severity.WARNING
        assert result.status == QualityCheckStatus.SKIPPED
        assert "No candles found" in result.details["message"]

    def test_has_blocking_failures(self):
        """Test has_blocking_failures method."""
        results = [
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test1",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
            ),
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test2",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
            ),
        ]
        
        assert self.quality_gate.has_blocking_failures(results) is True

    def test_get_blocking_failures(self):
        """Test get_blocking_failures method."""
        results = [
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test1",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
            ),
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test2",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
            ),
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test3",
                severity=Severity.WARNING,
                status=QualityCheckStatus.FAIL,
            ),
        ]
        
        blocking_failures = self.quality_gate.get_blocking_failures(results)
        
        assert len(blocking_failures) == 1
        assert blocking_failures[0].check_name == "test2"

    def test_get_summary(self):
        """Test get_summary method."""
        results = [
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test1",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
            ),
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test2",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
            ),
            DataQualityResult(
                run_id=self.run.run_id,
                stage_name="quality_gate",
                check_name="test3",
                severity=Severity.WARNING,
                status=QualityCheckStatus.SKIPPED,
            ),
        ]
        
        summary = self.quality_gate.get_summary(results)
        
        assert summary["total"] == 3
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        assert summary["skipped"] == 1
        assert summary["blocking_failed"] == 1
        assert summary["passed_all_blocking"] is False