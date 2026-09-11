"""Integration tests for analytics repository with real pg8000 driver."""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4

import pg8000

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
from app.analytics.repository import AnalyticsRepository


@pytest.fixture(scope="module")
def pg8000_conn():
    """Create a real pg8000 connection to test database."""
    conn = pg8000.connect(
        host="localhost",
        port=5432,
        database="trad_bot_migration_test",
        user="postgres",
        password="",
    )
    
    yield conn
    
    conn.close()


@pytest.fixture
def repository(pg8000_conn):
    """Create AnalyticsRepository with real pg8000 connection."""
    # Clean up before each test
    cursor = pg8000_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result")
    cursor.execute("DELETE FROM analytics.analysis_stage_run")
    cursor.execute("DELETE FROM analytics.analysis_run")
    cursor.execute("DELETE FROM market.candle")
    pg8000_conn.commit()
    
    repo = AnalyticsRepository(pg8000_conn)
    yield repo
    
    # Clean up after each test
    cursor = pg8000_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result")
    cursor.execute("DELETE FROM analytics.analysis_stage_run")
    cursor.execute("DELETE FROM analytics.analysis_run")
    cursor.execute("DELETE FROM market.candle")
    pg8000_conn.commit()


class TestAnalyticsRepositoryPG8000:
    """Integration tests for AnalyticsRepository with real pg8000 driver."""

    def test_create_analysis_run(self, repository):
        """Test creating an analysis run with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            maturity=Maturity.PROVISIONAL,
            status=RunStatus.CREATED,
        )
        
        result = repository.create_analysis_run(run)
        
        assert result.run_id == run.run_id
        assert result.business_date == run.business_date
        assert result.maturity == Maturity.PROVISIONAL
        assert result.status == RunStatus.CREATED

    def test_get_analysis_run(self, repository):
        """Test getting an analysis run with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        
        repository.create_analysis_run(run)
        retrieved = repository.get_analysis_run(run.run_id)
        
        assert retrieved is not None
        assert retrieved.run_id == run.run_id

    def test_get_analysis_run_by_date(self, repository):
        """Test getting an analysis run by date with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        
        repository.create_analysis_run(run)
        retrieved = repository.get_analysis_run_by_date(date(2026, 9, 12))
        
        assert retrieved is not None
        assert retrieved.business_date == date(2026, 9, 12)

    def test_update_analysis_run(self, repository):
        """Test updating an analysis run with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            status=RunStatus.CREATED,
        )
        
        repository.create_analysis_run(run)
        
        # Update the run
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        repository.update_analysis_run(run)
        
        # Verify update
        retrieved = repository.get_analysis_run(run.run_id)
        assert retrieved.status == RunStatus.RUNNING
        assert retrieved.started_at is not None

    def test_create_stage_run(self, repository):
        """Test creating a stage run with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="candle_reconciliation",
            attempt=1,
            status=StageStatus.RUNNING,
        )
        
        result = repository.create_stage_run(stage_run)
        
        assert result.stage_run_id is not None
        assert result.stage_name == "candle_reconciliation"

    def test_update_stage_run(self, repository):
        """Test updating a stage run with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name="candle_reconciliation",
            attempt=1,
            status=StageStatus.RUNNING,
        )
        repository.create_stage_run(stage_run)
        
        # Update the stage run
        stage_run.status = StageStatus.SUCCEEDED
        stage_run.finished_at = datetime.now(timezone.utc)
        repository.update_stage_run(stage_run)
        
        # Verify update
        cursor = repository._conn.cursor()
        cursor.execute(
            "SELECT status FROM analytics.analysis_stage_run WHERE stage_run_id = %s",
            (stage_run.stage_run_id,),
        )
        row = cursor.fetchone()
        assert row[0] == "SUCCEEDED"

    def test_create_quality_result(self, repository):
        """Test creating a quality result with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        result = DataQualityResult(
            run_id=run.run_id,
            stage_name="quality_gate",
            check_name="postgresql_availability",
            severity=Severity.BLOCKING,
            status=StageStatus.PASS,  # Use PASS for data_quality_result
            details={"message": "PostgreSQL is available"},
        )
        
        created = repository.create_quality_result(result)
        
        assert created.quality_result_id is not None
        assert created.check_name == "postgresql_availability"

    def test_insert_candle(self, repository):
        """Test inserting a candle with real pg8000."""
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
        
        result = repository.insert_candle(candle)
        
        assert result is True
        
        # Verify candle was inserted
        cursor = repository._conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM market.candle")
        count = cursor.fetchone()[0]
        assert count == 1

    def test_insert_candle_upsert(self, repository):
        """Test candle UPSERT with real pg8000."""
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
        
        # Insert first time
        repository.insert_candle(candle)
        
        # Insert again (should update)
        candle.volume = 2000.0
        repository.insert_candle(candle)
        
        # Verify only one candle exists with updated volume
        cursor = repository._conn.cursor()
        cursor.execute("SELECT volume FROM market.candle WHERE instrument_id = 1")
        row = cursor.fetchone()
        assert row[0] == 2000.0

    def test_insert_candles_batch(self, repository):
        """Test batch candle insert with real pg8000."""
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
                instrument_id=2,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                open=200.0,
                high=205.0,
                low=199.0,
                close=203.0,
                volume=2000.0,
            ),
        ]
        
        inserted, updated, rejected = repository.insert_candles_batch(candles)
        
        assert inserted == 2
        assert updated == 0
        assert rejected == 0

    def test_advisory_lock(self, repository):
        """Test advisory lock with real pg8000."""
        # Acquire lock
        acquired = repository.acquire_advisory_lock(12345)
        assert acquired is True
        
        # Release lock
        released = repository.release_advisory_lock(12345)
        assert released is True

    def test_transaction_rollback(self, pg8000_conn):
        """Test transaction rollback on error."""
        repo = AnalyticsRepository(pg8000_conn)
        
        # First create a valid run
        valid_run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repo.create_analysis_run(valid_run)
        
        # Verify run was created
        cursor = pg8000_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM analytics.analysis_run")
        count = cursor.fetchone()[0]
        assert count == 1
        
        # Now try to create a run with duplicate run_id (should fail at DB level)
        duplicate_run = AnalysisRun(
            run_id=valid_run.run_id,  # Same run_id
            business_date=date(2026, 9, 13),
            analysis_from=datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        
        with pytest.raises(Exception):
            repo.create_analysis_run(duplicate_run)
        
        # Verify the original run still exists (transaction was rolled back)
        cursor = pg8000_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM analytics.analysis_run")
        count = cursor.fetchone()[0]
        assert count == 1

    def test_get_candle_coverage(self, repository):
        """Test getting candle coverage with real pg8000."""
        # Insert some candles
        for i in range(5):
            candle = Candle(
                instrument_id=1,
                timeframe="5",
                open_time=datetime(2026, 9, 12, 6, i*5, tzinfo=timezone.utc),
                close_time=datetime(2026, 9, 12, 6, (i+1)*5, tzinfo=timezone.utc),
                open=100.0 + i,
                high=105.0 + i,
                low=99.0 + i,
                close=103.0 + i,
                volume=1000.0 + i*100,
            )
            repository.insert_candle(candle)
        
        # Check coverage
        coverage = repository.get_candle_coverage(
            instrument_id=1,
            timeframe="5",
            from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 12, 6, 25, tzinfo=timezone.utc),
        )
        
        # Should have no gaps
        assert len(coverage) == 0

    def test_get_watermark(self, repository):
        """Test getting watermark with real pg8000."""
        # Insert a candle
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
        repository.insert_candle(candle)
        
        # Get watermark
        watermark = repository.get_watermark(instrument_id=1, timeframe="5")
        
        assert watermark is not None
        assert watermark.latest_candle_time == datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)

    def test_get_existing_final_run(self, repository):
        """Test getting existing FINAL run with real pg8000."""
        # Create a PROVISIONAL run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            maturity=Maturity.PROVISIONAL,
        )
        repository.create_analysis_run(run)
        
        # Check for FINAL run (should not exist)
        final_run = repository.get_existing_final_run(date(2026, 9, 12))
        assert final_run is None
        
        # Update to FINAL
        run.maturity = Maturity.FINAL
        repository.update_analysis_run(run)
        
        # Check again (should exist now)
        final_run = repository.get_existing_final_run(date(2026, 9, 12))
        assert final_run is not None
        assert final_run.maturity == Maturity.FINAL


class TestPG8000PlaceholderRegression:
    """Regression tests to ensure no unsupported placeholders are used."""

    def test_no_named_pyformat_placeholders(self):
        """Test that repository doesn't use named pyformat placeholders."""
        import inspect
        import re
        
        # Get all methods in AnalyticsRepository
        for name, method in inspect.getmembers(AnalyticsRepository, predicate=inspect.isfunction):
            if name.startswith('_'):
                continue
            
            # Get the source code of the method
            source = inspect.getsource(method)
            
            # Check for named pyformat placeholders %(name)s
            matches = re.findall(r'%\([a-zA-Z_][a-zA-Z0-9_]*\)s', source)
            
            assert len(matches) == 0, f"Method {name} contains named pyformat placeholders: {matches}"

    def test_all_sql_uses_positional_placeholders(self):
        """Test that all SQL statements use positional placeholders."""
        import inspect
        import re
        
        # Get all methods in AnalyticsRepository
        for name, method in inspect.getmembers(AnalyticsRepository, predicate=inspect.isfunction):
            if name.startswith('_'):
                continue
            
            # Get the source code of the method
            source = inspect.getsource(method)
            
            # Count positional placeholders
            positional_count = len(re.findall(r'%s', source))
            
            # Count named placeholders
            named_count = len(re.findall(r'%\([a-zA-Z_][a-zA-Z0-9_]*\)s', source))
            
            # If there are any placeholders, they should all be positional
            if positional_count + named_count > 0:
                assert named_count == 0, f"Method {name} has {named_count} named placeholders"