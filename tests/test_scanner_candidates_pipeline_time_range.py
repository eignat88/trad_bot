"""Integration tests for scanner_candidates_pipeline time-range fix."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pg8000
import pytest

from conftest import connect_test_db, run_psql_file, psql_args


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_045 = ROOT / "sql" / "migrations" / "045_scanner_candidates_pipeline_time_range.sql"
SCHEMA_SQL = ROOT / "app" / "db" / "schema.sql"
VISIBILITY_SQL = ROOT / "sql" / "migrations" / "006_scanner_grafana_visibility_separate_table.sql"


def run_psql_file_safe(path: Path) -> subprocess.CompletedProcess[str]:
    return run_psql_file(path)


@pytest.fixture(scope="module")
def db_conn():
    conn = connect_test_db()
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def apply_schema_and_migration():
    result = run_psql_file_safe(SCHEMA_SQL)
    assert result.returncode == 0, f"Schema failed: {result.stderr}"
    result = run_psql_file_safe(VISIBILITY_SQL)
    assert result.returncode == 0, f"Visibility migration failed: {result.stderr}"
    result = run_psql_file_safe(MIGRATION_045)
    assert result.returncode == 0, f"Migration 045 failed: {result.stderr}"


def _insert_run(cursor, run_id, status, finished_at, started_at=None, symbols_total=50):
    if started_at is None:
        started_at = finished_at - timedelta(minutes=5)
    cursor.execute(
        "INSERT INTO dds.scanner_run (run_id, status, started_at, finished_at, symbols_total) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, "
        "started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at",
        (run_id, status, started_at, finished_at, symbols_total),
    )


def _insert_stat(cursor, run_id, scanner_name, symbols=50, candidates=0, setups=0, errors=0):
    cursor.execute(
        "INSERT INTO dds.scanner_run_stat "
        "(run_id, scanner_name, symbols_scanned, candidates_found, setups_saved, duration_ms, errors_count) "
        "VALUES (%s, %s, %s, %s, %s, 100.0, %s) "
        "ON CONFLICT (run_id, scanner_name) DO UPDATE SET "
        "symbols_scanned = EXCLUDED.symbols_scanned, "
        "candidates_found = EXCLUDED.candidates_found, "
        "setups_saved = EXCLUDED.setups_saved",
        (run_id, scanner_name, symbols, candidates, setups, errors),
    )


def _cleanup_run(cursor, run_id):
    cursor.execute("DELETE FROM dds.scanner_run_stat WHERE run_id = %s", (run_id,))
    cursor.execute("DELETE FROM dds.scanner_run WHERE run_id = %s", (run_id,))


class TestMigrationIdempotent:
    def test_first_run(self):
        result = run_psql_file_safe(MIGRATION_045)
        assert result.returncode == 0, f"First run failed: {result.stderr}"

    def test_second_run(self):
        result = run_psql_file_safe(MIGRATION_045)
        assert result.returncode == 0, f"Second run failed: {result.stderr}"


class TestViewExists:
    def test_pipeline_view_exists(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'mart' AND viewname = 'scanner_candidates_pipeline'"
        )
        assert cursor.fetchone() is not None, "mart.scanner_candidates_pipeline view not found"

    def test_latest_view_exists(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'mart' AND viewname = 'scanner_candidates_latest'"
        )
        assert cursor.fetchone() is not None, "mart.scanner_candidates_latest view not found"


class TestMultipleRuns:
    def test_two_completed_runs_both_visible(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_A, RUN_B = 9000001, 9000002

        try:
            _insert_run(cursor, RUN_A, "COMPLETED", now - timedelta(hours=1),
                        started_at=now - timedelta(hours=2))
            _insert_run(cursor, RUN_B, "COMPLETED", now,
                        started_at=now - timedelta(minutes=30))
            _insert_stat(cursor, RUN_A, "SCANNER_A", symbols=50, candidates=3, setups=1)
            _insert_stat(cursor, RUN_B, "SCANNER_A", symbols=50, candidates=7, setups=2)
            db_conn.commit()

            cursor.execute(
                "SELECT run_id, symbols, candidates, setups "
                "FROM mart.scanner_candidates_pipeline "
                "WHERE scanner_name = 'SCANNER_A' ORDER BY run_id"
            )
            rows = cursor.fetchall()
            assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
            assert rows[0][0] == RUN_A
            assert rows[0][1] == 50
            assert rows[0][2] == 3
            assert rows[0][3] == 1
            assert rows[1][0] == RUN_B
            assert rows[1][1] == 50
            assert rows[1][2] == 7
            assert rows[1][3] == 2
        finally:
            _cleanup_run(cursor, RUN_A)
            _cleanup_run(cursor, RUN_B)
            db_conn.commit()


class TestGrafanaAggregation:
    def test_aggregation_correct(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_A, RUN_B = 9000003, 9000004

        try:
            _insert_run(cursor, RUN_A, "COMPLETED", now - timedelta(hours=1),
                        started_at=now - timedelta(hours=2))
            _insert_run(cursor, RUN_B, "COMPLETED", now,
                        started_at=now - timedelta(minutes=30))
            _insert_stat(cursor, RUN_A, "SCANNER_AGG", symbols=50, candidates=3, setups=1)
            _insert_stat(cursor, RUN_B, "SCANNER_AGG", symbols=50, candidates=7, setups=2)
            db_conn.commit()

            cursor.execute(
                """
                SELECT
                    scanner_name,
                    MAX(symbols) AS symbols,
                    SUM(candidates) AS candidates,
                    SUM(setups) AS setups,
                    CASE
                        WHEN SUM(candidates) > 0
                        THEN ROUND(100.0 * SUM(setups)::numeric / SUM(candidates)::numeric, 1)
                        ELSE 0
                    END AS conversion_pct
                FROM mart.scanner_candidates_pipeline
                WHERE scanner_name = 'SCANNER_AGG'
                GROUP BY scanner_name
                """
            )
            row = cursor.fetchone()
            assert row is not None, "No rows returned"
            _, symbols, candidates, setups, conversion_pct = row
            assert symbols == 50, f"Expected MAX(symbols)=50, got {symbols}"
            assert candidates == 10, f"Expected SUM(candidates)=10, got {candidates}"
            assert setups == 3, f"Expected SUM(setups)=3, got {setups}"
            assert float(conversion_pct) == 30.0, f"Expected conversion=30.0%, got {conversion_pct}"
        finally:
            _cleanup_run(cursor, RUN_A)
            _cleanup_run(cursor, RUN_B)
            db_conn.commit()


class TestNonCompletedRunsExcluded:
    def test_running_excluded(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_ID = 9000005

        try:
            _insert_run(cursor, RUN_ID, "RUNNING", finished_at=now, started_at=now)
            _insert_stat(cursor, RUN_ID, "SCANNER_EXCL", symbols=50, candidates=10, setups=5)
            db_conn.commit()

            cursor.execute(
                "SELECT COUNT(*) FROM mart.scanner_candidates_pipeline WHERE run_id = %s",
                (RUN_ID,),
            )
            count = cursor.fetchone()[0]
            assert count == 0, f"Expected 0 rows for RUNNING run, got {count}"
        finally:
            _cleanup_run(cursor, RUN_ID)
            db_conn.commit()

    def test_failed_excluded(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_ID = 9000006

        try:
            _insert_run(cursor, RUN_ID, "FAILED", finished_at=now, started_at=now)
            _insert_stat(cursor, RUN_ID, "SCANNER_EXCL", symbols=50, candidates=10, setups=5, errors=1)
            db_conn.commit()

            cursor.execute(
                "SELECT COUNT(*) FROM mart.scanner_candidates_pipeline WHERE run_id = %s",
                (RUN_ID,),
            )
            count = cursor.fetchone()[0]
            assert count == 0, f"Expected 0 rows for FAILED run, got {count}"
        finally:
            _cleanup_run(cursor, RUN_ID)
            db_conn.commit()


class TestLatestView:
    def test_latest_view_returns_only_newest_run(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_OLD, RUN_NEW = 9000007, 9000008

        try:
            _insert_run(cursor, RUN_OLD, "COMPLETED", now - timedelta(hours=3),
                        started_at=now - timedelta(hours=4))
            _insert_stat(cursor, RUN_OLD, "SCANNER_LATEST", symbols=50, candidates=10, setups=5)

            _insert_run(cursor, RUN_NEW, "COMPLETED", now,
                        started_at=now - timedelta(minutes=5))
            _insert_stat(cursor, RUN_NEW, "SCANNER_LATEST", symbols=50, candidates=20, setups=8)
            db_conn.commit()

            cursor.execute(
                "SELECT symbols, candidates, setups, conversion_pct "
                "FROM mart.scanner_candidates_latest WHERE scanner_name = 'SCANNER_LATEST'"
            )
            rows = cursor.fetchall()
            assert len(rows) == 1, f"Expected 1 row in latest view, got {len(rows)}"
            assert rows[0][1] == 20, f"Expected candidates=20, got {rows[0][1]}"
            assert rows[0][2] == 8, f"Expected setups=8, got {rows[0][2]}"
            assert float(rows[0][3]) == 40.0, f"Expected conversion=40.0%, got {rows[0][3]}"
        finally:
            _cleanup_run(cursor, RUN_OLD)
            _cleanup_run(cursor, RUN_NEW)
            db_conn.commit()


class TestZeroCandidates:
    def test_zero_candidates_conversion_zero(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_ID = 9000009

        try:
            _insert_run(cursor, RUN_ID, "COMPLETED", now)
            _insert_stat(cursor, RUN_ID, "SCANNER_ZERO", symbols=50, candidates=0, setups=0)
            db_conn.commit()

            cursor.execute(
                "SELECT candidates, setups FROM mart.scanner_candidates_pipeline WHERE run_id = %s",
                (RUN_ID,),
            )
            row = cursor.fetchone()
            assert row is not None, "Row not found"
            assert row[0] == 0
            assert row[1] == 0

            cursor.execute(
                """
                SELECT SUM(candidates), SUM(setups),
                    CASE
                        WHEN SUM(candidates) > 0
                        THEN ROUND(100.0 * SUM(setups)::numeric / SUM(candidates)::numeric, 1)
                        ELSE 0
                    END AS conversion_pct
                FROM mart.scanner_candidates_pipeline
                WHERE scanner_name = 'SCANNER_ZERO'
                """
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0
            assert row[1] == 0
            assert float(row[2]) == 0.0, f"Expected conversion=0.0%, got {row[2]}"
        finally:
            _cleanup_run(cursor, RUN_ID)
            db_conn.commit()

    def test_latest_view_zero_candidates(self, db_conn, apply_schema_and_migration):
        cursor = db_conn.cursor()
        now = datetime.now(tz=timezone.utc)
        RUN_ID = 9000010

        try:
            _insert_run(cursor, RUN_ID, "COMPLETED", now)
            _insert_stat(cursor, RUN_ID, "SCANNER_ZERO_LATEST", symbols=50, candidates=0, setups=0)
            db_conn.commit()

            cursor.execute(
                "SELECT candidates, setups, conversion_pct "
                "FROM mart.scanner_candidates_latest WHERE scanner_name = 'SCANNER_ZERO_LATEST'"
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0
            assert row[1] == 0
            assert float(row[2]) == 0.0, f"Expected conversion=0.0%, got {row[2]}"
        finally:
            _cleanup_run(cursor, RUN_ID)
            db_conn.commit()
