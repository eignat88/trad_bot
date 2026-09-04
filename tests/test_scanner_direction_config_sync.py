"""Regression tests for scanner direction config sync and fail-closed behavior.

Covers:
1. MOMENTUM_EXHAUSTION_R appears in config snapshot after sync.
2. Separate LONG and SHORT records are created.
3. mart.scanner_direction_status shows the new scanner.
4. Blocklist for MOMENTUM_EXHAUSTION_R works independently from MOMENTUM_EXHAUSTION.
5. Direction gate behaves correctly when scanner/direction is missing from config.
6. ScannerDirectionGatePolicy fail-closed and static fallback behavior.
7. sync_scanner_direction_gate writes to config.scanner_direction_gate (canonical source).
8. Manual operator blocks (source='MANUAL') survive service restart.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_REGIME,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)
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


def _policy(*gates):
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates}, {}
    )


# ═══════════════════════════════════════════════════════════════════
# PART A: ScannerDirectionGatePolicy (from main)
# ═══════════════════════════════════════════════════════════════════

def test_enabled_gate_allows_candidate():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "LONG", GATE_ENABLED)
    ).evaluate("ACTIVE", "LONG", "RANGE")
    assert decision.allowed is True
    assert decision.status == GATE_ENABLED


def test_blocked_gate_rejects_candidate():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "SHORT", GATE_BLOCKED, reason="validation")
    ).evaluate("ACTIVE", "SHORT", "RANGE")
    assert decision.allowed is False
    assert decision.reason_code == "DIRECTION_GATE_BLOCKED"


@pytest.mark.parametrize("regime, allowed", [("TREND_UP", True), ("RANGE", False), ("TREND_DOWN", False)])
def test_regime_gate_checks_market_regime(regime, allowed):
    decision = _policy(
        ScannerDirectionGate("TREND_PULLBACK_V2", "LONG", GATE_REGIME, ("TREND_UP",))
    ).evaluate("TREND_PULLBACK_V2", "LONG", regime)
    assert decision.allowed is allowed
    assert decision.status == GATE_REGIME


def test_static_fallback_preserves_current_blocklist_and_regime_policy():
    policy = ScannerDirectionGatePolicy.static_fallback(
        ["TREND_PULLBACK_V2", "BREAKOUT_RETEST"],
        [("BREAKOUT_RETEST", "LONG")],
        {"TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)}},
    )
    assert not policy.evaluate("BREAKOUT_RETEST", "LONG", "TREND_UP").allowed
    assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "TREND_UP").allowed
    assert not policy.evaluate("TREND_PULLBACK_V2", "LONG", "RANGE").allowed


def test_database_failure_uses_static_fallback():
    class FailingRepository:
        def get_scanner_direction_gates(self):
            raise RuntimeError("database unavailable")

    policy = ScannerDirectionGatePolicy.load_for_cycle(
        FailingRepository(),
        scanner_names=["ACTIVE"],
        blocked_combinations=[("ACTIVE", "SHORT")],
        regime_whitelist={},
    )
    assert policy.evaluate("ACTIVE", "LONG", "RANGE").allowed
    assert not policy.evaluate("ACTIVE", "SHORT", "RANGE").allowed


def test_missing_gate_is_fail_closed():
    policy = ScannerDirectionGatePolicy.static_fallback([], [], {})
    decision = policy.evaluate("NEW_SCANNER", "LONG", "TREND_UP")
    assert not decision.allowed
    assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"


def test_runtime_snapshot_is_loaded_once_per_cycle():
    class Repository:
        calls = 0

        def get_scanner_direction_gates(self):
            self.calls += 1
            return [ScannerDirectionGate("ACTIVE", "LONG", GATE_ENABLED)]

    repository = Repository()
    policy = ScannerDirectionGatePolicy.load_for_cycle(
        repository, scanner_names=["ACTIVE"], blocked_combinations=[], regime_whitelist={}
    )
    for _ in range(20):
        assert policy.evaluate("ACTIVE", "LONG", "RANGE").allowed
    assert repository.calls == 1


def test_unknown_status_never_fails_open():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "LONG", "TYPO")
    ).evaluate("ACTIVE", "LONG", "RANGE")
    assert not decision.allowed
    assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# PART B: MOMENTUM_EXHAUSTION_R in orchestrator (unit tests)
# ═══════════════════════════════════════════════════════════════════

def test_momentum_exhaustion_r_in_orchestrator():
    """MOMENTUM_EXHAUSTION_R is registered in the default orchestrator."""
    orch = ScannerOrchestrator()
    assert "MOMENTUM_EXHAUSTION_R" in orch.scanners


def test_momentum_exhaustion_r_scanner_name():
    """The R scanner has the correct scanner_name attribute."""
    orch = ScannerOrchestrator()
    scanner = orch.scanners["MOMENTUM_EXHAUSTION_R"]
    assert scanner.name == "MOMENTUM_EXHAUSTION_R"


def test_config_yaml_blocked_not_affecting_r_scanner():
    """MOMENTUM_EXHAUSTION_R is NOT in the default blocked_scanner_directions."""
    settings = Settings()
    blocked = set(settings.blocked_scanner_directions)
    assert ("MOMENTUM_EXHAUSTION_R", "LONG") not in blocked
    assert ("MOMENTUM_EXHAUSTION_R", "SHORT") not in blocked


# ═══════════════════════════════════════════════════════════════════
# PART C: sync_scanner_direction_config (integration tests)
# ═══════════════════════════════════════════════════════════════════

def _ensure_direction_config_table(repo: ScannerRepository) -> bool:
    """Create dds.scanner_direction_config and mart views if missing."""
    if not repo._use_pg:
        return False
    cursor = repo._conn.cursor()

    for schema in ("dds", "mart", "config"):
        try:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
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


def test_sync_creates_long_and_short_for_r_scanner(postgres):
    """sync_scanner_direction_config creates both LONG and SHORT rows."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
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


