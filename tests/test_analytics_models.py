"""Unit tests for analytics models."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4

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
    CandleRange,
    Gap,
    Watermark,
)


class TestAnalysisRun:
    """Tests for AnalysisRun model."""

    def test_valid_analysis_run(self):
        """Test creating a valid analysis run."""
        run = AnalysisRun(
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        )
        
        assert run.business_date == date(2026, 9, 12)
        assert run.analysis_from < run.analysis_to
        assert run.maturity == Maturity.PROVISIONAL
        assert run.status == RunStatus.CREATED

    def test_invalid_analysis_from_after_to(self):
        """Test that analysis_from must be before analysis_to."""
        with pytest.raises(ValueError, match="analysis_from must be before analysis_to"):
            AnalysisRun(
                business_date=date(2026, 9, 12),
                analysis_from=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
                analysis_to=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            )

    def test_error_message_with_secrets(self):
        """Test that error_message cannot contain secrets."""
        with pytest.raises(ValueError, match="error_message must not contain secrets"):
            AnalysisRun(
                business_date=date(2026, 9, 12),
                error_message="Password is incorrect",
            )

    def test_maturity_states(self):
        """Test all maturity states."""
        for maturity in Maturity:
            run = AnalysisRun(
                business_date=date(2026, 9, 12),
                analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
                maturity=maturity,
            )
            assert run.maturity == maturity

    def test_status_states(self):
        """Test all status states."""
        for status in RunStatus:
            run = AnalysisRun(
                business_date=date(2026, 9, 12),
                analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
                status=status,
            )
            assert run.status == status


class TestAnalysisStageRun:
    """Tests for AnalysisStageRun model."""

    def test_valid_stage_run(self):
        """Test creating a valid stage run."""
        stage = AnalysisStageRun(
            run_id=uuid4(),
            stage_name="candle_reconciliation",
            attempt=1,
        )
        
        assert stage.stage_name == "candle_reconciliation"
        assert stage.attempt == 1
        assert stage.status == StageStatus.CREATED

    def test_empty_stage_name(self):
        """Test that stage_name cannot be empty."""
        with pytest.raises(ValueError, match="stage_name is required"):
            AnalysisStageRun(
                run_id=uuid4(),
                stage_name="",
            )

    def test_stage_status_states(self):
        """Test all stage status states."""
        for status in StageStatus:
            stage = AnalysisStageRun(
                run_id=uuid4(),
                stage_name="test_stage",
                status=status,
            )
            assert stage.status == status


class TestDataQualityResult:
    """Tests for DataQualityResult model."""

    def test_valid_quality_result(self):
        """Test creating a valid quality result."""
        result = DataQualityResult(
            run_id=uuid4(),
            stage_name="quality_gate",
            check_name="postgresql_availability",
            severity=Severity.BLOCKING,
            status=StageStatus.SUCCEEDED,
        )
        
        assert result.check_name == "postgresql_availability"
        assert result.severity == Severity.BLOCKING
        assert result.status == StageStatus.SUCCEEDED

    def test_severity_levels(self):
        """Test all severity levels."""
        for severity in Severity:
            result = DataQualityResult(
                run_id=uuid4(),
                stage_name="test_stage",
                check_name="test_check",
                severity=severity,
                status=StageStatus.SUCCEEDED,
            )
            assert result.severity == severity


class TestCandle:
    """Tests for Candle model."""

    def test_valid_candle(self):
        """Test creating a valid candle."""
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
        
        assert candle.instrument_id == 1
        assert candle.open == 100.0
        assert candle.high == 105.0
        assert candle.low == 99.0
        assert candle.close == 103.0
        assert candle.is_closed is True

    def test_invalid_ohlc_relationships(self):
        """Test invalid OHLC relationships."""
        # High < open
        with pytest.raises(ValueError, match="high must be >= open, close, and low"):
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                open=100.0,
                high=95.0,  # Invalid: high < open
                low=99.0,
                close=103.0,
                volume=1000.0,
            )

    def test_invalid_prices(self):
        """Test invalid prices."""
        # Zero price
        with pytest.raises(ValueError, match="All prices must be > 0"):
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                open=0.0,  # Invalid
                high=105.0,
                low=99.0,
                close=103.0,
                volume=1000.0,
            )

    def test_negative_volume(self):
        """Test negative volume."""
        with pytest.raises(ValueError, match="Volume must be >= 0"):
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                open=100.0,
                high=105.0,
                low=99.0,
                close=103.0,
                volume=-100.0,  # Invalid
            )

    def test_invalid_timestamps(self):
        """Test invalid timestamps."""
        with pytest.raises(ValueError, match="close_time must be > open_time"):
            Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),  # Invalid
                open=100.0,
                high=105.0,
                low=99.0,
                close=103.0,
                volume=1000.0,
            )

    def test_candle_always_closed(self):
        """Test that candle is always closed for analytics."""
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
            is_closed=False,  # Should be overridden
        )
        
        assert candle.is_closed is True

    def test_to_dict(self):
        """Test conversion to dictionary."""
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
        
        d = candle.to_dict()
        
        assert d["instrument_id"] == 1
        assert d["timeframe"] == "5"
        assert d["open"] == 100.0
        assert d["is_closed"] is True
        assert d["quality_status"] == "validated"


class TestCandleRange:
    """Tests for CandleRange model."""

    def test_valid_range(self):
        """Test creating a valid range."""
        range_ = CandleRange(
            instrument_id=1,
            timeframe="5",
            from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        )
        
        assert range_.from_time < range_.to_time

    def test_invalid_range(self):
        """Test invalid range."""
        with pytest.raises(ValueError, match="from_time must be before to_time"):
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            )


class TestGap:
    """Tests for Gap model."""

    def test_gap_duration(self):
        """Test gap duration calculation."""
        gap = Gap(
            instrument_id=1,
            timeframe="5",
            gap_start=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            gap_end=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        )
        
        assert gap.duration == timedelta(hours=4)


class TestWatermark:
    """Tests for Watermark model."""

    def test_watermark(self):
        """Test watermark creation."""
        watermark = Watermark(
            instrument_id=1,
            timeframe="5",
            latest_candle_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
        )
        
        assert watermark.instrument_id == 1
        assert watermark.timeframe == "5"