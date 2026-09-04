"""Tests for scanner Grafana visibility feature.

Covers:
1. config.is_scanner_visible() returns TRUE for visible scanners.
2. config.is_scanner_visible() returns FALSE for hidden scanners.
3. config.is_scanner_visible() defaults to TRUE for unknown scanners.
4. show_in_grafana column exists on config.scanner_direction_gate.
5. sync_scanner_direction_gate preserves show_in_grafana on conflict.
6. scanner_grafana_visibility table exists and works.
7. Historical scanners (not in direction_gate) can be hidden via visibility table.
8. Visibility table takes precedence over direction_gate.
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
    """Apply migrations 005 and 006 if they do not yet exist."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM pg_proc WHERE proname = 'is_scanner_visible'"
        ")"
    )
    if not cursor.fetchone()[0]:
        migration = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "005_scanner_grafana_visibility.sql"
        )
        if migration.exists():
            sql = migration.read_text(encoding="utf-8")
            cursor.execute(sql)
        conn.commit()

    # Apply migration 006 (separate table)
    cursor.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables "
        "  WHERE table_schema = 'config' "
        "  AND table_name = 'scanner_grafana_visibility'"
        ")"
    )
    if not cursor.fetchone()[0]:
        migration006 = (
            Path(__file__).resolve().parent.parent
            / "sql" / "migrations" / "006_scanner_grafana_visibility_separate_table.sql"
        )
        if migration006.exists():
            sql = migration006.read_text(encoding="utf-8")
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


class TestScannerGrafanaVisibilityTable:
    """Tests for the dedicated scanner_grafana_visibility table."""

    def test_table_exists(self):
        """scanner_grafana_visibility table must exist after migration."""
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'config'
                      AND table_name = 'scanner_grafana_visibility'
                )
                """
            )
            assert cursor.fetchone()[0] is True
        finally:
            conn.close()

    def test_hide_historical_scanner(self):
        """Historical scanner not in direction_gate can be hidden."""
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()

            # TREND_PULLBACK is a historical scanner - not in direction_gate
            # Insert into visibility table to hide it
            cursor.execute(
                """
                INSERT INTO config.scanner_grafana_visibility
                    (scanner_name, show_in_grafana, updated_by)
                VALUES ('TREND_PULLBACK', FALSE, 'MANUAL')
                ON CONFLICT (scanner_name) DO UPDATE SET
                    show_in_grafana = FALSE, updated_by = 'MANUAL'
                """
            )
            conn.commit()

            # Verify: is_scanner_visible should return FALSE
            cursor.execute("SELECT config.is_scanner_visible('TREND_PULLBACK')")
            assert cursor.fetchone()[0] is False
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_grafana_visibility "
                "WHERE scanner_name = 'TREND_PULLBACK'"
            )
            conn.commit()
            conn.close()

    def test_independent_control_v2_v3(self):
        """TREND_PULLBACK_V2 and V3 can be controlled independently."""
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()

            # V2: hide
            cursor.execute(
                """
                INSERT INTO config.scanner_grafana_visibility
                    (scanner_name, show_in_grafana, updated_by)
                VALUES ('TREND_PULLBACK_V2', FALSE, 'MANUAL')
                ON CONFLICT (scanner_name) DO UPDATE SET
                    show_in_grafana = FALSE, updated_by = 'MANUAL'
                """
            )
            # V3: show (explicitly set TRUE)
            cursor.execute(
                """
                INSERT INTO config.scanner_grafana_visibility
                    (scanner_name, show_in_grafana, updated_by)
                VALUES ('TREND_PULLBACK_V3', TRUE, 'MANUAL')
                ON CONFLICT (scanner_name) DO UPDATE SET
                    show_in_grafana = TRUE, updated_by = 'MANUAL'
                """
            )
            conn.commit()

            # Verify independent control
            cursor.execute("SELECT config.is_scanner_visible('TREND_PULLBACK_V2')")
            assert cursor.fetchone()[0] is False

            cursor.execute("SELECT config.is_scanner_visible('TREND_PULLBACK_V3')")
            assert cursor.fetchone()[0] is True
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_grafana_visibility "
                "WHERE scanner_name IN ('TREND_PULLBACK_V2', 'TREND_PULLBACK_V3')"
            )
            conn.commit()
            conn.close()

    def test_visibility_table_takes_precedence(self):
        """Visibility table overrides direction_gate setting."""
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()

            # Insert scanner into direction_gate with show_in_grafana = TRUE
            cursor.execute(
                """
                INSERT INTO config.scanner_direction_gate
                    (scanner_name, direction, status, show_in_grafana, source)
                VALUES ('TEST_PRECEDENCE', 'LONG', 'ENABLED', TRUE, 'TEST')
                ON CONFLICT (scanner_name, direction) DO UPDATE SET
                    show_in_grafana = TRUE, source = 'TEST'
                """
            )
            # Insert into visibility table with show_in_grafana = FALSE
            cursor.execute(
                """
                INSERT INTO config.scanner_grafana_visibility
                    (scanner_name, show_in_grafana, updated_by)
                VALUES ('TEST_PRECEDENCE', FALSE, 'MANUAL')
                ON CONFLICT (scanner_name) DO UPDATE SET
                    show_in_grafana = FALSE, updated_by = 'MANUAL'
                """
            )
            conn.commit()

            # Visibility table should take precedence
            cursor.execute("SELECT config.is_scanner_visible('TEST_PRECEDENCE')")
            assert cursor.fetchone()[0] is False
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_grafana_visibility "
                "WHERE scanner_name = 'TEST_PRECEDENCE'"
            )
            cursor.execute(
                "DELETE FROM config.scanner_direction_gate "
                "WHERE scanner_name = 'TEST_PRECEDENCE'"
            )
            conn.commit()
            conn.close()

    def test_upsert_sql_pattern(self):
        """Verify the exact SQL pattern from the task description works."""
        conn = _connect_pg()
        try:
            _ensure_migration(conn)
            cursor = conn.cursor()

            # Execute the exact SQL from the task description
            cursor.execute(
                """
                INSERT INTO config.scanner_grafana_visibility
                    (scanner_name, show_in_grafana)
                VALUES
                    ('TREND_PULLBACK', FALSE)
                ON CONFLICT (scanner_name)
                DO UPDATE SET show_in_grafana = EXCLUDED.show_in_grafana
                """
            )
            conn.commit()

            # Verify: should return FALSE
            cursor.execute("SELECT config.is_scanner_visible('TREND_PULLBACK')")
            result = cursor.fetchone()[0]
            assert result is False, f"Expected FALSE, got {result}"
        finally:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM config.scanner_grafana_visibility "
                "WHERE scanner_name = 'TREND_PULLBACK'"
            )
            conn.commit()
            conn.close()
