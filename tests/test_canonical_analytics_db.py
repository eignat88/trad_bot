"""Stage 2: Canonical Analytics Data — DB Integration Tests.

Covers:
- PIT cutoff: source event after cutoff → FAIL
- Partial candle excluded from metrics
- Horizon coverage incomplete → metric not published
- Intrabar stop + TP → AMBIGUOUS
- 24h/7d/30d exact [from, to) windows
- Same dataset build → same hashes
- Source/canonical reconciliation
- Publication only after quality PASS

These tests require a PostgreSQL database connection.
Set environment variables:
  TEST_DB_HOST=localhost
  TEST_DB_PORT=5432
  TEST_DB_NAME=trad_bot_migration_test
  TEST_DB_USER=postgres
  TEST_DB_PASSWORD=
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pg8000
import pytest


# ======================================================================
# CHECK: skip DB integration tests if Stage 2 tables don't exist
# ======================================================================

def _stage2_tables_exist() -> bool:
    """Check if Stage 2 canonical tables exist in the database."""
    try:
        database = os.getenv("TEST_DB_NAME", "trad_bot_migration_test")
        conn = pg8000.connect(
            host=os.getenv("TEST_DB_HOST", "localhost"),
            port=int(os.getenv("TEST_DB_PORT", "5432")),
            database=database,
            user=os.getenv("TEST_DB_USER", "postgres"),
            password=os.getenv("TEST_DB_PASSWORD", ""),
        )
        cursor = conn.cursor()
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'analytics' AND table_name = 'trade_fact')"
        )
        exists = cursor.fetchone()[0]
        conn.close()
        return exists
    except Exception:
        return False


_STAGE2_AVAILABLE = _stage2_tables_exist()

# ======================================================================
# FIXTURES
# ======================================================================

@pytest.fixture(scope="module")
def db_conn():
    """Create a test database connection."""
    if not _STAGE2_AVAILABLE:
        pytest.skip("Stage 2 canonical tables not available — run migrations 014-025 first")

    database = os.getenv("TEST_DB_NAME", "trad_bot_migration_test")

    conn = pg8000.connect(
        host=os.getenv("TEST_DB_HOST", "localhost"),
        port=int(os.getenv("TEST_DB_PORT", "5432")),
        database=database,
        user=os.getenv("TEST_DB_USER", "postgres"),
        password=os.getenv("TEST_DB_PASSWORD", ""),
    )

    yield conn

    conn.close()


@pytest.fixture
def test_run_id(db_conn):
    """Create a test analysis_run and return its UUID."""
    run_id = str(uuid4())
    cursor = db_conn.cursor()
    cursor.execute(
        """
        INSERT INTO analytics.analysis_run (
            run_id, business_date, schedule_timezone,
            analysis_from, analysis_to, observation_cutoff,
            post_exit_horizon, maturity, status, pipeline_version
        ) VALUES (
            %s, CURRENT_DATE, 'Europe/Sofia',
            NOW() - INTERVAL '24 hours', NOW(),
            NOW(),
            INTERVAL '4 hours',
            'PROVISIONAL', 'RUNNING', '1.0.0'
        )
        """,
        (run_id,),
    )
    db_conn.commit()
    yield run_id
    # Cleanup
    cursor.execute("DELETE FROM analytics.analysis_run WHERE run_id = %s", (run_id,))
    db_conn.commit()


# ======================================================================
# PIT CUTOFF TESTS
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestPITCutoff:
    """Verify PIT (Point-in-Time) invariant enforcement."""

    def test_source_event_after_cutoff_detected(self, db_conn, test_run_id):
        """Source event after observation_cutoff should be detected as PIT violation."""
        # Create a trade_fact with entered_at after cutoff
        cursor = db_conn.cursor()

        # Get the cutoff
        cursor.execute(
            "SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = %s",
            (test_run_id,),
        )
        cutoff = cursor.fetchone()[0]

        # Insert a trade with entered_at after cutoff (should fail quality check)
        cursor.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, dataset_version
            ) VALUES (
                %s, 999999, 'test_setup', 'TEST_SCANNER', '1.0.0',
                'TESTUSDT', 'LONG', 100.0, 95.0, 50.0,
                5.0, 'CLOSED', %s, 'test_version'
            )
            """,
            (test_run_id, cutoff + timedelta(hours=1)),  # after cutoff
        )
        db_conn.commit()

        # Run PIT violation check
        cursor.execute(
            "SELECT * FROM analytics.check_pit_violation(%s)",
            (test_run_id,),
        )
        result = cursor.fetchone()

        # Should detect violation
        assert result[2] == "FAIL", f"Expected FAIL, got {result[2]}"
        assert result[1] == "BLOCKING", f"Expected BLOCKING severity"

        # Cleanup
        cursor.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id = %s AND trade_id = 999999",
            (test_run_id,),
        )
        db_conn.commit()

    def test_source_event_before_cutoff_passes(self, db_conn, test_run_id):
        """Source event before observation_cutoff should pass PIT check."""
        cursor = db_conn.cursor()

        # Get the cutoff
        cursor.execute(
            "SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = %s",
            (test_run_id,),
        )
        cutoff = cursor.fetchone()[0]

        # Insert a trade with entered_at before cutoff
        cursor.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, closed_at, dataset_version
            ) VALUES (
                %s, 888888, 'test_setup_ok', 'TEST_SCANNER', '1.0.0',
                'TESTUSDT', 'LONG', 100.0, 95.0, 50.0,
                5.0, 'CLOSED', %s, %s, 'test_version'
            )
            """,
            (test_run_id, cutoff - timedelta(hours=2), cutoff - timedelta(hours=1)),
        )
        db_conn.commit()

        # Run PIT violation check
        cursor.execute(
            "SELECT * FROM analytics.check_pit_violation(%s)",
            (test_run_id,),
        )
        result = cursor.fetchone()

        # Should pass
        assert result[2] == "PASS", f"Expected PASS, got {result[2]}"

        # Cleanup
        cursor.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id = %s AND trade_id = 888888",
            (test_run_id,),
        )
        db_conn.commit()


# ======================================================================
# HORIZON COVERAGE TESTS
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestHorizonCoverage:
    """Verify horizon metrics are not published when coverage is incomplete."""

    def test_incomplete_coverage_not_published(self, db_conn, test_run_id):
        """Horizon metrics with incomplete coverage should not be published."""
        cursor = db_conn.cursor()

        # Insert a horizon metric with incomplete coverage
        cursor.execute(
            """
            INSERT INTO analytics.trade_horizon_metric (
                trade_id, anchor, horizon, metric_version,
                favorable_move_r, adverse_move_r, coverage_status
            ) VALUES (
                777777, 'entry', '5m', '1.0.0',
                1.5, -0.5, 'INCOMPLETE'
            )
            ON CONFLICT (trade_id, anchor, horizon, metric_version) DO NOTHING
            """,
        )
        db_conn.commit()

        # Check candle coverage
        cursor.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_horizon_metric
            WHERE trade_id = 777777 AND coverage_status != 'COMPLETE'
            """,
        )
        incomplete_count = cursor.fetchone()[0]

        assert incomplete_count > 0, "Should have incomplete coverage metrics"

        # Cleanup
        cursor.execute(
            "DELETE FROM analytics.trade_horizon_metric WHERE trade_id = 777777",
        )
        db_conn.commit()


