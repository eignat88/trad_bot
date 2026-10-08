"""Integration tests for migration 062 activation boundary record.

These tests exercise the migration against a real local PostgreSQL
instance. They are skipped unless ``SRR_PERSISTENCE_POSTGRES_IT=1`` is set,
mirroring the existing SRR integration-test pattern.

The migration is applied to a dedicated throwaway database named
``trad_bot_srr_activation_it_20261008``; the existing integration database
``trad_bot_srr_persistence_it_20261008`` is used for the supporting bootstrap
schema.
"""
from __future__ import annotations

import os
import uuid

import pytest

pg8000 = pytest.importorskip("pg8000")

ENABLE = "SRR_PERSISTENCE_POSTGRES_IT"
IT_DB = "trad_bot_srr_persistence_it_20261008"
ACTIVATION_DB = "trad_bot_srr_activation_it_20261008"

SRR_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
BOUNDARY_VERSION = "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1"


def _connect(dbname: str):
    if os.getenv("TEST_DB_HOST", "127.0.0.1") != "127.0.0.1":
        pytest.fail("STOP: TEST_DB_HOST must be 127.0.0.1")
    conn = pg8000.connect(
        host="127.0.0.1",
        port=5432,
        database=dbname,
        user="postgres",
        timeout=5,
    )
    return conn


@pytest.fixture
def conn():
    if os.getenv(ENABLE) != "1":
        pytest.skip(f"Set {ENABLE}=1 to run local PostgreSQL integration tests")
    c = _connect(IT_DB)
    yield c
    c.close()


def _ensure_activation_db():
    """Create the dedicated activation test database if missing."""
    admin = _connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (ACTIVATION_DB,),
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{ACTIVATION_DB}"')
    finally:
        admin.close()


def _drop_activation_db():
    admin = _connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (ACTIVATION_DB,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{ACTIVATION_DB}"')
    finally:
        admin.close()


def _bootstrap_activation_db():
    """Apply the IT bootstrap (schema + supporting tables) to the throwaway db."""
    bootstrap_path = os.path.join(
        os.path.dirname(__file__), "..", "srr_persistence_it_bootstrap.sql"
    )
    with open(bootstrap_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    conn = _connect(ACTIVATION_DB)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _apply_migration_062():
    migration_path = os.path.join(
        os.path.dirname(__file__), "..", "sql", "migrations",
        "062_srr_short_writer_activation_boundary.sql",
    )
    with open(migration_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    conn = _connect(ACTIVATION_DB)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


@pytest.fixture
def activation_conn():
    """Fixture that provisions the throwaway activation database."""
    if os.getenv(ENABLE) != "1":
        pytest.skip(f"Set {ENABLE}=1 to run local PostgreSQL integration tests")
    _ensure_activation_db()
    _bootstrap_activation_db()
    _apply_migration_062()
    c = _connect(ACTIVATION_DB)
    yield c
    c.close()
    _drop_activation_db()


def test_migration_062_creates_table_and_constraints(activation_conn):
    with activation_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('research.srr_short_writer_activation')"
        )
        assert cur.fetchone()[0] is not None

        cur.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'research.srr_short_writer_activation'::regclass "
            "ORDER BY conname"
        )
        names = {row[0] for row in cur.fetchall()}
        assert "srr_short_writer_activation_pkey" in names
        assert any("experiment_id" in n for n in names)
        assert any("direction" in n for n in names)
        assert any("boundary_version" in n for n in names)
        assert any("status" in n for n in names)


def test_migration_062_creates_block_triggers(activation_conn):
    with activation_conn.cursor() as cur:
        cur.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'research.srr_short_writer_activation'::regclass "
            "AND NOT tgisinternal ORDER BY tgname"
        )
        names = {row[0] for row in cur.fetchall()}
        assert "trg_srr_short_writer_activation_updated_at" in names
        assert "trg_srr_short_writer_activation_block_change" in names
        assert "trg_srr_short_writer_activation_block_delete" in names


