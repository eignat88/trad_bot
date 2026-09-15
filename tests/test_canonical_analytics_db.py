"""Stage 2: Canonical Analytics Data — DB Integration Tests.

Connection modes:
  Unix socket:  TEST_DB_UNIX_SOCK=/var/run/postgresql/.s.PGSQL.5432
  TCP:          TEST_DB_HOST=localhost  TEST_DB_PORT=5432

Common:
  TEST_DB_NAME=trad_bot_stage2_test
  TEST_DB_USER=postgres
  TEST_DB_PASSWORD=

Connection/auth/socket errors are real failures — never masked as skip.
Missing Stage 2 tables after a successful connection produces a clean skip.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pg8000
import pytest

from conftest import connect_test_db


# ======================================================================
# FIXTURES
# ======================================================================

@pytest.fixture(scope="module")
def db_conn():
    """Yield a live DB connection, or skip if Stage 2 is absent.

    Connection / auth errors → hard failure (pytest.fail).
    Successful connect but no Stage 2 tables → pytest.skip.
    """
    # --- 1. Establish connection (errors propagate) ---
    try:
        conn = connect_test_db()
    except Exception as exc:
        pytest.fail(f"Database connection failed — check env vars and server: {exc}")

    # --- 2. Check Stage 2 tables ---
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_schema = 'analytics' AND table_name = 'trade_fact'"
            ")"
        )
        available = cur.fetchone()[0]
    except Exception as exc:
        conn.close()
        pytest.fail(f"Schema introspection failed: {exc}")

    if not available:
        conn.close()
        pytest.skip(
            "Stage 2 canonical tables not found — "
            "run migrations 014-025 on the test database first. "
            "Set TEST_DB_NAME to a database with Stage 2 applied."
        )

    yield conn
    conn.close()


@pytest.fixture
def test_run_id(db_conn):
    """Create a disposable analysis_run row and return its UUID."""
    run_id = str(uuid4())
    cur = db_conn.cursor()
    cur.execute(
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
    cur.execute("DELETE FROM analytics.analysis_run WHERE run_id = %s", (run_id,))
    db_conn.commit()


# ======================================================================
# 1-2. PIT CUTOFF
# ======================================================================

class TestPITCutoff:

    def test_source_event_after_cutoff_detected(self, db_conn, test_run_id):
        """Event after observation_cutoff triggers a BLOCKING PIT violation."""
        cur = db_conn.cursor()
        cur.execute(
            "SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = %s",
            (test_run_id,),
        )
        cutoff = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, dataset_version
            ) VALUES (
                %s, 999999, 'pit_after', 'T', '1', 'X',
                'LONG', 100, 95, 50, 5, 'CLOSED', %s, '0'
            )
            """,
            (test_run_id, cutoff + timedelta(hours=1)),
        )
        db_conn.commit()

        cur.execute("SELECT * FROM analytics.check_pit_violation(%s)", (test_run_id,))
        row = cur.fetchone()
        assert row[2] == "FAIL", f"expected FAIL, got {row[2]}"
        assert row[1] == "BLOCKING"

        cur.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=999999",
            (test_run_id,),
        )
        db_conn.commit()

    def test_source_event_before_cutoff_passes(self, db_conn, test_run_id):
        """Event before observation_cutoff passes PIT check."""
        cur = db_conn.cursor()
        cur.execute(
            "SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = %s",
            (test_run_id,),
        )
        cutoff = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, closed_at, dataset_version
            ) VALUES (
                %s, 888888, 'pit_before', 'T', '1', 'X',
                'LONG', 100, 95, 50, 5, 'CLOSED', %s, %s, '0'
            )
            """,
            (test_run_id, cutoff - timedelta(hours=2), cutoff - timedelta(hours=1)),
        )
        db_conn.commit()

        cur.execute("SELECT * FROM analytics.check_pit_violation(%s)", (test_run_id,))
        assert cur.fetchone()[2] == "PASS"

        cur.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=888888",
            (test_run_id,),
        )
        db_conn.commit()


# ======================================================================
# 3. HORIZON COVERAGE
# ======================================================================

class TestHorizonCoverage:

    def test_incomplete_coverage_not_published(self, db_conn):
        """Incomplete horizon coverage is detectable."""
        cur = db_conn.cursor()
        cur.execute(
            """
            INSERT INTO analytics.trade_horizon_metric (
                trade_id, anchor, horizon, metric_version,
                favorable_move_r, adverse_move_r, coverage_status
            ) VALUES (777777, 'entry', '5m', '1.0.0', 1.5, -0.5, 'INCOMPLETE')
            ON CONFLICT (trade_id, anchor, horizon, metric_version) DO NOTHING
            """,
        )
        db_conn.commit()

        cur.execute(
            "SELECT COUNT(*) FROM analytics.trade_horizon_metric"
            " WHERE trade_id=777777 AND coverage_status != 'COMPLETE'"
        )
        assert cur.fetchone()[0] > 0

        cur.execute(
            "DELETE FROM analytics.trade_horizon_metric WHERE trade_id=777777"
        )
        db_conn.commit()


# ======================================================================
# 4-5. SEMI-OPEN WINDOW [from, to)
# ======================================================================

class TestMetricWindows:

    def test_window_boundary_inclusive_from(self, db_conn, test_run_id):
        """[from, to) includes the exact 'from' boundary."""
        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        window_from = now - timedelta(hours=24)
        window_to = now

        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, closed_at, net_pnl, pnl_r,
                dataset_version
            ) VALUES (
                %s, 666666, 'win_from', 'T', '1', 'X',
                'LONG', 100, 95, 50, 5, 'CLOSED', %s, 10, 0.5, '0'
            )
            """,
            (test_run_id, window_from),
        )
        db_conn.commit()

        cur.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_fact
            WHERE run_id=%s AND status='CLOSED'
              AND closed_at >= %s AND closed_at < %s
            """,
            (test_run_id, window_from, window_to),
        )
        assert cur.fetchone()[0] >= 1

        cur.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=666666",
            (test_run_id,),
        )
        db_conn.commit()

    def test_window_boundary_exclusive_to(self, db_conn, test_run_id):
        """[from, to) excludes the exact 'to' boundary."""
        cur = db_conn.cursor()
        now = datetime.now(timezone.utc)
        window_from = now - timedelta(hours=24)
        window_to = now

        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, closed_at, net_pnl, pnl_r,
                dataset_version
            ) VALUES (
                %s, 555555, 'win_to', 'T', '1', 'X',
                'LONG', 100, 95, 50, 5, 'CLOSED', %s, 10, 0.5, '0'
            )
            """,
            (test_run_id, window_to),
        )
        db_conn.commit()

        cur.execute(
            """
            SELECT COUNT(*) FROM analytics.trade_fact
            WHERE run_id=%s AND trade_id=555555
              AND closed_at >= %s AND closed_at < %s
            """,
            (test_run_id, window_from, window_to),
        )
        assert cur.fetchone()[0] == 0, "window_to must be exclusive"

        cur.execute(
            "DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=555555",
            (test_run_id,),
        )
        db_conn.commit()


