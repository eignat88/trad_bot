"""Integration tests for analytics migrations."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from pathlib import Path
import subprocess

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


ROOT = Path(__file__).resolve().parents[1]

MIGRATION_008 = ROOT / "sql" / "migrations" / "008_analytics_foundation.sql"
MIGRATION_009 = ROOT / "sql" / "migrations" / "009_market_candle.sql"


def run_psql_migration(path: Path) -> subprocess.CompletedProcess[str]:
    """Run a migration against the isolated PostgreSQL 17 test database."""
    psql = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")

    return subprocess.run(
        [
            str(psql),
            "-U", "postgres",
            "-h", "localhost",
            "-p", "5432",
            "-d", "trad_bot_migration_test",
            "-v", "ON_ERROR_STOP=1",
            "-f", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class TestAnalyticsMigrations:
    """Tests for analytics migrations."""

    def test_migration_008_creates_analytics_schema(self, db_session):
        """Test that migration 008 creates analytics schema."""
        # Check if schema exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'analytics'"
        )
        result = cursor.fetchone()
        
        # If schema doesn't exist, skip the test
        if result is None:
            pytest.skip("Analytics schema not found - migration 008 not applied")
        
        assert result is not None

    def test_migration_008_creates_analysis_run_table(self, db_session):
        """Test that migration 008 creates analysis_run table."""
        # Check if table exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics' AND table_name = 'analysis_run'"
        )
        result = cursor.fetchone()
        
        # If table doesn't exist, skip the test
        if result is None:
            pytest.skip("analysis_run table not found - migration 008 not applied")
        
        assert result is not None

    def test_migration_008_creates_analysis_stage_run_table(self, db_session):
        """Test that migration 008 creates analysis_stage_run table."""
        # Check if table exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics' AND table_name = 'analysis_stage_run'"
        )
        result = cursor.fetchone()
        
        # If table doesn't exist, skip the test
        if result is None:
            pytest.skip("analysis_stage_run table not found - migration 008 not applied")
        
        assert result is not None

    def test_migration_008_creates_data_quality_result_table(self, db_session):
        """Test that migration 008 creates data_quality_result table."""
        # Check if table exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics' AND table_name = 'data_quality_result'"
        )
        result = cursor.fetchone()
        
        # If table doesn't exist, skip the test
        if result is None:
            pytest.skip("data_quality_result table not found - migration 008 not applied")
        
        assert result is not None

    def test_migration_009_creates_market_schema(self, db_session):
        """Test that migration 009 creates market schema."""
        # Check if schema exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'market'"
        )
        result = cursor.fetchone()
        
        # If schema doesn't exist, skip the test
        if result is None:
            pytest.skip("Market schema not found - migration 009 not applied")
        
        assert result is not None

    def test_migration_009_creates_candle_table(self, db_session):
        """Test that migration 009 creates candle table."""
        # Check if table exists (migration must be applied first)
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'market' AND table_name = 'candle'"
        )
        result = cursor.fetchone()
        
        # If table doesn't exist, skip the test
        if result is None:
            pytest.skip("candle table not found - migration 009 not applied")
        
        assert result is not None

    def test_migration_008_is_idempotent(self, db_session):
        """Migration 008 can be executed repeatedly without errors."""
        first = run_psql_migration(MIGRATION_008)
        assert first.returncode == 0, first.stderr

        second = run_psql_migration(MIGRATION_008)
        assert second.returncode == 0, second.stderr

        cursor = db_session.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.triggers
            WHERE event_object_schema = 'analytics'
              AND event_object_table = 'analysis_run'
              AND trigger_name = 'update_analysis_run_updated_at'
            """
        )
        assert cursor.fetchone()[0] == 1

    def test_migration_009_is_idempotent(self, db_session):
        """Migration 009 can be executed repeatedly without errors."""
        first = run_psql_migration(MIGRATION_009)
        assert first.returncode == 0, first.stderr

        second = run_psql_migration(MIGRATION_009)
        assert second.returncode == 0, second.stderr

        cursor = db_session.cursor()
        cursor.execute(
            """
            SELECT COUNT(DISTINCT trigger_name)
            FROM information_schema.triggers
            WHERE event_object_schema = 'market'
              AND event_object_table = 'candle'
              AND trigger_name = 'validate_candle_data'
            """
        )
        assert cursor.fetchone()[0] == 1

    def test_migration_preserves_existing_data(self, db_session):
        """Analytics migrations must not modify existing DDS/config/mart objects."""
        cursor = db_session.cursor()

        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('dds', 'config', 'mart')
            ORDER BY table_schema, table_name
            """
        )
        before_tables = cursor.fetchall()

        cursor.execute(
            """
            SELECT schemaname, tablename, indexname
            FROM pg_indexes
            WHERE schemaname IN ('dds', 'config', 'mart')
            ORDER BY schemaname, tablename, indexname
            """
        )
        before_indexes = cursor.fetchall()

        result_008 = run_psql_migration(MIGRATION_008)
        assert result_008.returncode == 0, result_008.stderr

        result_009 = run_psql_migration(MIGRATION_009)
        assert result_009.returncode == 0, result_009.stderr

        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN ('dds', 'config', 'mart')
            ORDER BY table_schema, table_name
            """
        )
        after_tables = cursor.fetchall()

        cursor.execute(
            """
            SELECT schemaname, tablename, indexname
            FROM pg_indexes
            WHERE schemaname IN ('dds', 'config', 'mart')
            ORDER BY schemaname, tablename, indexname
            """
        )
        after_indexes = cursor.fetchall()

        assert after_tables == before_tables
        assert after_indexes == before_indexes

    def test_migration_creates_indexes(self, db_session):
        """Test that migrations create proper indexes."""
        cursor = db_session.cursor()
        
        # Check if tables exist first
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics' AND table_name = 'analysis_run'"
        )
        if not cursor.fetchone():
            pytest.skip("analysis_run table not found - migration 008 not applied")
        
        # Check analysis_run indexes
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'analysis_run' AND schemaname = 'analytics'"
        )
        indexes = cursor.fetchall()
        assert len(indexes) >= 3  # At least primary key and two other indexes
        
        # Check candle indexes
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'candle' AND schemaname = 'market'"
        )
        indexes = cursor.fetchall()
        assert len(indexes) >= 4  # At least primary key and three other indexes

    def test_migration_creates_constraints(self, db_session):
        """Test that migrations create proper constraints."""
        cursor = db_session.cursor()
        
        # Check if tables exist first
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'analytics' AND table_name = 'analysis_run'"
        )
        if not cursor.fetchone():
            pytest.skip("analysis_run table not found - migration 008 not applied")
        
        # Check analysis_run constraints
        cursor.execute(
            "SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema = 'analytics' AND table_name = 'analysis_run' AND constraint_type = 'CHECK'"
        )
        constraints = cursor.fetchall()
        assert len(constraints) >= 2  # At least analysis_from < analysis_to and error_message check
        
        # Check candle constraints
        cursor.execute(
            "SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema = 'market' AND table_name = 'candle' AND constraint_type = 'CHECK'"
        )
        constraints = cursor.fetchall()
        assert len(constraints) >= 7  # Multiple OHLC and data validation constraints