def test_activation_record_immutable_fields(activation_conn):
    """experiment_id, direction, boundary_version, activation_ts never change."""
    with activation_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                SRR_EXPERIMENT_ID,
                BOUNDARY_VERSION,
                "2026-10-08T12:00:00Z",
                "SHORT",
                "PENDING",
                "test",
            ),
        )
        activation_conn.commit()

        # Attempt to change activation_ts (still in PENDING).
        with pytest.raises(pg8000.dbapi.Error, match="immutable"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET activation_ts = %s WHERE experiment_id = %s",
                ("2026-10-08T13:00:00Z", SRR_EXPERIMENT_ID),
            )
        activation_conn.rollback()

        # Attempt to change experiment_id.
        with pytest.raises(pg8000.dbapi.Error, match="immutable"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET experiment_id = %s WHERE experiment_id = %s",
                ("WRONG", SRR_EXPERIMENT_ID),
            )
        activation_conn.rollback()

        # Attempt to change direction.
        with pytest.raises(pg8000.dbapi.Error, match="immutable"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET direction = %s WHERE experiment_id = %s",
                ("LONG", SRR_EXPERIMENT_ID),
            )
        activation_conn.rollback()

        # Attempt to change boundary_version.
        with pytest.raises(pg8000.dbapi.Error, match="immutable"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET boundary_version = %s WHERE experiment_id = %s",
                ("OTHER", SRR_EXPERIMENT_ID),
            )
        activation_conn.rollback()


def test_activation_state_machine_transitions(activation_conn):
    """Only PENDING→ACTIVE, PENDING→REVOKED, ACTIVE→REVOKED are permitted."""
    with activation_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                SRR_EXPERIMENT_ID,
                BOUNDARY_VERSION,
                "2026-10-08T12:00:00Z",
                "SHORT",
                "PENDING",
            ),
        )
        activation_conn.commit()

        # PENDING → ACTIVE is permitted.
        cur.execute(
            "UPDATE research.srr_short_writer_activation "
            "SET status = 'ACTIVE' WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        activation_conn.commit()

        # ACTIVE → PENDING is not permitted.
        with pytest.raises(pg8000.dbapi.Error, match="not permitted"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET status = 'PENDING' WHERE experiment_id = %s",
                (SRR_EXPERIMENT_ID,),
            )
        activation_conn.rollback()

        # ACTIVE → REVOKED is permitted.
        cur.execute(
            "UPDATE research.srr_short_writer_activation "
            "SET status = 'REVOKED' WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        activation_conn.commit()

        # REVOKED → ACTIVE is not permitted (no reactivation).
        with pytest.raises(pg8000.dbapi.Error, match="not permitted"):
            cur.execute(
                "UPDATE research.srr_short_writer_activation "
                "SET status = 'ACTIVE' WHERE experiment_id = %s",
                (SRR_EXPERIMENT_ID,),
            )
        activation_conn.rollback()


def test_activation_pending_to_revoked_is_permitted(activation_conn):
    """Explicit cancel-before-activation path (PENDING → REVOKED)."""
    with activation_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                SRR_EXPERIMENT_ID,
                BOUNDARY_VERSION,
                "2026-10-08T12:00:00Z",
                "SHORT",
                "PENDING",
            ),
        )
        activation_conn.commit()

        cur.execute(
            "UPDATE research.srr_short_writer_activation "
            "SET status = 'REVOKED' WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        activation_conn.commit()

        cur.execute(
            "SELECT status FROM research.srr_short_writer_activation "
            "WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        assert cur.fetchone()[0] == "REVOKED"


def test_activation_delete_is_blocked(activation_conn):
    """DELETE of an activation record always raises."""
    with activation_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                SRR_EXPERIMENT_ID,
                BOUNDARY_VERSION,
                "2026-10-08T12:00:00Z",
                "SHORT",
                "PENDING",
            ),
        )
        activation_conn.commit()

        with pytest.raises(pg8000.dbapi.Error, match="cannot be deleted"):
            cur.execute(
                "DELETE FROM research.srr_short_writer_activation "
                "WHERE experiment_id = %s",
                (SRR_EXPERIMENT_ID,),
            )
        activation_conn.rollback()


def test_status_change_alone_is_allowed(activation_conn):
    """Changing only status and notes is permitted."""
    with activation_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, boundary_version, activation_ts, direction, status, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                SRR_EXPERIMENT_ID,
                BOUNDARY_VERSION,
                "2026-10-08T12:00:00Z",
                "SHORT",
                "PENDING",
                "initial",
            ),
        )
        activation_conn.commit()

        cur.execute(
            "UPDATE research.srr_short_writer_activation "
            "SET status = 'ACTIVE', notes = 'activated per operator' "
            "WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        activation_conn.commit()

        cur.execute(
            "SELECT status, notes FROM research.srr_short_writer_activation "
            "WHERE experiment_id = %s",
            (SRR_EXPERIMENT_ID,),
        )
        row = cur.fetchone()
        assert row[0] == "ACTIVE"
        assert row[1] == "activated per operator"
