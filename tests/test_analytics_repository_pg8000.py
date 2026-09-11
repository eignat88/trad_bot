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
    QualityCheckStatus,
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
    # First, rollback any aborted transaction
    try:
        pg8000_conn.rollback()
    except Exception:
        pass
    
    # Clean up before each test
    cursor = pg8000_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result")
    cursor.execute("DELETE FROM analytics.analysis_stage_run")
    cursor.execute("DELETE FROM analytics.analysis_run")
    cursor.execute("DELETE FROM market.candle")
    cursor.execute("DELETE FROM dds.paper_trade WHERE trade_id >= 900000")
    cursor.execute("DELETE FROM dds.scanner_setup WHERE setup_id LIKE 'setup_%'")
    cursor.execute("DELETE FROM dds.instrument WHERE symbol LIKE 'TFTEST%'")
    pg8000_conn.commit()
    
    repo = AnalyticsRepository(pg8000_conn)
    yield repo
    
    # Clean up after each test
    try:
        pg8000_conn.rollback()
    except Exception:
        pass
    
    cursor = pg8000_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result")
    cursor.execute("DELETE FROM analytics.analysis_stage_run")
    cursor.execute("DELETE FROM analytics.analysis_run")
    cursor.execute("DELETE FROM market.candle")
    cursor.execute("DELETE FROM dds.paper_trade WHERE trade_id >= 900000")
    cursor.execute("DELETE FROM dds.scanner_setup WHERE setup_id LIKE 'setup_%'")
    cursor.execute("DELETE FROM dds.instrument WHERE symbol LIKE 'TFTEST%'")
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
            status=QualityCheckStatus.PASS,
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


class TestPostExitHorizonRoundTrip:
    """Tests for post_exit_horizon timedelta round-trip through PostgreSQL."""

    def test_interval_round_trip(self, repository):
        """Test INTERVAL round-trip: create AnalysisRun with 4 hour horizon,
        persist, reload, execute post-exit coverage — no string parsing error."""
        from datetime import timedelta

        horizon = timedelta(hours=4)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            post_exit_horizon=horizon,
        )

        # Persist
        repository.create_analysis_run(run)

        # Reload
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded is not None
        assert isinstance(reloaded.post_exit_horizon, timedelta)
        assert reloaded.post_exit_horizon == horizon

        # Execute post-exit coverage — should not raise AttributeError
        from app.analytics.quality import DataQualityGate
        gate = DataQualityGate(repository)
        result = gate._check_post_exit_coverage(reloaded, "quality_gate")
        assert result is not None
        assert result.check_name == "post_exit_coverage"

    def test_interval_default_when_none(self, repository):
        """Test that model default of 4 hours is used when no explicit value given."""
        from datetime import timedelta

        # Create with explicit 4 hour horizon (the default)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            # post_exit_horizon defaults to timedelta(hours=4)
        )

        repository.create_analysis_run(run)
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded is not None
        assert reloaded.post_exit_horizon == timedelta(hours=4)

    def test_interval_6_hours_round_trip(self, repository):
        """Test non-default interval round-trip."""
        from datetime import timedelta

        horizon = timedelta(hours=6)
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            post_exit_horizon=horizon,
        )

        repository.create_analysis_run(run)
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.post_exit_horizon == horizon