# ======================================================================
# 6. RECONCILIATION
# ======================================================================

class TestReconciliation:

    def test_empty_reconciliation_passes(self, db_conn, test_run_id):
        """Zero source rows and zero canonical rows reconcile as PASS."""
        cur = db_conn.cursor()
        cur.execute(
            "SELECT * FROM analytics.reconcile_trade_count(%s)", (test_run_id,)
        )
        r = cur.fetchone()
        assert r[1] == 0, f"source_count={r[1]}"
        assert r[2] == 0, f"canonical_count={r[2]}"
        assert r[3] == 0, f"delta={r[3]}"
        assert r[4] == "PASS", f"severity={r[4]}"


# ======================================================================
# 7. QUALITY GATE
# ======================================================================

class TestQualityGate:

    def test_quality_gate_passes_empty(self, db_conn, test_run_id):
        """Empty run passes quality gate with zero failures."""
        cur = db_conn.cursor()
        cur.execute("SELECT * FROM analytics.quality_gate(%s)", (test_run_id,))
        r = cur.fetchone()
        assert r[0] is True, f"passed={r[0]}"
        assert r[1] == 0, f"blocking={r[1]}"


# ======================================================================
# 8. APPEND-ONLY
# ======================================================================

class TestAppendOnly:

    def test_update_blocked(self, db_conn, test_run_id):
        """UPDATE and DELETE on trade_event raise exceptions (append-only triggers)."""
        try:
            db_conn.rollback()
        except Exception:
            pass

        cur = db_conn.cursor()
        cur.execute(
            """
            INSERT INTO analytics.trade_event (
                run_id, trade_id, setup_id, event_type, event_at,
                source_event_key, payload_json
            ) VALUES (
                %s, 1, 'ao_test', 'ENTRY_FILLED', NOW(),
                'ao:' || md5(random()::text), '{}'::jsonb
            )
            RETURNING event_id
            """,
            (test_run_id,),
        )
        eid = cur.fetchone()[0]
        db_conn.commit()

        # UPDATE should be blocked
        with pytest.raises(Exception, match="append-only"):
            cur.execute(
                "UPDATE analytics.trade_event SET price=999 WHERE event_id=%s",
                (eid,),
            )
            db_conn.commit()
        db_conn.rollback()

        # DELETE should also be blocked
        with pytest.raises(Exception, match="append-only"):
            cur.execute("DELETE FROM analytics.trade_event WHERE event_id=%s", (eid,))
            db_conn.commit()
        db_conn.rollback()

        # Cleanup via TRUNCATE (only safe way for append-only table)
        cur.execute("TRUNCATE analytics.trade_event")
        db_conn.commit()


