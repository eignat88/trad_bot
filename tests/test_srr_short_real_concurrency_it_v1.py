"""Real two-connection PostgreSQL concurrency test for SRR activation.

This test exercises a genuine ``SrrOutcomeWriter.write()`` against a real
local PostgreSQL instance with two independent connections and a
deterministic synchronization hook that pauses the writer between the
``FOR SHARE`` acquisition and the outcome ``INSERT``/``UPDATE``.

Covered scenarios:
1. Writer acquires FOR SHARE; concurrent REVOKE blocks until writer COMMIT.
2. After writer COMMIT, REVOKE COMMITs.
3. A subsequent write returns strictly ``REJECTED_ACTIVATION_GATE`` with
   no outcome INSERT/UPDATE.
4. Reverse order: REVOKE committed before the writer reads → write
   strictly rejected.

All waits are bounded; ``lock_timeout`` bounds the revoke wait. The test
database is a dedicated throwaway DB.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tests.srr_postgres_test_safety import (
    SrrPostgresTestSafetyError,
    close_admin_connection,
    load_srr_postgres_test_config,
    open_admin_connection,
    validate_disposable_database,
    validate_server_identity,
)

pg8000 = pytest.importorskip("pg8000")

ENABLE = "SRR_PERSISTENCE_POSTGRES_IT"

SRR_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
BOUNDARY_VERSION = "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1"
ACTIVATION_TS = "2026-10-08T12:00:00Z"


def _config():
    return load_srr_postgres_test_config()


def _connect(dbname: str, autocommit: bool = False):
    config = _config()
    if dbname not in {config.database, config.activation_database, "postgres"}:
        raise SrrPostgresTestSafetyError(f"STOP: unexpected test database {dbname!r}")
    conn = pg8000.connect(
        host=config.host,
        port=config.port,
        database=dbname,
        user=config.user,
        timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_setting('server_version'), "
                "inet_server_addr(), inet_server_port()"
            )
            database, version, address, port = cur.fetchone()
            validate_server_identity(
                config,
                {
                    "current_database": database,
                    "server_version": version,
                    "inet_server_addr": address,
                    "inet_server_port": port,
                },
            )
    except Exception:
        conn.close()
        raise
    conn.autocommit = autocommit
    return conn


def _ensure_activation_db():
    config = _config()
    validate_disposable_database(config.activation_database)
    admin = open_admin_connection(config, pg8000.connect)
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (config.activation_database,),
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{config.activation_database}"')
    finally:
        close_admin_connection(admin)


def _drop_activation_db():
    config = _config()
    validate_disposable_database(config.activation_database)
    admin = open_admin_connection(config, pg8000.connect)
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (config.activation_database,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{config.activation_database}"')
    finally:
        close_admin_connection(admin)


def _apply_bootstrap():
    path = os.path.join(
        os.path.dirname(__file__), "..", "srr_persistence_it_bootstrap.sql"
    )
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    conn = _connect(_config().activation_database, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _apply_migration(migration_num: str):
    config = _config()
    path = os.path.join(
        os.path.dirname(__file__), "..", "sql", "migrations",
        f"{migration_num}_srr_short_execution_outcome_persistence.sql"
        if migration_num == "061" else
        f"{migration_num}_srr_short_writer_activation_boundary.sql",
    )
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    conn = _connect(config.activation_database, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _seed_observation(conn, observation_id: int):
    from datetime import datetime, timezone
    import json
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.prospective_experiment (experiment_id) "
            "VALUES (%s) ON CONFLICT DO NOTHING",
            (SRR_EXPERIMENT_ID,),
        )
        cur.execute(
            "INSERT INTO research.prospective_observation "
            "(observation_id, experiment_id, source_signal_id, symbol, "
            " direction, signal_time, reference_price, invalidation_price, "
            " variant_entry, variant_stop, variant_target, features) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                observation_id, SRR_EXPERIMENT_ID, observation_id,
                "AAAUSDT", "SHORT",
                datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
                100.0, 102.0, 100.0, 102.0, 98.5,
                json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
            ),
        )
    conn.commit()


def _seed_activation(conn, status: str = "ACTIVE"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (experiment_id) DO UPDATE SET status = EXCLUDED.status",
            (SRR_EXPERIMENT_ID, BOUNDARY_VERSION, ACTIVATION_TS, "SHORT", status),
        )
    conn.commit()


def _make_gate():
    from app.research.srr_short_writer_activation_boundary_v1 import (
        SrrShortWriterActivationBoundary,
        SrrShortWriterActivationGate,
        SrrShortWriterActivationMode,
    )
    boundary = SrrShortWriterActivationBoundary.create(ACTIVATION_TS)
    return SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )


def _make_observation(observation_id: int):
    from datetime import datetime, timezone
    return {
        "observation_id": observation_id,
        "experiment_id": SRR_EXPERIMENT_ID,
        "direction": "SHORT",
        "signal_time": datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
        "symbol": "AAAUSDT",
        "reference_price": 100.0,
        "invalidation_price": 102.0,
        "variant_entry": 100.0,
        "variant_stop": 102.0,
        "variant_target": 98.5,
        "features": '{"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}',
    }


def _nonfinal_result():
    return {
        "status": "INCOMPLETE_COVERAGE",
        "reason_code": "OK",
        "finalization_eligible": False,
        "path_class": None,
        "source_status": "SOURCE_VALIDATED",
        "eligible_candle_count": 0,
        "gross_r": None,
        "cost_r_normal": None,
        "cost_r_elevated": None,
        "net_r_normal": None,
        "net_r_elevated": None,
        "policy_result": {},
        "source_diagnostics": {},
        "route_diagnostics": {},
    }


@pytest.fixture
def it_db():
    """Provision the throwaway DB, seed observation + ACTIVE record."""
    if os.getenv(ENABLE) != "1":
        pytest.skip(f"Set {ENABLE}=1 to run local PostgreSQL integration tests")
    _ensure_activation_db()
    _apply_bootstrap()
    _apply_migration("061")
    _apply_migration("062")

    setup = _connect(_config().activation_database)
    _seed_observation(setup, 1001)
    _seed_activation(setup, "ACTIVE")
    setup.close()

    yield _config().activation_database

    _drop_activation_db()


# ═══════════════════════════════════════════════════════════════════
# Real concurrency tests
# ═══════════════════════════════════════════════════════════════════


def test_real_concurrency_writer_lock_revoke_waits(it_db):
    """Real SrrOutcomeWriter.write() with a deterministic pause between
    FOR SHARE and INSERT. A concurrent REVOKE must block until the writer
    commits. After writer COMMIT, REVOKE COMMITs. A subsequent write is
    strictly REJECTED_ACTIVATION_GATE with no outcome SQL."""
    from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter

    gate = _make_gate()

    writer_conn = _connect(it_db)
    revoke_conn = _connect(it_db)

    writer_thread_ready = threading.Event()
    writer_thread_done = threading.Event()
    release_writer = threading.Event()
    writer_result = {}
    writer_error = {}

    revoke_thread_started = threading.Event()
    revoke_thread_done = threading.Event()
    revoke_result = {}

    def do_writer():
        writer = SrrOutcomeWriter(writer_conn, activation_gate=gate)

        def after_lock_hook():
            writer_thread_ready.set()
            # Wait (bounded) until the test releases us.
            release_writer.wait(timeout=10)

        writer._after_lock_hook = after_lock_hook
        try:
            writer_result["result"] = writer.write(
                observation_id=1001,
                experiment_id=SRR_EXPERIMENT_ID,
                observation=_make_observation(1001),
                result=_nonfinal_result(),
            )
        except Exception as exc:
            writer_error["exc"] = exc
        finally:
            writer_thread_done.set()

    def do_revoke():
        revoke_thread_started.set()
        try:
            with revoke_conn.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute(
                    "UPDATE research.srr_short_writer_activation "
                    "SET status = 'REVOKED' WHERE experiment_id = %s",
                    (SRR_EXPERIMENT_ID,),
                )
            revoke_conn.commit()
            revoke_result["status"] = "COMMITTED"
        except Exception as exc:
            revoke_result["status"] = f"ERROR: {exc}"
        finally:
            revoke_thread_done.set()

    writer_thread = threading.Thread(target=do_writer, daemon=True)
    revoke_thread = threading.Thread(target=do_revoke, daemon=True)

    try:
        # 1. Start Thread A (writer). It pauses after FOR SHARE.
        writer_thread.start()
        assert writer_thread_ready.wait(timeout=5), (
            "Writer thread did not reach the lock point"
        )

        # 2. Start Thread B (revoke). It must block on the FOR SHARE lock.
        revoke_thread.start()
        revoke_thread_started.wait(timeout=2)
        time.sleep(0.3)

        # 3. Revoke must still be waiting (not done).
        assert not revoke_thread_done.is_set(), (
            f"REVOKE completed while writer held FOR SHARE: {revoke_result}"
        )

        # 4. Release the writer to continue to INSERT/UPDATE + COMMIT.
        release_writer.set()
        writer_thread_done.wait(timeout=10)
        writer_thread.join(timeout=5)

        # 5. Writer COMMIT succeeded.
        assert "exc" not in writer_error, f"Writer raised: {writer_error}"
        assert writer_result["result"].action in {"INSERTED", "REFRESHED"}

        # 6. REVOKE should now complete.
        revoke_thread_done.wait(timeout=5)
        revoke_thread.join(timeout=5)
        assert revoke_result["status"] == "COMMITTED", revoke_result

        # 7. Subsequent write must be strictly REJECTED_ACTIVATION_GATE
        #    with no outcome INSERT/UPDATE.
        verify_conn = _connect(it_db)
        verify_writer = SrrOutcomeWriter(verify_conn, activation_gate=gate)
        try:
            result2 = verify_writer.write(
                observation_id=1002,
                experiment_id=SRR_EXPERIMENT_ID,
                observation=_make_observation(1002),
                result=_nonfinal_result(),
            )
            assert result2.action == "REJECTED_ACTIVATION_GATE"

            # Verify no outcome row was created for 1002.
            with verify_conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM "
                    "research.srr_short_execution_prospective_outcome "
                    "WHERE observation_id = %s",
                    (1002,),
                )
                assert cur.fetchone()[0] == 0
        finally:
            verify_conn.close()
    finally:
        # Cleanup: ensure no leaked transactions.
        release_writer.set()
        writer_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        try:
            writer_conn.rollback()
        except Exception:
            pass
        writer_conn.close()
        try:
            revoke_conn.rollback()
        except Exception:
            pass
        revoke_conn.close()


def test_real_reverse_order_revoke_before_writer(it_db):
    """REVOKE committed before the writer reads → the write is strictly
    REJECTED_ACTIVATION_GATE with no outcome INSERT/UPDATE."""
    from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter

    gate = _make_gate()

    # Revoke first.
    revoke_conn = _connect(it_db)
    with revoke_conn.cursor() as cur:
        cur.execute(
            "UPDATE research.srr_short_writer_activation "
            "SET status = 'REVOKED' WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
    revoke_conn.commit()
    revoke_conn.close()

    # Writer tries to write.
    writer_conn = _connect(it_db)
    writer = SrrOutcomeWriter(writer_conn, activation_gate=gate)
    try:
        result = writer.write(
            observation_id=1002,
            experiment_id=SRR_EXPERIMENT_ID,
            observation=_make_observation(1002),
            result=_nonfinal_result(),
        )
        assert result.action == "REJECTED_ACTIVATION_GATE"

        # Verify no outcome row was created.
        with writer_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM "
                "research.srr_short_execution_prospective_outcome "
                "WHERE observation_id = %s",
                (1002,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        writer_conn.close()


def test_hook_exception_releases_for_share_lock(it_db):
    """A _after_lock_hook exception after FOR SHARE acquisition triggers
    rollback; a subsequent ACTIVE -> REVOKED on a second connection
    completes immediately (no lock_timeout needed). No outcome rows are
    created; the reader transaction is unaffected."""
    from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter

    gate = _make_gate()

    writer_conn = _connect(it_db)
    revoke_conn = _connect(it_db)
    reader_conn = _connect(it_db)

    writer = SrrOutcomeWriter(writer_conn, activation_gate=gate)

    try:
        # Reader opens a transaction to verify it is unaffected.
        with reader_conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1

        # Set a hook that raises after FOR SHARE is acquired.
        def raising_hook():
            raise RuntimeError("INJECTED_HOOK_FAILURE")

        writer._after_lock_hook = raising_hook

        # The write must raise; the rollback inside write() releases the lock.
        with pytest.raises(RuntimeError, match="INJECTED_HOOK_FAILURE"):
            writer.write(
                observation_id=1001,
                experiment_id=SRR_EXPERIMENT_ID,
                observation=_make_observation(1001),
                result=_nonfinal_result(),
            )

        # A second connection performs ACTIVE -> REVOKED. Because the lock
        # was rolled back, this must complete immediately. We do NOT set
        # lock_timeout: if the lock were still held, this UPDATE would
        # block until the statement timeout (default 0 = wait forever),
        # causing the test to hang and be caught by the outer test timeout.
        with revoke_conn.cursor() as cur:
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET status = 'REVOKED' WHERE experiment_id = %s",
                (SRR_EXPERIMENT_ID,),
            )
        revoke_conn.commit()

        # Verify no outcome row was created by the failed write.
        with writer_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM "
                "research.srr_short_execution_prospective_outcome "
                "WHERE observation_id = %s",
                (1001,),
            )
            assert cur.fetchone()[0] == 0

        # Reader transaction is still open and unaffected.
        with reader_conn.cursor() as cur:
            cur.execute("SELECT 2")
            assert cur.fetchone()[0] == 2
    finally:
        # Cleanup: ensure no leaked transactions.
        try:
            writer_conn.rollback()
        except Exception:
            pass
        writer_conn.close()
        try:
            revoke_conn.rollback()
        except Exception:
            pass
        revoke_conn.close()
        try:
            reader_conn.rollback()
        except Exception:
            pass
        reader_conn.close()