def test_sync_blocks_specified_combinations(postgres):
    """Blocked combinations get enabled=FALSE and block_reason='config_block'."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
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


def test_sync_applies_regime_whitelist(postgres):
    """Regime whitelist entries get block_reason='regime_filter'."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
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


def test_sync_is_idempotent(postgres):
    """Running sync twice does not create duplicate rows."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
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


def test_momentum_exhaustion_r_blocklist_independent(postgres):
    """Blocking MOMENTUM_EXHAUSTION does not affect MOMENTUM_EXHAUSTION_R."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
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
    assert rows[("MOMENTUM_EXHAUSTION", "LONG")] is False
    assert rows[("MOMENTUM_EXHAUSTION", "SHORT")] is True
    assert rows[("MOMENTUM_EXHAUSTION_R", "LONG")] is True
    assert rows[("MOMENTUM_EXHAUSTION_R", "SHORT")] is True


def test_all_orchestrator_scanners_get_config(postgres):
    """Every scanner registered in the orchestrator gets config rows after sync."""
    repo = postgres
    try:
        repo._conn.rollback()
    except Exception:
        pass
    orch = ScannerOrchestrator()
    registered = list(orch.scanners.keys())
    blocked = frozenset(Settings().blocked_scanner_directions)

    repo.sync_scanner_direction_config(registered, blocked)

    cursor = repo._conn.cursor()
    cursor.execute("SELECT DISTINCT scanner_name FROM dds.scanner_direction_config")
    db_scanners = {row[0] for row in cursor.fetchall()}

    for name in registered:
        assert name in db_scanners, f"{name} missing from dds.scanner_direction_config"


# ═══════════════════════════════════════════════════════════════════
# PART D: sync_scanner_direction_gate – canonical runtime source
# ═══════════════════════════════════════════════════════════════════

