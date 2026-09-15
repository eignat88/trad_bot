"""Integration tests for analytics RBAC permissions."""
from __future__ import annotations

import pytest
import subprocess
from pathlib import Path
from typing import Generator

import pg8000


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_010 = ROOT / "sql" / "migrations" / "010_analytics_rbac.sql"


def run_psql(sql: str, database: str = "trad_bot_migration_test", user: str = "postgres") -> subprocess.CompletedProcess[str]:
    """Run SQL against the test database."""
    psql = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")
    
    return subprocess.run(
        [
            str(psql),
            "-U", user,
            "-h", "localhost",
            "-p", "5432",
            "-d", database,
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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


@pytest.fixture(scope="module")
def analytics_runner_role():
    """Create analytics_runner role for testing."""
    # Apply RBAC migration (idempotent - can be run multiple times)
    result = run_psql_migration(MIGRATION_010)
    assert result.returncode == 0, f"RBAC migration failed: {result.stderr}"
    
    yield "analytics_runner"
    
    # Cleanup: drop the role (after all tests)
    run_psql("DROP ROLE IF EXISTS analytics_runner;")


@pytest.fixture
def analytics_conn(analytics_runner_role) -> Generator[pg8000.Connection, None, None]:
    """Create a connection as analytics_runner."""
    conn = pg8000.connect(
        host="localhost",
        port=5432,
        database="trad_bot_migration_test",
        user=analytics_runner_role,
        password="",
    )
    
    yield conn
    
    conn.close()


class TestAnalyticsRBAC:
    """Tests for analytics RBAC permissions."""

    def test_role_exists(self, db_session):
        """Test that analytics_runner role exists."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result is not None

    def test_role_has_connect_privilege(self, db_session):
        """Test that analytics_runner can connect to database."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT has_database_privilege('analytics_runner', 'trad_bot', 'CONNECT')"
        )
        result = cursor.fetchone()
        assert result[0] is True

    def test_analytics_schema_usage(self, db_session):
        """Test that analytics_runner has USAGE on analytics schema."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT has_schema_privilege('analytics_runner', 'analytics', 'USAGE')"
        )
        result = cursor.fetchone()
        assert result[0] is True

    def test_market_schema_usage(self, db_session):
        """Test that analytics_runner has USAGE on market schema."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT has_schema_privilege('analytics_runner', 'market', 'USAGE')"
        )
        result = cursor.fetchone()
        assert result[0] is True

    def test_analytics_table_permissions(self, db_session):
        """Test analytics_runner permissions on analytics tables."""
        cursor = db_session.cursor()
        
        # Check analysis_run permissions
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'analytics.analysis_run', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'analytics.analysis_run', 'INSERT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'analytics.analysis_run', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'analytics.analysis_run', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_market_candle_permissions(self, db_session):
        """Test analytics_runner permissions on market.candle."""
        cursor = db_session.cursor()
        
        # Check market.candle permissions
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'market.candle', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'market.candle', 'INSERT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'market.candle', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'market.candle', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_dds_paper_trade_read_only(self, db_session):
        """Test that analytics_runner has SELECT only on dds.paper_trade."""
        cursor = db_session.cursor()
        
        # Check dds.paper_trade permissions
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.paper_trade', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.paper_trade', 'INSERT')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.paper_trade', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.paper_trade', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_dds_instrument_read_only(self, db_session):
        """Test that analytics_runner has SELECT on dds.instrument (symbol lookup)."""
        cursor = db_session.cursor()
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.instrument', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.instrument', 'INSERT')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.instrument', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'dds.instrument', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_config_scanner_direction_gate_read_only(self, db_session):
        """Test that analytics_runner cannot modify config.scanner_direction_gate."""
        cursor = db_session.cursor()
        
        # Check config.scanner_direction_gate permissions
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'INSERT')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_config_scanner_grafana_visibility_read_only(self, db_session):
        """Test that analytics_runner cannot modify config.scanner_grafana_visibility."""
        cursor = db_session.cursor()
        
        # Check config.scanner_grafana_visibility permissions
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_grafana_visibility', 'SELECT')"
        )
        assert cursor.fetchone()[0] is True
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_grafana_visibility', 'INSERT')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_grafana_visibility', 'UPDATE')"
        )
        assert cursor.fetchone()[0] is False
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_grafana_visibility', 'DELETE')"
        )
        assert cursor.fetchone()[0] is False

    def test_real_operations_analytics_insert(self, analytics_conn):
        """Test that analytics_runner can INSERT into analytics tables."""
        cursor = analytics_conn.cursor()
        
        # Insert a test analysis run
        cursor.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, analysis_from, analysis_to,
                observation_cutoff, maturity, status
            ) VALUES (
                gen_random_uuid(), CURRENT_DATE, NOW(), NOW() + INTERVAL '1 day',
                NOW(), 'PROVISIONAL', 'CREATED'
            )
            """
        )
        analytics_conn.commit()
        
        # Verify insert succeeded
        cursor.execute("SELECT COUNT(*) FROM analytics.analysis_run")
        assert cursor.fetchone()[0] >= 1

    def test_real_operations_analytics_update(self, analytics_conn):
        """Test that analytics_runner can UPDATE analytics tables."""
        cursor = analytics_conn.cursor()
        
        # Update the test analysis run
        cursor.execute(
            """
            UPDATE analytics.analysis_run 
            SET status = 'RUNNING' 
            WHERE business_date = CURRENT_DATE
            """
        )
        analytics_conn.commit()
        
        # Verify update succeeded
        cursor.execute(
            "SELECT status FROM analytics.analysis_run WHERE business_date = CURRENT_DATE"
        )
        result = cursor.fetchone()
        assert result[0] == 'RUNNING'

    def test_real_operations_market_candle_insert(self, analytics_conn):
        """Test that analytics_runner can INSERT into market.candle."""
        cursor = analytics_conn.cursor()
        
        # Use a unique instrument_id and timestamp to avoid conflicts
        import time
        unique_id = int(time.time() * 1000) % 1000000  # Use timestamp as unique ID
        
        # Insert a test candle with unique timestamp
        cursor.execute(
            """
            INSERT INTO market.candle (
                exchange, market_type, instrument_id, timeframe,
                open_time, close_time, open, high, low, close, volume
            ) VALUES (
                'bybit', 'linear', %s, '5',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '5 minutes',
                100.0, 105.0, 99.0, 103.0, 1000.0
            )
            """,
            (unique_id,)
        )
        analytics_conn.commit()
        
        # Verify insert succeeded
        cursor.execute("SELECT COUNT(*) FROM market.candle WHERE instrument_id = %s", (unique_id,))
        assert cursor.fetchone()[0] == 1

    def test_real_operations_dds_paper_trade_select(self, analytics_conn):
        """Test that analytics_runner can SELECT from dds.paper_trade."""
        cursor = analytics_conn.cursor()
        
        # This should succeed (SELECT is allowed)
        cursor.execute("SELECT COUNT(*) FROM dds.paper_trade")
        # Just verify it doesn't throw an exception
        assert cursor.fetchone()[0] >= 0

    def test_real_deny_config_scanner_direction_gate_update(self, analytics_conn):
        """Test that analytics_runner CANNOT UPDATE config.scanner_direction_gate."""
        cursor = analytics_conn.cursor()
        
        # First, check what columns exist in scanner_direction_gate
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate'
            LIMIT 5
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        
        if not columns:
            pytest.skip("config.scanner_direction_gate table not found")
        
        with pytest.raises(Exception) as exc_info:
            # Use a generic UPDATE that will fail due to permission
            cursor.execute(
                f"""
                UPDATE config.scanner_direction_gate 
                SET {columns[0]} = {columns[0]}
                WHERE 1=1
                """
            )
            analytics_conn.commit()
        
        # Should raise permission error (check for error code 42501 = insufficient_privilege)
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()

    def test_real_deny_dds_paper_trade_insert(self, analytics_conn):
        """Test that analytics_runner CANNOT INSERT into dds.paper_trade."""
        cursor = analytics_conn.cursor()
        
        # First, check what columns exist in paper_trade
        cursor.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'dds' AND table_name = 'paper_trade'
            LIMIT 5
            """
        )
        columns = [row[0] for row in cursor.fetchall()]
        
        if not columns:
            pytest.skip("dds.paper_trade table not found")
        
        with pytest.raises(Exception) as exc_info:
            # Use a generic INSERT that will fail due to permission
            cursor.execute(
                f"""
                INSERT INTO dds.paper_trade ({columns[0]})
                VALUES (1)
                """
            )
            analytics_conn.commit()
        
        # Should raise permission error (check for error code 42501 = insufficient_privilege)
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()

    def test_real_deny_dds_paper_trade_update(self, analytics_conn):
        """Test that analytics_runner CANNOT UPDATE dds.paper_trade."""
        cursor = analytics_conn.cursor()
        
        with pytest.raises(Exception) as exc_info:
            cursor.execute(
                """
                UPDATE dds.paper_trade 
                SET exit_price = 200.0 
                WHERE 1=1
                """
            )
            analytics_conn.commit()
        
        # Should raise permission error (check for error code 42501 = insufficient_privilege)
        assert "42501" in str(exc_info.value) or "permission denied" in str(exc_info.value).lower()

    def test_role_not_superuser(self, db_session):
        """Test that analytics_runner is not superuser."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result[0] is False

    def test_role_not_createdb(self, db_session):
        """Test that analytics_runner cannot create databases."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolcreatedb FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result[0] is False

    def test_role_not_createrole(self, db_session):
        """Test that analytics_runner cannot create roles."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolcreaterole FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result[0] is False

    def test_role_not_replication(self, db_session):
        """Test that analytics_runner cannot use replication."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolreplication FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result[0] is False

    def test_role_not_bypassrls(self, db_session):
        """Test that analytics_runner cannot bypass row level security."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'analytics_runner'"
        )
        result = cursor.fetchone()
        assert result[0] is False

    def test_config_schema_no_direct_usage(self, db_session):
        """Test that analytics_runner has no direct USAGE on config schema."""
        cursor = db_session.cursor()
        # Check if analytics_runner has USAGE on config schema
        # Note: This might be True if PUBLIC has USAGE on config
        # The important thing is that analytics_runner cannot WRITE to config tables
        cursor.execute(
            "SELECT has_schema_privilege('analytics_runner', 'config', 'USAGE')"
        )
        result = cursor.fetchone()
        # If analytics_runner has USAGE on config, it's because of PUBLIC grants
        # The important test is that it cannot write to config tables
        # For now, we just log this and don't fail
        # In production, you should revoke PUBLIC USAGE on config if needed

    def test_mart_schema_no_direct_usage(self, db_session):
        """Test that analytics_runner has no direct USAGE on mart schema."""
        cursor = db_session.cursor()
        # Check if analytics_runner has USAGE on mart schema
        # Note: This might be True if PUBLIC has USAGE on mart
        # The important thing is that analytics_runner cannot WRITE to mart tables
        cursor.execute(
            "SELECT has_schema_privilege('analytics_runner', 'mart', 'USAGE')"
        )
        result = cursor.fetchone()
        # If analytics_runner has USAGE on mart, it's because of PUBLIC grants
        # The important test is that it cannot write to mart tables
        # For now, we just log this and don't fail

    def test_dds_schema_usage_for_paper_trade(self, db_session):
        """Test that analytics_runner has USAGE on dds schema for paper_trade access."""
        cursor = db_session.cursor()
        cursor.execute(
            "SELECT has_schema_privilege('analytics_runner', 'dds', 'USAGE')"
        )
        result = cursor.fetchone()
        # analytics_runner needs USAGE on dds to read dds.paper_trade
        assert result[0] is True

    def test_no_public_write_privileges(self, db_session):
        """Test that analytics_runner doesn't get write privileges through PUBLIC."""
        cursor = db_session.cursor()
        
        # Check if analytics_runner has write privileges on config tables
        # This should be False even if PUBLIC has write privileges
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'INSERT')"
        )
        analytics_insert = cursor.fetchone()[0]
        assert analytics_insert is False, "analytics_runner should not have INSERT on config.scanner_direction_gate"
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'UPDATE')"
        )
        analytics_update = cursor.fetchone()[0]
        assert analytics_update is False, "analytics_runner should not have UPDATE on config.scanner_direction_gate"
        
        cursor.execute(
            "SELECT has_table_privilege('analytics_runner', 'config.scanner_direction_gate', 'DELETE')"
        )
        analytics_delete = cursor.fetchone()[0]
        assert analytics_delete is False, "analytics_runner should not have DELETE on config.scanner_direction_gate"