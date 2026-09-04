"""Tests for scanner Grafana visibility feature.

Covers:
1. config.is_scanner_visible() returns TRUE for visible scanners.
2. config.is_scanner_visible() returns FALSE for hidden scanners.
3. config.is_scanner_visible() defaults to TRUE for unknown scanners.
4. show_in_grafana column exists on config.scanner_direction_gate.
5. sync_scanner_direction_gate preserves show_in_grafana on conflict.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect_pg():
    """Return a psycopg2 connection or skip if PostgreSQL unavailable."""
    import psycopg2
    try:
        return psycopg2.connect(
            host="localhost", port=5432, database="trad_bot", user="postgres",
        )
    except Exception:
        pytest.skip("PostgreSQL not available")


def _ensure_migration(conn) -> None:
    """Apply migration 005 if the function does not yet exist."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM pg_proc WHERE proname = 'is_scanner_visible'"
        ")"
    )
    if cursor.fetchone()[0]:
        conn.rollback()
        return
    migration = (
        Path(__file__).resolve().parent.parent
        / "sql" / "migrations" / "005_scanner_grafana_visibility.sql"
    )
    if migration.exists():
        sql = migration.read_text(encoding="utf-8")
        cursor.execute(sql)
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIsScannerVisibleFunction:
    """Unit tests for config.is_scanner_visible()."""

    def test_visible_scanner_returns_true(self):
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()
            # Ensure a visible scanner exists
            cursor.execute(
                """
                INSERT INTO config.scanner_direction_gate
                    (scanner_name, direction, status, show_in_grafana, source)
                VALUES ('TEST_VISIBLE_SCANNER', 'LONG', 'ENABLED', TRUE, 'TEST')
                ON CONFLICT (scanner_name, direction) DO UPDATE SET
                    show_in_grafana = TRUE, source = 'TEST'
                """
            )
            conn.commit()

            cursor.execute("SELECT config.is_scanner_visible('TEST_VISIBLE_SCANNER')")
            assert cursor.fetchone()[0] is True
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_direction_gate "
                "WHERE scanner_name = 'TEST_VISIBLE_SCANNER'"
            )
            conn.commit()
            conn.close()

    def test_hidden_scanner_returns_false(self):
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO config.scanner_direction_gate
                    (scanner_name, direction, status, show_in_grafana, source)
                VALUES ('TEST_HIDDEN_SCANNER', 'LONG', 'ENABLED', FALSE, 'TEST')
                ON CONFLICT (scanner_name, direction) DO UPDATE SET
                    show_in_grafana = FALSE, source = 'TEST'
                """
            )
            conn.commit()

            cursor.execute("SELECT config.is_scanner_visible('TEST_HIDDEN_SCANNER')")
            assert cursor.fetchone()[0] is False
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_direction_gate "
                "WHERE scanner_name = 'TEST_HIDDEN_SCANNER'"
            )
            conn.commit()
            conn.close()

    def test_unknown_scanner_defaults_to_true(self):
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT config.is_scanner_visible('NONEXISTENT_SCANNER_12345')"
            )
            assert cursor.fetchone()[0] is True
        finally:
            conn.close()

    def test_show_in_grafana_column_exists(self):
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'config'
                      AND table_name = 'scanner_direction_gate'
                      AND column_name = 'show_in_grafana'
                )
                """
            )
            assert cursor.fetchone()[0] is True
        finally:
            conn.close()


class TestSyncPreservesVisibility:
    """Verify sync_scanner_direction_gate does not overwrite show_in_grafana."""

    def test_sync_preserves_manual_visibility(self):
        """Operator sets show_in_grafana=FALSE; sync must not reset it."""
        from app.db.repository import ScannerRepository

        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()

            # Setup: insert a scanner with show_in_grafana = FALSE via MANUAL source
            cursor.execute(
                """
                INSERT INTO config.scanner_direction_gate
                    (scanner_name, direction, status, show_in_grafana, source)
                VALUES ('TEST_SYNC_VIS', 'LONG', 'ENABLED', FALSE, 'MANUAL')
                ON CONFLICT (scanner_name, direction) DO UPDATE SET
                    show_in_grafana = FALSE, source = 'MANUAL'
                """
            )
            conn.commit()

            # Run sync — the scanner is now registered, but source is MANUAL
            repo = ScannerRepository(
                host="localhost", port=5432, database="trad_bot",
                user="postgres", backend="postgres",
            )
            repo.sync_scanner_direction_gate(
                registered_scanners=["TEST_SYNC_VIS"],
                blocked_combinations=frozenset(),
                regime_whitelist={},
            )

            # Verify show_in_grafana was NOT overwritten
            gate = repo.get_scanner_direction_gate("TEST_SYNC_VIS", "LONG")
            assert gate is not None

            cursor2 = repo._conn.cursor()
            cursor2.execute(
                "SELECT show_in_grafana FROM config.scanner_direction_gate "
                "WHERE scanner_name = 'TEST_SYNC_VIS' AND direction = 'LONG'"
            )
            assert cursor2.fetchone()[0] is False, (
                "sync must not overwrite show_in_grafana for MANUAL rows"
            )

            repo.close()
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_direction_gate "
                "WHERE scanner_name = 'TEST_SYNC_VIS'"
            )
            conn.commit()
            conn.close()