def _ensure_gate_table(pg_conn) -> None:
    """Ensure config.scanner_direction_gate exists via migration.

    Applies the full migration file as a single string (the migration
    contains $$-delimited function bodies that cannot be split on ';').
    """
    cursor = pg_conn.cursor()
    cursor.execute("CREATE SCHEMA IF NOT EXISTS config")
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate'
        )
    """)
    exists = cursor.fetchone()[0]
    if exists:
        pg_conn.commit()
        return
    migration = (
        Path(__file__).resolve().parent.parent
        / "sql" / "migrations" / "004_scanner_direction_gate.sql"
    )
    if migration.exists():
        sql = migration.read_text(encoding="utf-8")
        cursor.execute(sql)
    pg_conn.commit()


def _connect_pg():
    """Return a psycopg2 connection or skip if PostgreSQL unavailable."""
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="localhost", port=5432, database="trad_bot", user="postgres",
        )
        return conn
    except Exception:
        pytest.skip("PostgreSQL not available")


def _make_gate_repo():
    """Create a fresh ScannerRepository for gate tests."""
    return ScannerRepository(
        host="localhost", port=5432, database="trad_bot",
        user="postgres", backend="postgres",
    )


def test_manual_block_survives_service_restart():
    """A manual operator block (source='MANUAL') is never overwritten by
    sync_scanner_direction_gate, simulating the scenario where a human
    blocks a scanner direction and the service restarts.

    Regression: after PR #46 the runtime gate switched to
    config.scanner_direction_gate but sync still wrote to
    dds.scanner_direction_config, causing MOMENTUM_EXHAUSTION_R to
    show as CONFIG_MISSING.
    """
    pg_conn = _connect_pg()
    cursor = pg_conn.cursor()
    try:
        _ensure_gate_table(pg_conn)

        # --- Step 1: Insert a MANUAL block.
        cursor.execute(
            """
            INSERT INTO config.scanner_direction_gate
                (scanner_name, direction, status, reason, source, updated_at, updated_by)
            VALUES ('TEST_SCANNER_MANUAL', 'LONG', 'BLOCKED', 'operator decision', 'MANUAL', now(), 'admin')
            ON CONFLICT (scanner_name, direction) DO UPDATE SET
                status = 'BLOCKED', source = 'MANUAL', reason = 'operator decision',
                updated_at = now(), updated_by = 'admin'
            """
        )
        pg_conn.commit()

        # --- Step 2: Run sync_scanner_direction_gate — the scanner is NOT in
        # the registered list, so it would not appear in the snapshot, but the
        # existing MANUAL row must survive.
        repo = _make_gate_repo()
        repo.sync_scanner_direction_gate(
            registered_scanners=["UNRELATED_SCANNER"],
            blocked_combinations=frozenset(),
            regime_whitelist={},
        )

        # --- Step 3: Verify the MANUAL block survived.
        gate = repo.get_scanner_direction_gate("TEST_SCANNER_MANUAL", "LONG")
        assert gate is not None, "MANUAL gate was deleted by sync"
        assert gate.status == "BLOCKED", f"expected BLOCKED, got {gate.status}"

        # Verify source is still MANUAL.
        cursor.execute(
            "SELECT source FROM config.scanner_direction_gate "
            "WHERE scanner_name = 'TEST_SCANNER_MANUAL' AND direction = 'LONG'"
        )
        source = cursor.fetchone()[0]
        assert source == "MANUAL", f"expected MANUAL, got {source}"

        repo.close()
    finally:
        cursor.execute(
            "DELETE FROM config.scanner_direction_gate "
            "WHERE scanner_name = 'TEST_SCANNER_MANUAL'"
        )
        pg_conn.commit()
        pg_conn.close()


def test_sync_scanner_direction_gate_creates_all_combinations():
    """sync_scanner_direction_gate registers all scanners × (LONG, SHORT)
    and marks blocked / regime / enabled correctly."""
    pg_conn = _connect_pg()
    cursor = pg_conn.cursor()
    try:
        _ensure_gate_table(pg_conn)

        # Cleanup test rows first.
        cursor.execute(
            "DELETE FROM config.scanner_direction_gate "
            "WHERE scanner_name LIKE 'TEST_GATE_%'"
        )
        pg_conn.commit()

        repo = _make_gate_repo()
        counts = repo.sync_scanner_direction_gate(
            registered_scanners=["TEST_GATE_A", "TEST_GATE_B"],
            blocked_combinations=frozenset({("TEST_GATE_A", "SHORT")}),
            regime_whitelist={"TEST_GATE_B": {"LONG": ("TREND_UP",)}},
        )

        assert counts["combinations"] == 4
        assert counts["scanners"] == 2
        assert counts["blocked"] == 1
        assert counts["regime"] == 1
        assert counts["enabled"] == 2

        # Verify individual gates.
        gate_a_long = repo.get_scanner_direction_gate("TEST_GATE_A", "LONG")
        assert gate_a_long is not None
        assert gate_a_long.status == "ENABLED"

        gate_a_short = repo.get_scanner_direction_gate("TEST_GATE_A", "SHORT")
        assert gate_a_short is not None
        assert gate_a_short.status == "BLOCKED"

        gate_b_long = repo.get_scanner_direction_gate("TEST_GATE_B", "LONG")
        assert gate_b_long is not None
        assert gate_b_long.status == "REGIME"
        assert gate_b_long.allowed_regimes == ("TREND_UP",)

        gate_b_short = repo.get_scanner_direction_gate("TEST_GATE_B", "SHORT")
        assert gate_b_short is not None
        assert gate_b_short.status == "ENABLED"

        repo.close()
    finally:
        cursor.execute(
            "DELETE FROM config.scanner_direction_gate "
            "WHERE scanner_name LIKE 'TEST_GATE_%'"
        )
        pg_conn.commit()
        pg_conn.close()


def test_momentum_exhaustion_r_created_as_enabled():
    """MOMENTUM_EXHAUSTION_R LONG and SHORT are created as ENABLED
    when not in blocked_scanner_directions."""
    pg_conn = _connect_pg()
    cursor = pg_conn.cursor()
    try:
        _ensure_gate_table(pg_conn)

        repo = _make_gate_repo()
        counts = repo.sync_scanner_direction_gate(
            registered_scanners=["MOMENTUM_EXHAUSTION_R"],
            blocked_combinations=frozenset(),
            regime_whitelist={},
        )

        gate_long = repo.get_scanner_direction_gate("MOMENTUM_EXHAUSTION_R", "LONG")
        assert gate_long is not None
        assert gate_long.status == "ENABLED"

        gate_short = repo.get_scanner_direction_gate("MOMENTUM_EXHAUSTION_R", "SHORT")
        assert gate_short is not None
        assert gate_short.status == "ENABLED"

        repo.close()
    finally:
        cursor.execute(
            "DELETE FROM config.scanner_direction_gate "
            "WHERE scanner_name = 'MOMENTUM_EXHAUSTION_R'"
        )
        pg_conn.commit()
        pg_conn.close()
