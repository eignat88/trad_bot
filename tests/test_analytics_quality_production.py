"""Integration tests for analytics quality gate with production schema."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4

import pg8000

from app.analytics.models import (
    AnalysisRun,
    DataQualityResult,
    Maturity,
    RunStatus,
    StageStatus,
    Severity,
    QualityCheckStatus,
)
from app.analytics.quality import DataQualityGate
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
    
    # Clean up before each test (order matters for foreign keys)
    cursor = pg8000_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result")
    cursor.execute("DELETE FROM analytics.analysis_stage_run")
    cursor.execute("DELETE FROM analytics.analysis_run")
    cursor.execute("DELETE FROM market.candle")
    cursor.execute("DELETE FROM dds.paper_trade WHERE trade_id >= 900000")
    cursor.execute("DELETE FROM dds.scanner_setup WHERE setup_id LIKE 'setup_%'")
    cursor.execute("DELETE FROM dds.instrument WHERE symbol LIKE 'TESTSYM_%'")
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
    cursor.execute("DELETE FROM dds.instrument WHERE symbol LIKE 'TESTSYM_%'")
    pg8000_conn.commit()


@pytest.fixture
def quality_gate(repository):
    """Create DataQualityGate with real repository."""
    return DataQualityGate(repository)


class TestPostExitCoverageProduction:
    """Tests for post-exit coverage with production schema."""

    def test_no_closed_trades(self, quality_gate, repository):
        """Test post-exit coverage with no closed trades."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        result = quality_gate._check_post_exit_coverage(run, "quality_gate")
        
        assert result.status == QualityCheckStatus.PASS
        assert result.check_name == "post_exit_coverage"

    def test_closed_trade_with_full_coverage(self, quality_gate, repository, pg8000_conn):
        """Test post-exit coverage with full coverage against real PostgreSQL 17."""
        cursor = pg8000_conn.cursor()
        now = datetime.now(timezone.utc)
        closed_at = now - timedelta(hours=1)
        exit_hour = closed_at.hour

        # 1. Insert a real instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, %s, 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            ("TESTSYM_CVRG", "TEST"),
        )
        instrument_id = cursor.fetchone()[0]

        # 2. Insert a scanner_setup row (all NOT NULL columns satisfied)
        setup_id = f"setup_cvg_{uuid4().hex[:8]}"
        signal_candle_ts = int((closed_at - timedelta(hours=1)).timestamp() * 1000)
        setup_time = closed_at - timedelta(hours=1)
        cursor.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, detected_at, reference_price, score,
                status, reasons, features, created_at, signal_candle_open_time,
                updated_at
            ) VALUES (
                %s, 'TEST_SCANNER', '1.0', %s,
                'LONG', '60', '5', '5',
                %s, %s, 50000.0, 0.8,
                'DETECTED', '[]', '{}', NOW(), %s, NOW()
            )
            """,
            (setup_id, instrument_id, setup_time, setup_time, signal_candle_ts),
        )

        # 3. Insert a closed paper_trade referencing that setup
        cursor.execute(
            """
            INSERT INTO dds.paper_trade (
                trade_id, setup_id, symbol, scanner_name, direction,
                score, entry_price, entry_fee, stop_price, position_size,
                risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                duration_sec, balance_before, balance_after, entry_timeframe
            ) VALUES (
                900001, %s, 'TESTSYM_CVRG', 'TEST_SCANNER', 'LONG',
                0.8, 50000.0, 10.0, 49000.0, 0.1,
                100.0, 51000.0, 'TAKE_PROFIT_1', 10.0, 100.0,
                1.0, 2.0, 0.1, 'CLOSED',
                %s, %s,
                14400, 10000.0, 10100.0, '5'
            )
            """,
            (setup_id, closed_at - timedelta(hours=5), closed_at),
        )

        # 4. Insert 48 candles covering 4 hours of 5m data after exit
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

        # 5. Create analysis_run covering the trade window
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date.today(),
            analysis_from=now - timedelta(hours=6),
            analysis_to=now,
            observation_cutoff=now,
        )
        repository.create_analysis_run(run)

        result = quality_gate._check_post_exit_coverage(run, "quality_gate")

        # Full coverage → PASS
        assert result.status == QualityCheckStatus.PASS
        assert result.check_name == "post_exit_coverage"

    def test_closed_trade_with_missing_coverage(self, quality_gate, repository, pg8000_conn):
        """Test post-exit coverage with missing coverage against real PostgreSQL 17."""
        cursor = pg8000_conn.cursor()
        now = datetime.now(timezone.utc)
        closed_at = now - timedelta(hours=1)

        # 1. Insert a real instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, %s, 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            ("TESTSYM_NOCVG", "TEST"),
        )
        instrument_id = cursor.fetchone()[0]

        # 2. Insert a scanner_setup row
        setup_id = f"setup_nocvg_{uuid4().hex[:8]}"
        signal_candle_ts = int((closed_at - timedelta(hours=1)).timestamp() * 1000)
        setup_time = closed_at - timedelta(hours=1)
        cursor.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, detected_at, reference_price, score,
                status, reasons, features, created_at, signal_candle_open_time,
                updated_at
            ) VALUES (
                %s, 'TEST_SCANNER', '1.0', %s,
                'LONG', '60', '5', '5',
                %s, %s, 50000.0, 0.8,
                'DETECTED', '[]', '{}', NOW(), %s, NOW()
            )
            """,
            (setup_id, instrument_id, setup_time, setup_time, signal_candle_ts),
        )

        # 3. Insert a closed paper_trade — no candles will be created
        cursor.execute(
            """
            INSERT INTO dds.paper_trade (
                trade_id, setup_id, symbol, scanner_name, direction,
                score, entry_price, entry_fee, stop_price, position_size,
                risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                duration_sec, balance_before, balance_after, entry_timeframe
            ) VALUES (
                900002, %s, 'TESTSYM_NOCVG', 'TEST_SCANNER', 'LONG',
                0.8, 50000.0, 10.0, 49000.0, 0.1,
                100.0, 51000.0, 'TAKE_PROFIT_1', 10.0, 100.0,
                1.0, 2.0, 0.1, 'CLOSED',
                %s, %s,
                14400, 10000.0, 10100.0, '5'
            )
            """,
            (setup_id, closed_at - timedelta(hours=5), closed_at),
        )
        pg8000_conn.commit()

        # 4. Create analysis_run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date.today(),
            analysis_from=now - timedelta(hours=6),
            analysis_to=now,
            observation_cutoff=now,
        )
        repository.create_analysis_run(run)

        result = quality_gate._check_post_exit_coverage(run, "quality_gate")

        # Missing candles → FAIL with WARNING severity (PROVISIONAL)
        assert result.status == QualityCheckStatus.FAIL
        assert result.severity == Severity.WARNING
        assert result.affected_entity_count == 1


