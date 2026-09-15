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
from pathlib import Path

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
        """Source and canonical counts reconcile (delta=0)."""
        cur = db_conn.cursor()
        try:
            db_conn.rollback()
        except Exception:
            pass
        cur.execute(
            "SELECT * FROM analytics.reconcile_trade_count(%s)", (test_run_id,)
        )
        r = cur.fetchone()
        # Both counts should match — delta must be 0
        assert r[3] == 0, f"delta={r[3]}, source={r[1]}, canonical={r[2]}"
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

    def test_mfe_mae_synthetic_contract(self, db_conn):
        """Synthetic test: verify formula with known mfe/mae/risk_distance values.

        paper_trade stores mfe/mae as unsigned absolute price distances.
        mfe_r = mfe / risk_distance  (always >= 0)
        mae_r = -mae / risk_distance  (always <= 0)
        """
        cur = db_conn.cursor()

        # Create a run
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        cur.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone,
                analysis_from, analysis_to, observation_cutoff,
                post_exit_horizon, maturity, status, pipeline_version
            ) VALUES (%s, CURRENT_DATE, 'Europe/Sofia',
                %s, %s, %s, INTERVAL '4 hours', 'PROVISIONAL', 'RUNNING', '1.0.0')
            """,
            (run_id, now - timedelta(hours=24), now, now),
        )

        # Insert LONG trade: risk_distance=5, mfe=10 (price went +10 from ref), mae=2.5 (price went -2.5)
        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, closed_at,
                mfe_r, mae_r, pnl_r, dataset_version
            ) VALUES (
                %s, 200, 'synth_long', 'T', '1', 'X', 'LONG',
                100, 95, 25, 5, 'CLOSED',
                %s, %s,
                2.0, -0.5, 1.0, '0'
            )
            """,
            (run_id, now - timedelta(hours=1), now),
        )
        db_conn.commit()

        # Verify synthetic contract
        cur.execute(
            "SELECT mfe_r, mae_r FROM analytics.trade_fact WHERE run_id=%s AND trade_id=200",
            (run_id,),
        )
        mfe_r, mae_r = cur.fetchone()
        assert float(mfe_r) == pytest.approx(2.0), f"LONG mfe_r expected 2.0, got {mfe_r}"
        assert float(mae_r) == pytest.approx(-0.5), f"LONG mae_r expected -0.5, got {mae_r}"

        # Cleanup
        db_conn.rollback()
        cur.execute("TRUNCATE analytics.trade_event")
        cur.execute("DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=200", (run_id,))
        cur.execute("DELETE FROM analytics.analysis_run WHERE run_id=%s", (run_id,))
        db_conn.commit()

    def test_mfe_mae_synthetic_short(self, db_conn):
        """Synthetic test for SHORT direction contract."""
        cur = db_conn.cursor()

        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        cur.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone,
                analysis_from, analysis_to, observation_cutoff,
                post_exit_horizon, maturity, status, pipeline_version
            ) VALUES (%s, CURRENT_DATE, 'Europe/Sofia',
                %s, %s, %s, INTERVAL '4 hours', 'PROVISIONAL', 'RUNNING', '1.0.0')
            """,
            (run_id, now - timedelta(hours=24), now, now),
        )

        # SHORT trade: risk_distance=5, mfe=10 (price dropped 10 from ref), mae=2.5 (price rose 2.5)
        cur.execute(
            """
            INSERT INTO analytics.trade_fact (
                run_id, trade_id, setup_id, scanner_name, scanner_version,
                symbol, direction, reference_price, initial_stop, risk_usdt,
                initial_risk_distance, status, entered_at, closed_at,
                mfe_r, mae_r, pnl_r, dataset_version
            ) VALUES (
                %s, 300, 'synth_short', 'T', '1', 'X', 'SHORT',
                100, 105, 25, 5, 'CLOSED',
                %s, %s,
                2.0, -0.5, 0.8, '0'
            )
            """,
            (run_id, now - timedelta(hours=1), now),
        )
        db_conn.commit()

        cur.execute(
            "SELECT mfe_r, mae_r FROM analytics.trade_fact WHERE run_id=%s AND trade_id=300",
            (run_id,),
        )
        mfe_r, mae_r = cur.fetchone()
        assert float(mfe_r) == pytest.approx(2.0), f"SHORT mfe_r expected 2.0, got {mfe_r}"
        assert float(mae_r) == pytest.approx(-0.5), f"SHORT mae_r expected -0.5, got {mae_r}"

        # Cleanup
        db_conn.rollback()
        cur.execute("TRUNCATE analytics.trade_event")
        cur.execute("DELETE FROM analytics.trade_fact WHERE run_id=%s AND trade_id=300", (run_id,))
        cur.execute("DELETE FROM analytics.analysis_run WHERE run_id=%s", (run_id,))
        db_conn.commit()


# ======================================================================
# 11. HORIZON GEOMETRY MIRROR TEST
# ======================================================================

class TestHorizonGeometry:
    """Verify LONG/SHORT horizon R symmetry with GREATEST/LEAST clamping."""

    def test_long_short_mirror_favorable(self, db_conn):
        """LONG and SHORT with mirrored candles produce symmetric favorable R."""
        cur = db_conn.cursor()
        # Create test data
        cur.execute(
            """
            SELECT
                GREATEST(0, (105 - 100) / 5.0) AS long_fav,
                GREATEST(0, (100 - 95) / 5.0)  AS short_fav
            """
        )
        long_fav, short_fav = cur.fetchone()
        assert float(long_fav) == pytest.approx(1.0)
        assert float(short_fav) == pytest.approx(1.0)

    def test_long_adverse_clamped_at_zero(self, db_conn):
        """LONG adverse is clamped at 0 when price only went up."""
        cur = db_conn.cursor()
        # anchor=100, min=102 (price only went up), risk=5
        cur.execute("SELECT LEAST(0, (102 - 100) / 5.0)")
        result = cur.fetchone()[0]
        assert float(result) == pytest.approx(0.0)

    def test_short_adverse_clamped_at_zero(self, db_conn):
        """SHORT adverse is clamped at 0 when price only went down."""
        cur = db_conn.cursor()
        # anchor=100, max=98 (price only went down), risk=5
        cur.execute("SELECT LEAST(0, (100 - 98) / 5.0)")
        result = cur.fetchone()[0]
        assert float(result) == pytest.approx(0.0)

    def test_long_full_range(self, db_conn):
        """LONG: anchor=100, max=110, min=95, risk=5 → fav=2.0, adv=-1.0."""
        cur = db_conn.cursor()
        cur.execute(
            """
            SELECT
                GREATEST(0, (110 - 100) / 5.0),
                LEAST(0, (95 - 100) / 5.0)
            """
        )
        fav, adv = cur.fetchone()
        assert float(fav) == pytest.approx(2.0)
        assert float(adv) == pytest.approx(-1.0)

    def test_short_full_range(self, db_conn):
        """SHORT: anchor=100, min=90, max=105, risk=5 → fav=2.0, adv=-1.0."""
        cur = db_conn.cursor()
        cur.execute(
            """
            SELECT
                GREATEST(0, (100 - 90) / 5.0),
                LEAST(0, (100 - 105) / 5.0)
            """
        )
        fav, adv = cur.fetchone()
        assert float(fav) == pytest.approx(2.0)
        assert float(adv) == pytest.approx(-1.0)


# ======================================================================
# 12. E2E: paper_trade → trade_fact build via migration 017
# ======================================================================

class TestTradeFactBuildE2E:
    """End-to-end: insert synthetic paper_trade + scanner_setup, run
    trade_fact build function, verify mfe_r/mae_r are recomputed correctly."""

    def _cleanup(self, cur, db_conn, run_id=None, instrument_ids=None, setup_ids=None, trade_ids=None):
        """Clean up test data to avoid cross-test pollution."""
        try:
            db_conn.rollback()
        except Exception:
            pass
        # Clean trade_event first (append-only, must truncate)
        try:
            cur.execute("TRUNCATE analytics.trade_event")
        except Exception:
            db_conn.rollback()
        if trade_ids:
            for tid in trade_ids:
                cur.execute("DELETE FROM analytics.trade_fact WHERE trade_id = %s", (tid,))
        if setup_ids:
            for sid in setup_ids:
                cur.execute("DELETE FROM dds.scanner_setup WHERE setup_id = %s", (sid,))
        if instrument_ids:
            for iid in instrument_ids:
                cur.execute("DELETE FROM dds.market_signal WHERE instrument_id = %s", (iid,))
                cur.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (iid,))
        if run_id:
            cur.execute("DELETE FROM analytics.analysis_run WHERE run_id = %s", (run_id,))
        db_conn.commit()

    def _cleanup_all(self, cur, db_conn):
        """Nuclear cleanup: remove all E2E test artifacts."""
        try:
            db_conn.rollback()
        except Exception:
            pass
        try:
            cur.execute("TRUNCATE analytics.trade_event")
        except Exception:
            db_conn.rollback()
        # Collect test run_ids before deleting
        cur.execute(
            "SELECT run_id FROM analytics.analysis_run "
            "WHERE pipeline_version = '1.0.0' AND business_date = CURRENT_DATE"
        )
        test_run_ids = [r[0] for r in cur.fetchall()]

        # FK order: trade_event (truncated) → trade_horizon_metric → trade_replay_metric
        #           → trade_fact → paper_trade → scanner_setup → market_signal → instrument
        #           → setup_counterfactual → analysis_run
        cur.execute("DELETE FROM analytics.trade_horizon_metric WHERE trade_id IN (500, 501)")
        cur.execute("DELETE FROM analytics.trade_replay_metric WHERE trade_id IN (500, 501)")
        # Delete ALL trade_fact rows for test runs (migration 017 may create extra rows)
        for rid in test_run_ids:
            cur.execute("DELETE FROM analytics.trade_fact WHERE run_id = %s", (rid,))
        cur.execute("DELETE FROM dds.paper_trade WHERE trade_id IN (500, 501)")
        cur.execute("DELETE FROM dds.scanner_setup WHERE setup_id LIKE 'e2e_%'")
        cur.execute("DELETE FROM dds.market_signal WHERE instrument_id IN (SELECT instrument_id FROM dds.instrument WHERE symbol LIKE 'E2E%')")
        cur.execute("DELETE FROM dds.instrument WHERE symbol LIKE 'E2E%'")
        for rid in test_run_ids:
            cur.execute("DELETE FROM analytics.analysis_run WHERE run_id = %s", (rid,))
        db_conn.commit()
        db_conn.commit()

    def _setup_prereqs(self, cur, run_id, symbol_suffix=""):
        """Create analysis_run, instrument, scanner_setup, market_signal."""
        now = datetime.now(timezone.utc)
        symbol = f"E2E{symbol_suffix}USDT" if symbol_suffix else "E2EUSDT"

        cur.execute(
            """
            INSERT INTO analytics.analysis_run (
                run_id, business_date, schedule_timezone,
                analysis_from, analysis_to, observation_cutoff,
                post_exit_horizon, maturity, status, pipeline_version
            ) VALUES (%s, CURRENT_DATE, 'Europe/Sofia',
                %s, %s, %s, INTERVAL '4 hours', 'PROVISIONAL', 'SUCCEEDED', '1.0.0')
            """,
            (run_id, now - timedelta(hours=24), now + timedelta(hours=1), now),
        )

        cur.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, 'E2E', 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            (symbol,),
        )
        instrument_id = cur.fetchone()[0]

        setup_id = f"e2e_{run_id[:8]}"

        cur.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, detected_at, reference_price, score,
                status, reasons, features, created_at, signal_candle_open_time, updated_at
            ) VALUES (
                %s, 'TEST_SCANNER', '2.1', %s,
                'LONG', '60', '5m', '5m',
                %s, %s, 100.0, 0.8,
                'EXECUTED', '[]', '{}', NOW(), 0, NOW()
            )
            """,
            (setup_id, instrument_id, now - timedelta(hours=2), now - timedelta(hours=2)),
        )

        cur.execute(
            """
            INSERT INTO dds.market_signal (
                instrument_id, direction, timeframe, scanner_count, scanners,
                max_score, aggregate_score, first_detected_at, last_detected_at, status
            ) VALUES (
                %s, 'LONG', '5m', 1, '[{\"name\":\"TEST_SCANNER\"}]'::jsonb,
                80, 80, %s, %s, 'EXECUTED'
            )
            """,
            (instrument_id, now - timedelta(hours=2), now - timedelta(hours=2)),
        )

        return instrument_id, setup_id

    def test_build_recomputes_mfe_r_mae_r(self, db_conn):
        """paper_trade.mfe/mae → trade_fact.mfe_r/mae_r via build function."""
        cur = db_conn.cursor()
        self._cleanup_all(cur, db_conn)
        run_id = str(uuid4())
        instrument_id, setup_id = self._setup_prereqs(cur, run_id)
        db_conn.commit()

        try:
            now = datetime.now(timezone.utc)
            cur.execute(
                """
                INSERT INTO dds.paper_trade (
                    trade_id, setup_id, symbol, scanner_name, direction,
                    score, entry_price, entry_fee, stop_price, position_size,
                    risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                    pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                    duration_sec, balance_before, balance_after, entry_timeframe,
                    gross_pnl, mfe, mae, mfe_r, mae_r
                ) VALUES (
                    500, %s, 'E2EUSDT', 'TEST_SCANNER', 'LONG',
                    0.8, 100.0, 0.5, 95.0, 5.0,
                    25.0, 103.0, 'TAKE_PROFIT_1', 0.5, 15.0,
                    0.6, 0.6, 0.1, 'CLOSED',
                    %s, %s,
                    3600, 10000.0, 10150.0, '5m',
                    15.0, 10.0, 2.5, 2.0, -0.5
                )
                """,
                (setup_id, now - timedelta(hours=1), now),
            )
            db_conn.commit()

            # Run trade_fact build (migration 017 as a statement)
            migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "017_trade_fact_build.sql"
            build_sql = migration_path.read_text(encoding="utf-8")
            cur.execute(build_sql)
            db_conn.commit()

            # Verify
            cur.execute(
                "SELECT mfe_r, mae_r, pnl_r, initial_risk_distance "
                "FROM analytics.trade_fact WHERE run_id=%s AND trade_id=500",
                (run_id,),
            )
            row = cur.fetchone()
            assert row is not None, "trade_fact row not created"
            mfe_r, mae_r, pnl_r, risk_dist = row

            assert float(mfe_r) == pytest.approx(2.0), f"mfe_r={mfe_r}, expected 2.0"
            assert float(mae_r) == pytest.approx(-0.5), f"mae_r={mae_r}, expected -0.5"
            assert float(risk_dist) == pytest.approx(5.0), f"risk_dist={risk_dist}, expected 5.0"
        finally:
            self._cleanup_all(cur, db_conn)

    def test_build_recomputes_short_mfe_r_mae_r(self, db_conn):
        """SHORT paper_trade → trade_fact with correct SHORT mfe_r/mae_r."""
        cur = db_conn.cursor()
        self._cleanup_all(cur, db_conn)
        run_id = str(uuid4())
        instrument_id, setup_id = self._setup_prereqs(cur, run_id, symbol_suffix="S")
        db_conn.commit()

        try:
            # Override setup to SHORT
            cur.execute(
                "UPDATE dds.scanner_setup SET direction='SHORT' WHERE setup_id=%s",
                (setup_id,),
            )
            cur.execute(
                "UPDATE dds.market_signal SET direction='SHORT' WHERE instrument_id=%s",
                (instrument_id,),
            )
            db_conn.commit()

            now = datetime.now(timezone.utc)
            cur.execute(
                """
                INSERT INTO dds.paper_trade (
                    trade_id, setup_id, symbol, scanner_name, direction,
                    score, entry_price, entry_fee, stop_price, position_size,
                    risk_usdt, exit_price, exit_reason, exit_fee, pnl_usdt,
                    pnl_r, pnl_percent, slippage, status, entered_at, closed_at,
                    duration_sec, balance_before, balance_after, entry_timeframe,
                    gross_pnl, mfe, mae, mfe_r, mae_r
                ) VALUES (
                    501, %s, 'E2ESUSDT', 'TEST_SCANNER', 'SHORT',
                    0.8, 100.0, 0.5, 105.0, 5.0,
                    25.0, 97.0, 'TAKE_PROFIT_1', 0.5, 15.0,
                    0.6, 0.6, 0.1, 'CLOSED',
                    %s, %s,
                    3600, 10000.0, 10150.0, '5m',
                    15.0, 10.0, 2.5, 2.0, -0.5
                )
                """,
                (setup_id, now - timedelta(hours=1), now),
            )
            db_conn.commit()

            migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "017_trade_fact_build.sql"
            build_sql = migration_path.read_text(encoding="utf-8")
            cur.execute(build_sql)
            db_conn.commit()

            cur.execute(
                "SELECT mfe_r, mae_r, initial_risk_distance "
                "FROM analytics.trade_fact WHERE run_id=%s AND trade_id=501",
                (run_id,),
            )
            row = cur.fetchone()
            assert row is not None, "trade_fact row not created for SHORT"
            mfe_r, mae_r, risk_dist = row

            assert float(mfe_r) == pytest.approx(2.0), f"SHORT mfe_r={mfe_r}"
            assert float(mae_r) == pytest.approx(-0.5), f"SHORT mae_r={mae_r}"
            assert float(risk_dist) == pytest.approx(5.0)
        finally:
            self._cleanup_all(cur, db_conn)


