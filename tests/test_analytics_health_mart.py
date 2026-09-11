"""Integration tests for analytics health mart."""
from __future__ import annotations

import pytest
import subprocess
from pathlib import Path
from typing import Generator

import pg8000


ROOT = Path(__file__).resolve().parents[1]
HEALTH_MART = ROOT / "sql" / "mart" / "analytics_health.sql"


def run_psql_migration(path: Path, database: str = "trad_bot_migration_test") -> subprocess.CompletedProcess[str]:
    """Run a SQL file against the test database."""
    psql = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")
    
    return subprocess.run(
        [
            str(psql),
            "-U", "postgres",
            "-h", "localhost",
            "-p", "5432",
            "-d", database,
            "-v", "ON_ERROR_STOP=1",
            "-f", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture(scope="module")
def health_mart():
    """Apply analytics health mart and yield database connection."""
    # Apply health mart
    result = run_psql_migration(HEALTH_MART)
    assert result.returncode == 0, f"Health mart failed: {result.stderr}"
    
    # Connect to database
    conn = pg8000.connect(
        host="localhost",
        port=5432,
        database="trad_bot_migration_test",
        user="postgres",
        password="",
    )
    
    yield conn
    
    conn.close()


class TestAnalyticsHealthMart:
    """Tests for analytics health mart."""

    def test_health_mart_first_run(self):
        """Test that health mart can be applied successfully."""
        result = run_psql_migration(HEALTH_MART)
        assert result.returncode == 0, f"First run failed: {result.stderr}"

    def test_health_mart_second_run_idempotent(self):
        """Test that health mart is idempotent (can be run multiple times)."""
        # First run
        first = run_psql_migration(HEALTH_MART)
        assert first.returncode == 0, f"First run failed: {first.stderr}"
        
        # Second run
        second = run_psql_migration(HEALTH_MART)
        assert second.returncode == 0, f"Second run failed: {second.stderr}"

    def test_analytics_run_summary_view(self, health_mart):
        """Test that analytics_run_summary view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_run_summary")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_stage_performance_view(self, health_mart):
        """Test that analytics_stage_performance view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_stage_performance")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_quality_summary_view(self, health_mart):
        """Test that analytics_quality_summary view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_quality_summary")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_candle_stats_view(self, health_mart):
        """Test that analytics_candle_stats view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_candle_stats")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_pipeline_health_view(self, health_mart):
        """Test that analytics_pipeline_health view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_pipeline_health")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_unresolved_gaps_view(self, health_mart):
        """Test that analytics_unresolved_gaps view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_unresolved_gaps")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_kpis_view(self, health_mart):
        """Test that analytics_kpis view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_kpis")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_failed_runs_view(self, health_mart):
        """Test that analytics_failed_runs view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_failed_runs")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_quality_failures_view(self, health_mart):
        """Test that analytics_quality_failures view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_quality_failures")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_analytics_candle_coverage_view(self, health_mart):
        """Test that analytics_candle_coverage view works."""
        cursor = health_mart.cursor()
        
        # Query should succeed even with no data
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_candle_coverage")
        count = cursor.fetchone()[0]
        assert count >= 0

    def test_empty_market_candle_handling(self, health_mart):
        """Test that views work with empty market.candle table."""
        cursor = health_mart.cursor()
        
        # Temporarily truncate market.candle to test empty state
        cursor.execute("TRUNCATE market.candle CASCADE")
        health_mart.commit()
        
        try:
            # All views should still work
            cursor.execute("SELECT COUNT(*) FROM mart.analytics_run_summary")
            assert cursor.fetchone()[0] >= 0
            
            cursor.execute("SELECT COUNT(*) FROM mart.analytics_unresolved_gaps")
            assert cursor.fetchone()[0] >= 0
            
            cursor.execute("SELECT COUNT(*) FROM mart.analytics_candle_coverage")
            assert cursor.fetchone()[0] >= 0
            
        finally:
            # Restore data (this will be empty, but that's okay)
            health_mart.rollback()

    def test_views_contain_correct_columns(self, health_mart):
        """Test that all views have expected columns."""
        cursor = health_mart.cursor()
        
        # Check analytics_run_summary columns
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'mart' 
              AND table_name = 'analytics_run_summary'
            ORDER BY ordinal_position
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        assert 'run_id' in columns
        assert 'business_date' in columns
        assert 'maturity' in columns
        assert 'status' in columns
        
        # Check analytics_unresolved_gaps columns
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'mart' 
              AND table_name = 'analytics_unresolved_gaps'
            ORDER BY ordinal_position
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        assert 'instrument_id' in columns
        assert 'timeframe' in columns
        assert 'gap_count' in columns
        assert 'total_gap_minutes' in columns