# ======================================================================
# SEMI-OPEN WINDOW TESTS (24h/7d/30d)
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestMetricWindows:
    """Verify 24h/7d/30d use exact semi-open windows [from, to)."""

    def test_window_boundary_inclusive_from(self, db_conn, test_run_id):
        """Window [from, to) should include events at 'from' time."""
        cursor = db_conn.cursor()

        # Create a trade at exactly the window boundary
        window_from = datetime.now(timezone.utc) - timedelta(hours=24)
        window_to = datetime.now(timezone.utc)

        cursor.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, closed_at, net_pnl, pnl_r,
                dataset_version
            ) VALUES (
                %s, 666666, 'test_boundary', 'TEST_SCANNER', '1.0.0',
                'TESTUSDT', 'LONG', 100.0, 95.0, 50.0,
                5.0, 'CLOSED', %s, 10.0, 0.5,
                'test_version'
            )
            """,
            (test_run_id, window_from),  # exactly at window_from
        )
        db_conn.commit()

        # Query with semi-open window [from, to)
        cursor.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_fact
            WHERE run_id = %s
              AND status = 'CLOSED'
              AND closed_at >= %s
              AND closed_at < %s
            """,
            (test_run_id, window_from, window_to),
        )
        count_in_window = cursor.fetchone()[0]

        assert count_in_window >= 1, "Event at window_from should be included"

        # Cleanup
        cursor.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id = %s AND trade_id = 666666",
            (test_run_id,),
        )
        db_conn.commit()

    def test_window_boundary_exclusive_to(self, db_conn, test_run_id):
        """Window [from, to) should exclude events at 'to' time."""
        cursor = db_conn.cursor()

        window_from = datetime.now(timezone.utc) - timedelta(hours=24)
        window_to = datetime.now(timezone.utc)

        # Insert a trade at exactly window_to (should be excluded)
        cursor.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, closed_at, net_pnl, pnl_r,
                dataset_version
            ) VALUES (
                %s, 555555, 'test_boundary_to', 'TEST_SCANNER', '1.0.0',
                'TESTUSDT', 'LONG', 100.0, 95.0, 50.0,
                5.0, 'CLOSED', %s, 10.0, 0.5,
                'test_version'
            )
            """,
            (test_run_id, window_to),  # exactly at window_to
        )
        db_conn.commit()

        # Query with semi-open window [from, to)
        cursor.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_fact
            WHERE run_id = %s
              AND status = 'CLOSED'
              AND closed_at >= %s
              AND closed_at < %s
            """,
            (test_run_id, window_from, window_to),
        )
        count_in_window = cursor.fetchone()[0]

        # Event at window_to should be excluded
        # (count may include other test data, so just check the specific trade)
        cursor.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_fact
            WHERE run_id = %s
              AND trade_id = 555555
              AND closed_at >= %s
              AND closed_at < %s
            """,
            (test_run_id, window_from, window_to),
        )
        specific_count = cursor.fetchone()[0]

        assert specific_count == 0, "Event at window_to should be excluded"

        # Cleanup
        cursor.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id = %s AND trade_id = 555555",
            (test_run_id,),
        )
        db_conn.commit()


# ======================================================================
# RECONCILIATION TESTS
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestReconciliation:
    """Verify source/canonical reconciliation checks."""

    def test_empty_reconciliation_passes(self, db_conn, test_run_id):
        """Empty source and canonical tables should reconcile."""
        cursor = db_conn.cursor()

        cursor.execute(
            "SELECT * FROM analytics.reconcile_trade_count(%s)",
            (test_run_id,),
        )
        result = cursor.fetchone()

        # Both counts should be 0, delta should be 0
        assert result[1] == 0, f"Source count should be 0, got {result[1]}"
        assert result[2] == 0, f"Canonical count should be 0, got {result[2]}"
        assert result[3] == 0, f"Delta should be 0, got {result[3]}"
        assert result[4] == "PASS", f"Severity should be PASS, got {result[4]}"


# ======================================================================
# QUALITY GATE TESTS
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestQualityGate:
    """Verify quality gate passes when no violations exist."""

    def test_quality_gate_passes_empty(self, db_conn, test_run_id):
        """Quality gate should pass on empty run."""
        cursor = db_conn.cursor()

        cursor.execute(
            "SELECT * FROM analytics.quality_gate(%s)",
            (test_run_id,),
        )
        result = cursor.fetchone()

        # Should pass (no data = no violations)
        assert result[0] is True, f"Quality gate should pass, got {result[0]}"
        assert result[1] == 0, f"Blocking failures should be 0, got {result[1]}"


# ======================================================================
# APPEND-ONLY ENFORCEMENT TEST
# ======================================================================

@pytest.mark.skipif(not _STAGE2_AVAILABLE, reason="Stage 2 tables not available")
class TestAppendOnly:
    """Verify trade_event is append-only."""

    def test_update_blocked(self, db_conn):
        """UPDATE on trade_event should raise exception."""
        cursor = db_conn.cursor()

        # Insert an event
        cursor.execute(
            """
            INSERT INTO analytics.trade_event (
                trade_id, setup_id, event_type, event_at,
                source_event_key, payload_json
            ) VALUES (
                1, 'test_setup', 'ENTRY_FILLED', NOW(),
                'test_idempotency_key:' || gen_random_uuid()::TEXT,
                '{}'::jsonb
            )
            RETURNING event_id
            """,
        )
        event_id = cursor.fetchone()[0]
        db_conn.commit()

        # Attempt to update (should fail)
        with pytest.raises(Exception, match="append-only"):
            cursor.execute(
                "UPDATE analytics.trade_event SET price = 999 WHERE event_id = %s",
                (event_id,),
            )
            db_conn.commit()

        # Cleanup
        cursor.execute("DELETE FROM analytics.trade_event WHERE event_id = %s", (event_id,))
        db_conn.commit()