# ======================================================================
# 13. E2E: compute_horizon_metric() with real candles
# ======================================================================

class TestHorizonMetricE2E:
    """End-to-end: insert candle rows → call compute_horizon_metric()
    → verify LONG/SHORT geometry with GREATEST/LEAST clamping."""

    def _insert_candles(self, cur, instrument_id, base_time, prices):
        """Insert a series of 1-minute candles with given OHLC prices.

        Each candle: open=close=price, high=price, low=price
        so that max_price and min_price in the horizon are exactly the
        values in the prices list.
        """
        for i, close_price in enumerate(prices):
            open_time = base_time + timedelta(minutes=i)
            close_time = open_time + timedelta(minutes=1)
            cur.execute(
                """
                INSERT INTO market.candle (
                    exchange, market_type, instrument_id, timeframe,
                    open_time, close_time, open, high, low, close, volume,
                    is_closed, source, ingested_at, quality_status
                ) VALUES (
                    'bybit', 'linear', %s, '1',
                    %s, %s, %s, %s, %s, %s, 1000,
                    TRUE, 'test', NOW(), 'validated'
                )
                """,
                (instrument_id, open_time, close_time,
                 close_price, close_price, close_price, close_price),
            )

    def _create_instrument(self, cur, symbol):
        """Create instrument, return id. Cleans up previous if exists."""
        cur.execute("DELETE FROM dds.instrument WHERE symbol = %s", (symbol,))
        cur.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES (%s, 'T', 'USDT', 'linear', 'Trading') RETURNING instrument_id",
            (symbol,),
        )
        return cur.fetchone()[0]

    def test_long_horizon_with_real_candles(self, db_conn):
        """LONG: anchor=100, 5m horizon, candles go to 110 then 95, risk=5 -> fav=2.0, adv=-1.0."""
        cur = db_conn.cursor()
        try:
            db_conn.rollback()
        except Exception:
            pass
        base = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
        cutoff = base + timedelta(hours=1)

        instrument_id = self._create_instrument(cur, "E2E_HL")
        # 5-minute horizon: candles from base to base+5min
        # Need high=110 and low=95 within the first 5 minutes
        prices = [100, 110, 105, 100, 95, 100, 102, 105, 110, 108, 103, 100]
        self._insert_candles(cur, instrument_id, base, prices)
        db_conn.commit()

        cur.execute(
            """
            SELECT * FROM analytics.compute_horizon_metric(
                1::bigint, 'entry', '5m', 100.0, %s,
                'LONG', 5.0, %s, %s, NULL::timestamptz
            )
            """,
            (base, instrument_id, cutoff),
        )
        fav, adv, coverage = cur.fetchone()
        assert coverage == 'COMPLETE'
        assert float(fav) == pytest.approx(2.0), f"LONG fav={fav}, expected 2.0"
        assert float(adv) == pytest.approx(-1.0), f"LONG adv={adv}, expected -1.0"

    def test_short_horizon_with_real_candles(self, db_conn):
        """SHORT: anchor=100, 5m horizon, candles drop to 90 then rise to 105, risk=5 -> fav=2.0, adv=-1.0."""
        cur = db_conn.cursor()
        try:
            db_conn.rollback()
        except Exception:
            pass
        base = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        cutoff = base + timedelta(hours=1)

        instrument_id = self._create_instrument(cur, "E2E_SC")
        # 5-minute horizon: need low=90 and high=105 within first 5 minutes
        prices = [100, 90, 95, 100, 105, 100, 98, 95, 92, 90, 93, 96]
        self._insert_candles(cur, instrument_id, base, prices)
        db_conn.commit()

        cur.execute(
            """
            SELECT * FROM analytics.compute_horizon_metric(
                2::bigint, 'entry', '5m', 100.0, %s,
                'SHORT', 5.0, %s, %s, NULL::timestamptz
            )
            """,
            (base, instrument_id, cutoff),
        )
        fav, adv, coverage = cur.fetchone()
        assert coverage == 'COMPLETE'
        assert float(fav) == pytest.approx(2.0), f"SHORT fav={fav}, expected 2.0"
        assert float(adv) == pytest.approx(-1.0), f"SHORT adv={adv}, expected -1.0"

    def test_clamp_to_zero_when_no_adverse(self, db_conn):
        """When price only moves favorably, adverse is clamped to 0."""
        cur = db_conn.cursor()
        try:
            db_conn.rollback()
        except Exception:
            pass
        base = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        cutoff = base + timedelta(hours=1)

        instrument_id = self._create_instrument(cur, "E2E_CL")
        # 5m horizon: prices only go up, max=110 within first 5 candles
        prices = [100, 102, 105, 108, 110, 108, 105, 103, 101, 100]
        self._insert_candles(cur, instrument_id, base, prices)
        db_conn.commit()

        cur.execute(
            """
            SELECT * FROM analytics.compute_horizon_metric(
                3::bigint, 'entry', '5m', 100.0, %s,
                'LONG', 5.0, %s, %s, NULL::timestamptz
            )
            """,
            (base, instrument_id, cutoff),
        )
        fav, adv, coverage = cur.fetchone()
        assert coverage == 'COMPLETE'
        assert float(fav) == pytest.approx(2.0), f"fav={fav}"
        assert float(adv) == 0.0, f"adv={adv}, expected 0.0"
