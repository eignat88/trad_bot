"""Regression tests for scanner direction config sync and fail-closed behavior.

Covers:
1. MOMENTUM_EXHAUSTION_R appears in config snapshot after sync.
2. Separate LONG and SHORT records are created.
3. mart.scanner_direction_status shows the new scanner.
4. Blocklist for MOMENTUM_EXHAUSTION_R works independently from MOMENTUM_EXHAUSTION.
5. Direction gate behaves correctly when scanner/direction is missing from config.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.scanners.orchestrator import ScannerOrchestrator


# ── helpers ──────────────────────────────────────────────────────────

def _make_pg_repo() -> ScannerRepository:
    """Create a PostgreSQL-backed repository for integration tests."""
    repo = ScannerRepository(
        host="localhost", port=5432, database="trad_bot",
        user="postgres", backend="postgres",
    )
    if not repo.ping():
        pytest.skip("PostgreSQL not available")
    return repo


# ── 1. MOMENTUM_EXHAUSTION_R registered in orchestrator ──────────────

def test_momentum_exhaustion_r_in_orchestrator():
    """MOMENTUM_EXHAUSTION_R is registered in the default orchestrator."""
    orch = ScannerOrchestrator()
    assert "MOMENTUM_EXHAUSTION_R" in orch.scanners


def test_momentum_exhaustion_r_scanner_name():
    """The R scanner has the correct scanner_name attribute."""
    orch = ScannerOrchestrator()
    scanner = orch.scanners["MOMENTUM_EXHAUSTION_R"]
    assert scanner.name == "MOMENTUM_EXHAUSTION_R"


# ── 2. Sync creates separate LONG and SHORT records ─────────────────

def test_sync_creates_long_and_short_for_r_scanner(postgres):
    """sync_scanner_direction_config creates both LONG and SHORT rows."""
    repo = postgres
    registered = ["MOMENTUM_EXHAUSTION_R"]
    blocked = frozenset()

    count = repo.sync_scanner_direction_config(registered, blocked)
    assert count == 2  # LONG + SHORT

    cursor = repo._conn.cursor()
    cursor.execute(
        "SELECT direction, enabled, block_reason "
        "FROM dds.scanner_direction_config "
        "WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R' "
        "ORDER BY direction"
    )
    rows = cursor.fetchall()
    assert len(rows) == 2
    assert tuple(rows[0]) == ("LONG", True, None)
    assert tuple(rows[1]) == ("SHORT", True, None)


# ── 3. Sync respects blocked combinations ───────────────────────────

def test_sync_blocks_specified_combinations(postgres):
    """Blocked combinations get enabled=FALSE and block_reason='config_block'."""
    repo = postgres
    registered = ["MOMENTUM_EXHAUSTION_R"]
    blocked = frozenset({("MOMENTUM_EXHAUSTION_R", "LONG")})

    repo.sync_scanner_direction_config(registered, blocked)

    cursor = repo._conn.cursor()
    cursor.execute(
        "SELECT direction, enabled, block_reason "
        "FROM dds.scanner_direction_config "
        "WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R' "
        "ORDER BY direction"
    )
    rows = {r[0]: tuple(r) for r in cursor.fetchall()}
    assert rows["LONG"] == ("LONG", False, "config_block")
    assert rows["SHORT"] == ("SHORT", True, None)


# ── 4. Sync respects regime whitelist ───────────────────────────────

def test_sync_applies_regime_whitelist(postgres):
    """Regime whitelist entries get block_reason='regime_filter'."""
    repo = postgres
    registered = ["MOMENTUM_EXHAUSTION_R"]
    blocked = frozenset()
    whitelist = {"MOMENTUM_EXHAUSTION_R": {"LONG": ("TREND_UP",)}}

    repo.sync_scanner_direction_config(registered, blocked, whitelist)

    cursor = repo._conn.cursor()
    cursor.execute(
        "SELECT direction, enabled, block_reason, regime_whitelist "
        "FROM dds.scanner_direction_config "
        "WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R' "
        "ORDER BY direction"
    )
    rows = {r[0]: tuple(r) for r in cursor.fetchall()}
    assert rows["LONG"][1] is True  # enabled
    assert rows["LONG"][2] == "regime_filter"
    assert list(rows["LONG"][3]) == ["TREND_UP"]
    assert rows["SHORT"][1] is True  # enabled, no regime restriction
    assert rows["SHORT"][2] is None


# ── 5. Idempotent sync ─────────────────────────────────────────────

def test_sync_is_idempotent(postgres):
    """Running sync twice does not create duplicate rows."""
    repo = postgres
    registered = ["MOMENTUM_EXHAUSTION_R"]
    blocked = frozenset()

    repo.sync_scanner_direction_config(registered, blocked)
    repo.sync_scanner_direction_config(registered, blocked)

    cursor = repo._conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM dds.scanner_direction_config "
        "WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R'"
    )
    assert cursor.fetchone()[0] == 2


# ── 6. Blocklist independence from MOMENTUM_EXHAUSTION ─────────────

def test_momentum_exhaustion_r_blocklist_independent(postgres):
    """Blocking MOMENTUM_EXHAUSTION does not affect MOMENTUM_EXHAUSTION_R."""
    repo = postgres
    registered = ["MOMENTUM_EXHAUSTION", "MOMENTUM_EXHAUSTION_R"]
    blocked = frozenset({("MOMENTUM_EXHAUSTION", "LONG")})

    repo.sync_scanner_direction_config(registered, blocked)

    cursor = repo._conn.cursor()
    cursor.execute(
        "SELECT scanner_name, direction, enabled "
        "FROM dds.scanner_direction_config "
        "WHERE scanner_name IN ('MOMENTUM_EXHAUSTION', 'MOMENTUM_EXHAUSTION_R') "
        "ORDER BY scanner_name, direction"
    )
    rows = {(r[0], r[1]): r[2] for r in cursor.fetchall()}
    # MOMENTUM_EXHAUSTION LONG is blocked
    assert rows[("MOMENTUM_EXHAUSTION", "LONG")] is False
    # MOMENTUM_EXHAUSTION SHORT is enabled (not blocked)
    assert rows[("MOMENTUM_EXHAUSTION", "SHORT")] is True
    # MOMENTUM_EXHAUSTION_R both directions are enabled
    assert rows[("MOMENTUM_EXHAUSTION_R", "LONG")] is True
    assert rows[("MOMENTUM_EXHAUSTION_R", "SHORT")] is True


# ── 7. Fail-closed: missing scanner shows CONFIG_MISSING ───────────

def test_direction_status_config_missing_for_unknown_scanner(postgres):
    """A scanner in scanner_run_stat but NOT in scanner_direction_config
    shows CONFIG_MISSING, not ENABLED (fail-closed)."""
    repo = postgres
    # Recover from any prior failed transaction
    try:
        repo._conn.rollback()
    except Exception:
        pass
    # Ensure MOMENTUM_EXHAUSTION_R has config rows
    repo.sync_scanner_direction_config(
        ["MOMENTUM_EXHAUSTION_R"], frozenset(),
    )

    cursor = repo._conn.cursor()
    # Create a dummy scanner_run_stat entry for an unknown scanner
    cursor.execute("""
        INSERT INTO dds.scanner_run (started_at, universe_mode, symbols_total, status)
        VALUES (now(), 'static', 1, 'COMPLETED')
        RETURNING run_id
    """)
    run_id = cursor.fetchone()[0]
    cursor.execute("""
        INSERT INTO dds.scanner_run_stat (run_id, scanner_name, symbols_scanned, candidates_found, setups_saved, errors_count, duration_ms)
        VALUES (%s, 'UNKNOWN_SCANNER_XYZ', 10, 0, 0, 0, 1.0)
    """, (run_id,))
    repo._conn.commit()

    # Query the view
    cursor.execute("""
        SELECT scanner_name, long_status, short_status
        FROM mart.scanner_direction_status
        WHERE scanner_name = 'UNKNOWN_SCANNER_XYZ'
    """)
    row = cursor.fetchone()
    assert row is not None, "UNKNOWN_SCANNER_XYZ should appear in the view"
    assert row[1] == "CONFIG_MISSING", f"Expected CONFIG_MISSING, got {row[1]}"
    assert row[2] == "CONFIG_MISSING", f"Expected CONFIG_MISSING, got {row[2]}"

    # Cleanup
    cursor.execute("DELETE FROM dds.scanner_run_stat WHERE run_id = %s", (run_id,))
    cursor.execute("DELETE FROM dds.scanner_run WHERE run_id = %s", (run_id,))
    repo._conn.commit()


# ── 8. View shows correct status for MOMENTUM_EXHAUSTION_R ──────────

def test_direction_status_shows_r_scanner(postgres):
    """mart.scanner_direction_status shows MOMENTUM_EXHAUSTION_R after sync."""
    repo = postgres
    # Recover from any prior failed transaction
    try:
        repo._conn.rollback()
    except Exception:
        pass
    # Sync config
    repo.sync_scanner_direction_config(
        ["MOMENTUM_EXHAUSTION_R"], frozenset(),
    )

    cursor = repo._conn.cursor()
    # Ensure scanner_run_stat has an entry for MOMENTUM_EXHAUSTION_R
    cursor.execute(
        "SELECT 1 FROM dds.scanner_run_stat WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R'"
    )
    if cursor.fetchone() is None:
        cursor.execute("""
            INSERT INTO dds.scanner_run (started_at, universe_mode, symbols_total, status)
            VALUES (now(), 'static', 1, 'COMPLETED')
            RETURNING run_id
        """)
        run_id = cursor.fetchone()[0]
        cursor.execute("""
            INSERT INTO dds.scanner_run_stat (run_id, scanner_name, symbols_scanned, candidates_found, setups_saved, errors_count, duration_ms)
            VALUES (%s, 'MOMENTUM_EXHAUSTION_R', 10, 1, 1, 0, 1.0)
        """, (run_id,))
        repo._conn.commit()

    cursor.execute("""
        SELECT scanner_name, long_status, short_status
        FROM mart.scanner_direction_status
        WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R'
    """)
    row = cursor.fetchone()
    assert row is not None, "MOMENTUM_EXHAUSTION_R should appear in the view"
    assert row[1] == "ENABLED", f"Expected ENABLED, got {row[1]}"
    assert row[2] == "ENABLED", f"Expected ENABLED, got {row[2]}"


# ── 9. All orchestrator scanners have config after sync ─────────────

def test_all_orchestrator_scanners_get_config(postgres):
    """Every scanner registered in the orchestrator gets config rows after sync."""
    repo = postgres
    orch = ScannerOrchestrator()
    registered = list(orch.scanners.keys())
    blocked = frozenset(Settings().blocked_scanner_directions)

    repo.sync_scanner_direction_config(registered, blocked)

    cursor = repo._conn.cursor()
    cursor.execute("SELECT DISTINCT scanner_name FROM dds.scanner_direction_config")
    db_scanners = {row[0] for row in cursor.fetchall()}

    for name in registered:
        assert name in db_scanners, f"{name} missing from dds.scanner_direction_config"


# ── 10. Config.yaml sync roundtrip ──────────────────────────────────

def test_config_yaml_blocked_not_affecting_r_scanner():
    """MOMENTUM_EXHAUSTION_R is NOT in the default blocked_scanner_directions."""
    settings = Settings()
    blocked = set(settings.blocked_scanner_directions)
    # R scanner should not be blocked by default
    assert ("MOMENTUM_EXHAUSTION_R", "LONG") not in blocked
    assert ("MOMENTUM_EXHAUSTION_R", "SHORT") not in blocked


# ── PostgreSQL fixture ───────────────────────────────────────────────

def _ensure_direction_config_table(repo: ScannerRepository) -> bool:
    """Create dds.scanner_direction_config and mart.scanner_direction_status if missing.
    Returns True if the table exists."""
    if not repo._use_pg:
        return False
    cursor = repo._conn.cursor()

    # Ensure schemas exist
    for schema in ("dds", "mart"):
        try:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        except Exception:
            pass
    repo._conn.commit()

    # Check / create dds.scanner_direction_config
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'dds' AND table_name = 'scanner_direction_config'
        )
    """)
    exists = cursor.fetchone()[0]
    if not exists:
        migration_path = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "001_scanner_direction_config.sql"
        if migration_path.exists():
            sql = migration_path.read_text(encoding="utf-8")
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        cursor.execute(stmt)
                    except Exception:
                        pass
            repo._conn.commit()
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'dds' AND table_name = 'scanner_direction_config'
                )
            """)
            exists = cursor.fetchone()[0]

    # Ensure mart.scanner_direction_status view exists
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'mart' AND table_name = 'scanner_direction_status'
        )
    """)
    view_exists = cursor.fetchone()[0]
    if not view_exists:
        view_sql = """
        CREATE OR REPLACE VIEW mart.scanner_direction_status AS
        SELECT
            scanners.scanner_name,
            CASE
                WHEN sdc_long.scanner_name IS NULL          THEN 'CONFIG_MISSING'
                WHEN sdc_long.enabled                        THEN 'ENABLED'
                WHEN sdc_long.block_reason = 'regime_filter' THEN 'REGIME'
                ELSE 'BLOCKED'
            END AS long_status,
            CASE
                WHEN sdc_short.scanner_name IS NULL          THEN 'CONFIG_MISSING'
                WHEN sdc_short.enabled                       THEN 'ENABLED'
                WHEN sdc_short.block_reason = 'regime_filter' THEN 'REGIME'
                ELSE 'BLOCKED'
            END AS short_status
        FROM (
            SELECT DISTINCT scanner_name
            FROM dds.scanner_run_stat
        ) scanners
        LEFT JOIN dds.scanner_direction_config sdc_long
            ON sdc_long.scanner_name = scanners.scanner_name AND sdc_long.direction = 'LONG'
        LEFT JOIN dds.scanner_direction_config sdc_short
            ON sdc_short.scanner_name = scanners.scanner_name AND sdc_short.direction = 'SHORT'
        ORDER BY scanners.scanner_name;
        """
        try:
            cursor.execute(view_sql)
            repo._conn.commit()
        except Exception:
            repo._conn.rollback()

    return exists


@pytest.fixture(scope="session")
def postgres():
    """Provide a PostgreSQL repository for integration tests."""
    repo = _make_pg_repo()
    has_table = _ensure_direction_config_table(repo)
    if not has_table:
        repo.close()
        pytest.skip("dds.scanner_direction_config table not available")
    yield repo
    repo.close()