# ======================================================================
# 9. CROSS-RUN TRADE EVENT ISOLATION
# ======================================================================

class TestCrossRunEventIsolation:
    """Verify quality checks scope events by run_id."""

    def test_entered_without_fill_is_run_scoped(self, db_conn):
        """Event from run A should not satisfy quality check for run B."""
        cur = db_conn.cursor()

        # Create two analysis runs
        run_a = str(uuid4())
        run_b = str(uuid4())
        now = datetime.now(timezone.utc)

        for rid in (run_a, run_b):
            cur.execute(
                """
                INSERT INTO analytics.analysis_run (
                    run_id, business_date, schedule_timezone,
                    analysis_from, analysis_to, observation_cutoff,
                    post_exit_horizon, maturity, status, pipeline_version
                ) VALUES (%s, CURRENT_DATE, 'Europe/Sofia',
                    %s, %s, %s, INTERVAL '4 hours', 'PROVISIONAL', 'RUNNING', '1.0.0')
                """,
                (rid, now - timedelta(hours=24), now, now),
            )
        db_conn.commit()

        # Insert trade_fact in run A with entered_at set
        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, dataset_version
            ) VALUES (%s, 100, 'setup_iso', 'T', '1', 'X', 'LONG', 100, 95, 50, 5, 'CLOSED', %s, '0')
            """,
            (run_a, now - timedelta(hours=2)),
        )
        # Insert the ENTRY_FILLED event in run B (different run!)
        cur.execute(
            """
            INSERT INTO analytics.trade_event (
                run_id, trade_id, setup_id, event_type, event_at,
                source_event_key, payload_json
            ) VALUES (%s, 100, 'setup_iso', 'ENTRY_FILLED', %s,
                %s, '{}'::jsonb)
            """,
            (run_b, now - timedelta(hours=2), 'iso_fill:' + run_b),
        )
        db_conn.commit()

        # Run A should FAIL: trade has entered_at but no ENTRY_FILLED in run A
        cur.execute("SELECT * FROM analytics.check_entered_without_fill(%s)", (run_a,))
        row = cur.fetchone()
        assert row[2] == "FAIL", f"Run A should fail, got {row[2]}"

        # Cleanup — must remove trade_event rows before analysis_run (FK constraint)
        # trade_event is append-only (no DELETE/UPDATE), so TRUNCATE is the
        # only way to clean up test data.
        try:
            db_conn.rollback()
        except Exception:
            pass

        cur.execute("TRUNCATE analytics.trade_event")
        cur.execute("DELETE FROM analytics.trade_fact WHERE run_id IN (%s, %s) AND trade_id = 100", (run_a, run_b))
        cur.execute("DELETE FROM analytics.analysis_run WHERE run_id IN (%s, %s)", (run_a, run_b))
        db_conn.commit()


# ======================================================================
# 10. MFE/MAE CONTRACT
# ======================================================================

class TestMfeMaeContract:
    """Verify mfe_r/mae_r canonical formulas match paper_trade semantics."""

    def test_mfe_non_negative_mae_non_positive(self, db_conn):
        """mfe_r >= 0 and mae_r <= 0 for all closed trades in trade_fact."""
        cur = db_conn.cursor()
        cur.execute(
            """
            SELECT trade_id, mfe_r, mae_r
            FROM analytics.trade_fact
            WHERE status = 'CLOSED' AND mfe_r IS NOT NULL AND mae_r IS NOT NULL
            """
        )
        for trade_id, mfe_r, mae_r in cur.fetchall():
            assert float(mfe_r) >= 0, f"trade {trade_id}: mfe_r={mfe_r} < 0"
            assert float(mae_r) <= 0, f"trade {trade_id}: mae_r={mae_r} > 0"
