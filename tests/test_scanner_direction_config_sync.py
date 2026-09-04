import pytest

from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_REGIME,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)


def _policy(*gates):
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates}, {}
    )


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
# PART D: sync_scanner_direction_gate – manual block survival
# ═══════════════════════════════════════════════════════════════════

def _ensure_gate_table(pg_conn) -> None:
    """Ensure config.scanner_direction_gate exists via migration.

    Applies the full migration file as a single string (the migration
    contains $$-delimited function bodies that cannot be split on ';').
    """
    from pathlib import Path
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


def _make_repo():
    from app.db.repository import ScannerRepository
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
        repo = _make_repo()
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

        repo = _make_repo()
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

        repo = _make_repo()
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