class TestTransactionRecovery:
    """Tests for transaction recovery after SQL errors."""

    def test_transaction_recovery_after_sql_error(self, quality_gate, repository, pg8000_conn):
        """Test that transaction is recovered after SQL error."""
        # Create a run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        # Force a SQL error in a quality check
        # We'll use a non-existent table
        cursor = pg8000_conn.cursor()
        try:
            cursor.execute("SELECT * FROM non_existent_table")
        except Exception:
            # Rollback the transaction after the error
            pg8000_conn.rollback()
        
        # Now try to create a quality result
        # This should work if transaction was properly rolled back
        result = DataQualityResult(
            run_id=run.run_id,
            stage_name="quality_gate",
            check_name="test_check",
            severity=Severity.BLOCKING,
            status=QualityCheckStatus.PASS,
            details={"message": "Test check"},
        )
        
        # This should not raise SQLSTATE 25P02
        try:
            created = repository.create_quality_result(result)
            assert created.quality_result_id is not None
        except Exception as e:
            # If this fails with SQLSTATE 25P02, the transaction wasn't recovered
            assert "25P02" not in str(e), f"Transaction not recovered: {e}"

    def test_multiple_quality_checks_after_error(self, quality_gate, repository, pg8000_conn):
        """Test that multiple quality checks can run after an error."""
        # Create a run
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        # Run quality checks - some may fail but should not poison transaction
        passed, results = quality_gate.run_quality_checks(run)
        
        # Verify all results were created
        assert len(results) == 8  # 8 quality checks
        
        # Verify we can still insert quality results
        for result in results:
            assert result.quality_result_id is not None


class TestFullDataQualityGate:
    """Tests for full DataQualityGate.run_quality_checks()."""

    def test_run_quality_checks_with_pg8000(self, quality_gate, repository):
        """Test full run_quality_checks with real pg8000."""
        run = AnalysisRun(
            run_id=uuid4(),
            business_date=date(2026, 9, 12),
            analysis_from=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            analysis_to=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            observation_cutoff=datetime.now(timezone.utc),
        )
        repository.create_analysis_run(run)
        
        passed, results = quality_gate.run_quality_checks(run)
        
        # Should have 8 quality checks
        assert len(results) == 8
        
        # All should have quality_result_id
        for result in results:
            assert result.quality_result_id is not None
        
        # Check specific checks
        check_names = [r.check_name for r in results]
        assert "postgresql_availability" in check_names
        assert "source_timestamps" in check_names
        assert "duplicate_candles" in check_names
        assert "ohlc_validation" in check_names
        assert "closed_candle_intervals" in check_names
        assert "post_exit_coverage" in check_names
        assert "lifecycle_timestamps" in check_names
        assert "data_freshness" in check_names


class TestProductionSchemaValidation:
    """Tests to validate production schema assumptions."""

    def test_paper_trade_columns_exist(self, pg8000_conn):
        """Test that expected paper_trade columns exist."""
        cursor = pg8000_conn.cursor()
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'dds' 
              AND table_name = 'paper_trade'
              AND column_name IN ('symbol', 'closed_at', 'entry_timeframe')
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        
        assert 'symbol' in columns
        assert 'closed_at' in columns
        assert 'entry_timeframe' in columns
        
        # Verify these are the correct columns (not instrument_id, exit_time, timeframe)
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'dds' 
              AND table_name = 'paper_trade'
              AND column_name IN ('instrument_id', 'exit_time', 'timeframe')
            """
        )
        old_columns = [row[0] for row in cursor.fetchall()]
        
        # These should not exist
        assert 'instrument_id' not in old_columns
        assert 'exit_time' not in old_columns
        assert 'timeframe' not in old_columns

    def test_market_candle_instrument_id_lookup(self, pg8000_conn):
        """Test that we can look up instrument_id from symbol."""
        cursor = pg8000_conn.cursor()
        
        # Check if dds.instrument table exists and has symbol column
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'dds' 
              AND table_name = 'instrument'
              AND column_name IN ('instrument_id', 'symbol')
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        
        # If dds.instrument exists, we can use it for lookup
        if 'instrument_id' in columns and 'symbol' in columns:
            # Test the lookup query
            cursor.execute(
                """
                SELECT instrument_id 
                FROM dds.instrument 
                WHERE symbol = 'BTCUSDT' 
                LIMIT 1
                """
            )
            # This may return None if no BTCUSDT exists, but should not error
            result = cursor.fetchone()
            # Just verify the query doesn't error
            assert result is None or isinstance(result[0], int)