class TestQualityCheckStatusValues:
    """Tests that verify database quality_result status values."""

    def test_quality_result_pass_persists(self, repository):
        """Test that PASS status is accepted by database."""
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
            check_name="test_pass",
            severity=Severity.BLOCKING,
            status=QualityCheckStatus.PASS,
            details={"message": "test"},
        )
        created = repository.create_quality_result(result)
        assert created.quality_result_id is not None

    def test_quality_result_fail_persists(self, repository):
        """Test that FAIL status is accepted by database."""
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
            check_name="test_fail",
            severity=Severity.WARNING,
            status=QualityCheckStatus.FAIL,
            details={"message": "test failure"},
        )
        created = repository.create_quality_result(result)
        assert created.quality_result_id is not None

    def test_quality_result_skipped_persists(self, repository):
        """Test that SKIPPED status is accepted by database."""
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
            check_name="test_skipped",
            severity=Severity.WARNING,
            status=QualityCheckStatus.SKIPPED,
            details={"message": "skipped"},
        )
        created = repository.create_quality_result(result)
        assert created.quality_result_id is not None

    def test_quality_result_failed_not_accepted(self, repository):
        """Test that StageStatus.FAILED is NOT accepted by database."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)

        # Intentionally use wrong status type
        result = DataQualityResult(
            run_id=run.run_id,
            stage_name="quality_gate",
            check_name="test_wrong_status",
            severity=Severity.WARNING,
            status="FAILED",  # Raw string, not QualityCheckStatus
            details={"message": "wrong"},
        )
        with pytest.raises(Exception):
            repository.create_quality_result(result)

    def test_quality_result_succeeded_not_accepted(self, repository):
        """Test that StageStatus.SUCCEEDED is NOT accepted by database."""
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
            check_name="test_wrong_status",
            severity=Severity.WARNING,
            status="SUCCEEDED",  # Raw string, not QualityCheckStatus
            details={"message": "wrong"},
        )
        with pytest.raises(Exception):
            repository.create_quality_result(result)


class TestLifecycleRetryReset:
    """Tests for lifecycle retry/reset semantics."""

    def test_provisional_retry_resets_stale_fields(self, repository):
        """Test that retrying a failed PROVISIONAL run resets terminal fields."""
        from app.analytics.runner import AnalyticsRunner
        from app.analytics.models import RunStatus, Maturity

        # 1. Create an initial FAILED run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)

        # Simulate a failed first attempt
        run.status = RunStatus.FAILED
        run.maturity = Maturity.FAILED
        run.started_at = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        run.finished_at = datetime(2026, 9, 11, 12, 20, tzinfo=timezone.utc)
        run.error_code = "STAGE_FAILED"
        run.error_message = "old error message"
        repository.update_analysis_run(run)

        # Verify the failed state is persisted
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.status == RunStatus.FAILED
        assert reloaded.maturity == Maturity.FAILED
        assert reloaded.finished_at is not None
        assert reloaded.error_code == "STAGE_FAILED"
        assert reloaded.error_message == "old error message"

        # 2. Simulate retry: _prepare_run_for_execution resets fields
        now_before = datetime.now(timezone.utc)
        AnalyticsRunner._prepare_run_for_execution(run, Maturity.PROVISIONAL)
        repository.update_analysis_run(run)

        # 3. Verify stale fields are cleared
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.status == RunStatus.RUNNING
        assert reloaded.maturity == Maturity.PROVISIONAL
        assert reloaded.started_at >= now_before
        assert reloaded.finished_at is None
        assert reloaded.error_code is None
        assert reloaded.error_message is None
        # run_id preserved
        assert reloaded.run_id == run.run_id

    def test_final_retry_resets_stale_fields(self, repository):
        """Test that retrying a failed FINAL run resets terminal fields."""
        from app.analytics.runner import AnalyticsRunner
        from app.analytics.models import RunStatus, Maturity

        # 1. Create a FAILED FINAL run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            maturity=Maturity.PROVISIONAL,
        )
        repository.create_analysis_run(run)

        # Simulate a failed FINAL attempt
        run.status = RunStatus.FAILED
        run.maturity = Maturity.FAILED
        run.started_at = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        run.finished_at = datetime(2026, 9, 11, 12, 20, tzinfo=timezone.utc)
        run.error_code = "FINAL_STAGE_FAILED"
        run.error_message = "final error message"
        repository.update_analysis_run(run)

        # Verify the failed state
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.finished_at is not None
        assert reloaded.error_code == "FINAL_STAGE_FAILED"

        # 2. Simulate retry
        now_before = datetime.now(timezone.utc)
        AnalyticsRunner._prepare_run_for_execution(run, Maturity.PROVISIONAL)
        repository.update_analysis_run(run)

        # 3. Verify stale fields are cleared
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.status == RunStatus.RUNNING
        assert reloaded.finished_at is None
        assert reloaded.error_code is None
        assert reloaded.error_message is None
        assert reloaded.started_at >= now_before

    def test_attempt_numbers_increment_on_retry(self, repository):
        """Test that stage attempt numbers continue incrementing on retry."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)

        # First attempt stages
        for attempt in range(1, 4):
            stage_run = AnalysisStageRun(
                run_id=run.run_id,
                stage_name="candle_reconciliation",
                attempt=attempt,
                status=StageStatus.SUCCEEDED,
            )
            repository.create_stage_run(stage_run)

        # Verify attempt numbers
        cursor = repository._conn.cursor()
        cursor.execute(
            "SELECT MAX(attempt) FROM analytics.analysis_stage_run WHERE run_id = %s AND stage_name = %s",
            (str(run.run_id), "candle_reconciliation"),
        )
        max_attempt = cursor.fetchone()[0]
        assert max_attempt == 3

    def test_provisional_retry_lifecycle_timestamps(self, repository):
        """Test that lifecycle_timestamps check passes after retry."""
        from app.analytics.runner import AnalyticsRunner
        from app.analytics.quality import DataQualityGate
        from app.analytics.models import RunStatus, Maturity, QualityCheckStatus

        # 1. Create a FAILED run with stale finished_at before started_at
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
            started_at=datetime(2026, 9, 11, 12, 45, tzinfo=timezone.utc),
            finished_at=datetime(2026, 9, 11, 12, 19, tzinfo=timezone.utc),  # before started_at!
        )
        repository.create_analysis_run(run)

        # 2. Simulate retry
        AnalyticsRunner._prepare_run_for_execution(run, Maturity.PROVISIONAL)
        repository.update_analysis_run(run)

        # 3. Verify lifecycle_timestamps check passes
        gate = DataQualityGate(repository)
        result = gate._check_lifecycle_timestamps(run, "quality_gate")
        assert result.status == QualityCheckStatus.PASS

    def test_retry_refreshes_observation_cutoff(self, repository):
        """Test that retry refreshes observation_cutoff to current time."""
        from app.analytics.runner import AnalyticsRunner
        import time

        old_cutoff = datetime(2026, 9, 11, 11, 0, tzinfo=timezone.utc)

        # 1. Create a run with an old observation_cutoff
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=old_cutoff,
        )
        repository.create_analysis_run(run)

        # Verify old cutoff is persisted
        reloaded = repository.get_analysis_run(run.run_id)
        assert reloaded.observation_cutoff == old_cutoff

        # 2. Simulate retry with a small delay to ensure time advances
        time.sleep(0.01)
        AnalyticsRunner._prepare_run_for_execution(run, Maturity.PROVISIONAL)
        repository.update_analysis_run(run)

        # 3. Verify observation_cutoff is refreshed
        reloaded = repository.get_analysis_run(run.run_id)
        reloaded_cutoff = reloaded.observation_cutoff
        # Convert to UTC for comparison
        if reloaded_cutoff.tzinfo is not None:
            reloaded_cutoff_utc = reloaded_cutoff.astimezone(timezone.utc)
        else:
            reloaded_cutoff_utc = reloaded_cutoff.replace(tzinfo=timezone.utc)
        
        # The cutoff should be different from (and after) the old value
        assert reloaded_cutoff_utc != old_cutoff, (
            f"observation_cutoff was not refreshed: {reloaded_cutoff_utc} == {old_cutoff}"
        )


