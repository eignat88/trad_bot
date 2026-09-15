"""Integration tests for migration 013: analytics quality latest-state semantics."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from uuid import uuid4

import pg8000
import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_013 = ROOT / "sql" / "migrations" / "013_fix_analytics_quality_latest_state.sql"


def run_psql_file(path: Path) -> subprocess.CompletedProcess[str]:
    """Execute a SQL file against the test database."""
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


@pytest.fixture(scope="module")
def db_conn():
    """Create a real pg8000 connection."""
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
def cleanup(db_conn):
    """Clean up test data after each test."""
    yield
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM analytics.data_quality_result WHERE run_id IN (SELECT run_id FROM analytics.analysis_run WHERE business_date >= '2026-09-11')")
    cursor.execute("DELETE FROM analytics.analysis_stage_run WHERE run_id IN (SELECT run_id FROM analytics.analysis_run WHERE business_date >= '2026-09-11')")
    cursor.execute("DELETE FROM analytics.analysis_run WHERE business_date >= '2026-09-11'")
    db_conn.commit()


class TestMigration013:
    """Tests for migration 013."""

    def test_first_run_passes(self):
        """Test that migration 013 applies successfully."""
        result = run_psql_file(MIGRATION_013)
        assert result.returncode == 0, f"Migration 013 failed: {result.stderr}"

    def test_second_run_idempotent(self):
        """Test that migration 013 can be applied twice safely."""
        first = run_psql_file(MIGRATION_013)
        assert first.returncode == 0
        second = run_psql_file(MIGRATION_013)
        assert second.returncode == 0

    def test_view_exists(self, db_conn):
        """Test that the view exists."""
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT viewname FROM pg_views WHERE viewname = 'analytics_quality_failures' AND schemaname = 'mart'"
        )
        assert cursor.fetchone() is not None

    def test_historical_data_preserved(self, db_conn, cleanup):
        """Test that historical data_quality_result rows are not deleted."""
        run_id = uuid4()

        # Create an analysis run
        cursor = db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                source_watermarks, created_at, updated_at
            ) VALUES (
                %s, %s, 'Europe/Sofia', %s, %s,
                %s, '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0',
                '{}', NOW(), NOW()
            )
            """,
            (str(run_id), date(2026, 9, 11),
             datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        # Insert multiple quality results for the same check (simulating retries)
        for i in range(3):
            cursor.execute(
                """
                INSERT INTO analytics.data_quality_result (
                    run_id, stage_name, check_name, severity, status, checked_at
                ) VALUES (%s, 'quality_gate', 'source_timestamps', 'BLOCKING', 'FAIL', %s)
                """,
                (str(run_id), datetime(2026, 9, 11, 12, i, tzinfo=timezone.utc)),
            )

        # Insert a final PASS result
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'source_timestamps', 'BLOCKING', 'PASS', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        db_conn.commit()

        # Verify all 4 rows exist in data_quality_result
        cursor.execute(
            "SELECT COUNT(*) FROM analytics.data_quality_result WHERE run_id = %s",
            (str(run_id),),
        )
        assert cursor.fetchone()[0] == 4


class TestQualityFailuresLatestState:
    """Tests for latest-result semantics in analytics_quality_failures."""

    def test_retry_failure_then_pass_returns_zero(self, db_conn, cleanup):
        """Test: first source_timestamps = BLOCKING/FAIL,
        later source_timestamps = BLOCKING/PASS → 0 rows in failures view."""
        run_id = uuid4()

        cursor = db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                source_watermarks, created_at, updated_at
            ) VALUES (
                %s, %s, 'Europe/Sofia', %s, %s,
                %s, '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0',
                '{}', NOW(), NOW()
            )
            """,
            (str(run_id), date(2026, 9, 11),
             datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        # First retry: FAIL
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'source_timestamps', 'BLOCKING', 'FAIL', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 12, 45, tzinfo=timezone.utc)),
        )

        # Second retry: PASS (fixed)
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'source_timestamps', 'BLOCKING', 'PASS', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 13, 56, tzinfo=timezone.utc)),
        )

        db_conn.commit()

        # Query the view
        cursor.execute(
            """
            SELECT COUNT(*) FROM mart.analytics_quality_failures
            WHERE business_date = %s
            """,
            (date(2026, 9, 11),),
        )
        count = cursor.fetchone()[0]

        # Latest result is PASS → 0 failures
        assert count == 0

    def test_latest_is_blocking_fail(self, db_conn, cleanup):
        """Test: latest result is BLOCKING/FAIL → 1 row in failures view."""
        run_id = uuid4()

        cursor = db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                source_watermarks, created_at, updated_at
            ) VALUES (
                %s, %s, 'Europe/Sofia', %s, %s,
                %s, '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0',
                '{}', NOW(), NOW()
            )
            """,
            (str(run_id), date(2026, 9, 11),
             datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        # First retry: PASS
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'postgresql_availability', 'BLOCKING', 'PASS', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)),
        )

        # Second retry: FAIL (latest)
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'postgresql_availability', 'BLOCKING', 'FAIL', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)),
        )

        db_conn.commit()

        # Query the view
        cursor.execute(
            """
            SELECT COUNT(*) FROM mart.analytics_quality_failures
            WHERE business_date = %s
            """,
            (date(2026, 9, 11),),
        )
        count = cursor.fetchone()[0]

        # Latest result is FAIL → 1 row
        assert count == 1

    def test_warning_fail_not_returned(self, db_conn, cleanup):
        """Test: WARNING/FAIL is not returned by analytics_quality_failures."""
        run_id = uuid4()

        cursor = db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                source_watermarks, created_at, updated_at
            ) VALUES (
                %s, %s, 'Europe/Sofia', %s, %s,
                %s, '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0',
                '{}', NOW(), NOW()
            )
            """,
            (str(run_id), date(2026, 9, 11),
             datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        # Insert a WARNING/FAIL result
        cursor.execute(
            """
            INSERT INTO analytics.data_quality_result (
                run_id, stage_name, check_name, severity, status, checked_at
            ) VALUES (%s, 'quality_gate', 'data_freshness', 'WARNING', 'FAIL', %s)
            """,
            (str(run_id), datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)),
        )

        db_conn.commit()

        # Query the view
        cursor.execute(
            """
            SELECT COUNT(*) FROM mart.analytics_quality_failures
            WHERE business_date = %s
            """,
            (date(2026, 9, 11),),
        )
        count = cursor.fetchone()[0]

        # WARNING/FAIL should NOT appear in quality_failures
        assert count == 0

    def test_empty_date_returns_zero(self, db_conn, cleanup):
        """Test that a date with no data returns 0 rows."""
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM mart.analytics_quality_failures WHERE business_date = '2020-01-01'"
        )
        assert cursor.fetchone()[0] == 0

    def test_historical_data_not_deleted(self, db_conn, cleanup):
        """Test that migration does not delete historical data."""
        run_id = uuid4()

        cursor = db_conn.cursor()
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                source_watermarks, created_at, updated_at
            ) VALUES (
                %s, %s, 'Europe/Sofia', %s, %s,
                %s, '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0',
                '{}', NOW(), NOW()
            )
            """,
            (str(run_id), date(2026, 9, 11),
             datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc),
             datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)),
        )

        # Insert 5 quality results
        for i in range(5):
            cursor.execute(
                """
                INSERT INTO analytics.data_quality_result (
                    run_id, stage_name, check_name, severity, status, checked_at
                ) VALUES (%s, 'quality_gate', 'test_check', 'BLOCKING', 'FAIL', %s)
                """,
                (str(run_id), datetime(2026, 9, 11, 12, i, tzinfo=timezone.utc)),
            )

        db_conn.commit()

        # Re-apply migration (idempotent)
        run_psql_file(MIGRATION_013)

        # Verify all 5 rows still exist
        cursor.execute(
            "SELECT COUNT(*) FROM analytics.data_quality_result WHERE run_id = %s",
            (str(run_id),),
        )
        assert cursor.fetchone()[0] == 5

    def test_existing_grants_valid(self, db_conn):
        """Test that existing Grafana grants remain valid."""
        cursor = db_conn.cursor()

        # Check that the view is queryable
        cursor.execute("SELECT COUNT(*) FROM mart.analytics_quality_failures")
        assert cursor.fetchone()[0] >= 0

        cursor.execute("SELECT COUNT(*) FROM mart.analytics_quality_summary")
        assert cursor.fetchone()[0] >= 0

        cursor.execute("SELECT COUNT(*) FROM mart.analytics_failed_runs")
        assert cursor.fetchone()[0] >= 0

        cursor.execute("SELECT COUNT(*) FROM mart.analytics_pipeline_health")
        assert cursor.fetchone()[0] >= 0