class TestPostExitCoverageTimeframeNormalization:
    """Tests for post-exit coverage timeframe normalization."""

    def test_normalized_timeframe_matches_candle_table(self, repository, pg8000_conn):
        """Test that '5m' in paper_trade matches '5' in market.candle."""
        from app.analytics.quality import DataQualityGate
        from app.analytics.models import QualityCheckStatus, Severity, Maturity

        cursor = pg8000_conn.cursor()
        now = datetime.now(timezone.utc)
        closed_at = now - timedelta(hours=1)
        post_exit_end = closed_at + timedelta(hours=4)

        # 1. Insert instrument
        unique_sym = f"TFTEST1_{uuid4().hex[:8]}"
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            (unique_sym,),
        )
        instrument_id = cursor.fetchone()[0]

        # 2. Insert scanner_setup
        setup_id = f"setup_tf_{uuid4().hex[:8]}"
        signal_candle_ts = int((closed_at - timedelta(hours=1)).timestamp() * 1000)
        setup_time = closed_at - timedelta(hours=1)
        cursor.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, detected_at, reference_price, score,
                status, reasons, features, created_at, signal_candle_open_time, updated_at
            ) VALUES (
                %s, 'TEST_SCANNER', '1.0', %s,
                'LONG', '60', '5m', '5m',
                %s, %s, 50000.0, 0.8,
                'DETECTED', '[]', '{}', NOW(), %s, NOW()
            )
            """,
            (setup_id, instrument_id, setup_time, setup_time, signal_candle_ts),
        )

        # 3. Insert paper_trade with entry_timeframe = '5m' (production format)
        trade_id_1 = 910000 + int(uuid4().hex[:6], 16) % 100000
        cursor.execute(
            """
            INSERT INTO dds.paper_trade (
                trade_id, setup_id, symbol, scanner_name, direction,
                score, entry_price, entry_fee, stop_price, position_size,
                risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                duration_sec, balance_before, balance_after, entry_timeframe
            ) VALUES (
                %s, %s, %s, 'TEST_SCANNER', 'LONG',
                0.8, 50000.0, 10.0, 49000.0, 0.1,
                100.0, 51000.0, 'TAKE_PROFIT_1', 10.0, 100.0,
                1.0, 2.0, 0.1, 'CLOSED',
                %s, %s,
                14400, 10000.0, 10100.0, '5m'
            )
            """,
            (trade_id_1, setup_id, unique_sym, closed_at - timedelta(hours=5), closed_at),
        )

        # 4. Insert candles with timeframe = '5' (internal format)
        for i in range(48):
            candle_from = closed_at + timedelta(minutes=i * 5)
            candle_to = candle_from + timedelta(minutes=5)
            cursor.execute(
                """
                INSERT INTO market.candle (
                    exchange, market_type, instrument_id, timeframe,
                    open_time, close_time, open, high, low, close, volume,
                    is_closed, source, ingested_at, quality_status
                ) VALUES (
                    'bybit', 'linear', %s, '5',
                    %s, %s, 50000.0, 50100.0, 49900.0, 50050.0, 1000.0,
                    TRUE, 'test', NOW(), 'validated'
                )
                """,
                (instrument_id, candle_from, candle_to),
            )
        pg8000_conn.commit()

        # 5. Create analysis run and check coverage
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date.today(),
            analysis_from=now - timedelta(hours=6),
            analysis_to=now,
            observation_cutoff=now,
        )
        repository.create_analysis_run(run)

        gate = DataQualityGate(repository)
        result = gate._check_post_exit_coverage(run, "quality_gate")

        # With normalized timeframe, candles should be found
        assert result.status == QualityCheckStatus.PASS

    def test_final_rejects_incomplete_coverage(self, repository, pg8000_conn):
        """Test that FINAL rejects incomplete post-exit coverage."""
        from app.analytics.quality import DataQualityGate
        from app.analytics.models import QualityCheckStatus, Severity, Maturity

        cursor = pg8000_conn.cursor()
        now = datetime.now(timezone.utc)
        closed_at = now - timedelta(hours=1)

        # 1. Insert instrument
        unique_sym = f"TFTEST2_{uuid4().hex[:8]}"
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            (unique_sym,),
        )
        instrument_id = cursor.fetchone()[0]

        # 2. Insert scanner_setup
        setup_id = f"setup_tf2_{uuid4().hex[:8]}"
        signal_candle_ts = int((closed_at - timedelta(hours=1)).timestamp() * 1000)
        setup_time = closed_at - timedelta(hours=1)
        cursor.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, detected_at, reference_price, score,
                status, reasons, features, created_at, signal_candle_open_time, updated_at
            ) VALUES (
                %s, 'TEST_SCANNER', '1.0', %s,
                'LONG', '60', '5m', '5m',
                %s, %s, 50000.0, 0.8,
                'DETECTED', '[]', '{}', NOW(), %s, NOW()
            )
            """,
            (setup_id, instrument_id, setup_time, setup_time, signal_candle_ts),
        )

        # 3. Insert paper_trade — no candles will be created
        trade_id_2 = 920000 + int(uuid4().hex[:6], 16) % 100000
        cursor.execute(
            """
            INSERT INTO dds.paper_trade (
                trade_id, setup_id, symbol, scanner_name, direction,
                score, entry_price, entry_fee, stop_price, position_size,
                risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                duration_sec, balance_before, balance_after, entry_timeframe
            ) VALUES (
                %s, %s, %s, 'TEST_SCANNER', 'LONG',
                0.8, 50000.0, 10.0, 49000.0, 0.1,
                100.0, 51000.0, 'TAKE_PROFIT_1', 10.0, 100.0,
                1.0, 2.0, 0.1, 'CLOSED',
                %s, %s,
                14400, 10000.0, 10100.0, '5m'
            )
            """,
            (trade_id_2, setup_id, unique_sym, closed_at - timedelta(hours=5), closed_at),
        )
        pg8000_conn.commit()

        # 4. Create analysis run for FINAL
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date.today(),
            analysis_from=now - timedelta(hours=6),
            analysis_to=now,
            observation_cutoff=now,
            maturity=Maturity.FINAL,
        )
        repository.create_analysis_run(run)

        gate = DataQualityGate(repository)
        result = gate._check_post_exit_coverage(run, "quality_gate")

        # FINAL must FAIL when coverage is missing
        assert result.status == QualityCheckStatus.FAIL
        assert result.severity == Severity.BLOCKING
        assert result.affected_entity_count == 1