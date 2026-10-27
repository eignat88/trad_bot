"""Migration 080 SQL contract: forward-only intake, lifecycle, append-only.

Text-level contract tests (no PostgreSQL): they pin the safety properties of
``sql/migrations/080_me_short_a_source_intake_v1.sql`` and
``sql/grants/me_short_a_source_intake_least_privilege_v1.sql``, and keep the
writer module's INSERT column contract in sync with the table definition.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sql" / "migrations" / "080_me_short_a_source_intake_v1.sql"
GRANTS = ROOT / "sql" / "grants" / "me_short_a_source_intake_least_privilege_v1.sql"
MODULE = ROOT / "app" / "research" / "me_short_a_source_intake_v1.py"
RUNTIME_SOURCES = ROOT / "app" / "research" / \
    "me_short_a_forward_outcome_runtime_sources_v1.py"


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def grants_sql() -> str:
    return GRANTS.read_text(encoding="utf-8")


def module_sql() -> str:
    return MODULE.read_text(encoding="utf-8")


# ── file-level contracts ──────────────────────────────────────


def test_migration_080_is_the_only_source_intake_migration():
    files = sorted(p.name for p in (ROOT / "sql" / "migrations")
                   .glob("*me_short_a_source_intake*"))
    assert files == ["080_me_short_a_source_intake_v1.sql"]


def test_migration_is_transactional_and_idempotent():
    sql = migration_sql()
    assert sql.lstrip().startswith("--")  # header comment block
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert sql.count("IF NOT EXISTS (") >= 3  # trigger existence guards
    assert "ON CONFLICT DO NOTHING" in sql  # seed row is replay-safe


# ── activation lifecycle ──────────────────────────────────────


def test_activation_seed_is_inactive_without_freeze():
    sql = migration_sql()
    assert ("VALUES ('ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1', 'READY_TO_START')"
            in sql)
    assert "CHECK ((status='READY_TO_START' AND intake_freeze_ts IS NULL)" in sql
    # The only INSERT seeds the inactive state: the migration itself can never
    # start capture (no RUNNING insert, no freeze timestamp).
    assert sql.count("INSERT INTO") == 1
    seed = sql.split("INSERT INTO")[1].split(";")[0]
    assert "'RUNNING'" not in seed
    assert "intake_freeze_ts" not in seed


def test_activation_state_machine_is_guarded():
    sql = migration_sql()
    assert "fn_guard_me_short_a_source_intake_activation" in sql
    assert "trg_me_short_a_source_intake_activation_guard" in sql
    assert "fn_block_me_short_a_source_intake_activation_delete" in sql
    assert "trg_me_short_a_source_intake_activation_block_delete" in sql
    # intake_freeze_ts immutable once set.
    assert "intake_freeze_ts is immutable once set" in sql
    # Strict transitions only; terminal states are final.
    assert "OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED')" in sql
    assert "OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED')" in sql
    assert "NEW.status = 'RUNNING' AND NEW.intake_freeze_ts IS NULL" in sql


def test_experiment_id_is_immutable():
    sql = migration_sql()
    assert "experiment_id is immutable" in sql
    assert ("CHECK (experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1')"
            in sql)


# ── forward-only boundary ─────────────────────────────────────


def test_strict_forward_only_boundary_enforced_in_db():
    sql = migration_sql()
    # The no-backfill guarantee: only strictly post-activation events.
    assert "CHECK (signal_time > intake_freeze_ts)" in sql
    assert "intake_freeze_ts timestamptz NOT NULL" in sql  # evidence row pins boundary


def test_no_lookahead_checks_are_present():
    sql = migration_sql()
    assert "CHECK (captured_at >= signal_time)" in sql
    assert "CHECK (signal_candle_open_time IS NULL OR signal_candle_open_time <= signal_time)" in sql
    assert "CHECK (recent_high_window_end IS NULL OR recent_high_window_end <= signal_time)" in sql


def test_frozen_a_geometry_is_checked():
    sql = migration_sql()
    assert "CHECK (stop_price > reference_price)" in sql
    assert "CHECK (target_price > 0 AND target_price < reference_price)" in sql
    assert "CHECK (structural_r = stop_price - reference_price)" in sql
    assert "CHECK (scanner_name = 'MOMENTUM_EXHAUSTION')" in sql
    assert "CHECK (direction = 'SHORT')" in sql


def test_capture_route_enum_covers_every_trading_decision():
    sql = migration_sql()
    enum = re.search(r"capture_route text NOT NULL CHECK \(capture_route IN \((.*?)\)\)", sql, re.S)
    assert enum is not None
    values = set(re.findall(r"'([A-Z_]+)'", enum.group(1)))
    assert values == {
        "TRADEABLE_FLOW",
        "SHADOW_CONTROL",
        "OBSERVE_ONLY",
        "DIRECTION_GATE_BLOCKED",
        "EXPECTANCY_FILTER_REJECTED",
        "REGIME_FILTER_REJECTED",
    }


# ── append-only evidence ──────────────────────────────────────


def test_evidence_rows_are_append_only():
    sql = migration_sql()
    assert "fn_block_me_short_a_source_intake_mutation" in sql
    assert "trg_me_short_a_source_intake_block_mutation" in sql
    assert "BEFORE UPDATE OR DELETE ON research.me_short_a_source_intake_v1" in sql
    assert "append-only" in sql


def test_event_key_is_idempotent_primary_key():
    sql = migration_sql()
    assert "event_key text PRIMARY KEY" in sql
    assert "sha256" in sql  # documented identity hash


def test_keyset_indexes_support_the_runtime_pagination():
    sql = migration_sql()
    assert "ON research.me_short_a_source_intake_v1 (signal_time, event_key)" in sql
    assert "ON research.me_short_a_source_intake_v1 (capture_route, signal_time)" in sql


# ── writer <-> table column contract ──────────────────────────


def test_capture_insert_columns_match_table_definition():
    src = module_sql()
    insert = re.search(
        r"INSERT INTO research\.me_short_a_source_intake_v1\s*\((.*?)\)\s*SELECT",
        src, re.S)
    assert insert is not None
    columns = [c.strip() for c in insert.group(1).split(",")]
    table = re.search(
        r"CREATE TABLE IF NOT EXISTS research\.me_short_a_source_intake_v1 \((.*?)\n\);",
        migration_sql(), re.S)
    assert table is not None
    body = table.group(1)
    for column in columns:
        assert re.search(rf"\b{re.escape(column)}\b", body), f"missing column {column}"
    # captured_at is NOW() in SQL: bind params are columns minus one, plus the
    # one WHERE EXISTS guard parameter. Count only inside the INSERT_SQL
    # constant (log format strings also use %s).
    insert_sql = re.search(r'INSERT_SQL = """(.*?)"""', src, re.S)
    assert insert_sql is not None
    assert insert_sql.group(1).count("%s") == len(columns) - 1 + 1


# ── least-privilege grants ────────────────────────────────────


def test_grants_are_least_privilege():
    # Strip comments before scanning: the header names forbidden grants.
    body = "\n".join(
        line for line in grants_sql().splitlines()
        if not line.lstrip().startswith("--"))
    upper = body.upper()
    assert "REVOKE ALL" in upper
    # The scanner-side writer role gets exactly SELECT + INSERT on the intake
    # and SELECT on the activation: no UPDATE/DELETE/TRUNCATE anywhere.
    for forbidden in ("GRANT UPDATE", "GRANT DELETE", "GRANT TRUNCATE"):
        assert forbidden not in upper
    assert "GRANT SELECT" in upper
    assert "GRANT SELECT, INSERT" in upper
    assert "research.me_short_a_source_intake_v1" in body
    assert "research.me_short_a_source_intake_activation_v1" in body


# ── what this migration must never do ─────────────────────────


def test_no_writes_to_phase1_lineage_outcome_or_frozen_oos():
    sql = migration_sql()
    upper = sql.upper()
    for table in ("ME_SHORT_A_FILL_FEASIBILITY_V1",
                  "ME_SHORT_A_SOURCE_SIGNAL_V1",
                  "ME_SHORT_A_EVENT_LINEAGE_V1",
                  "ME_SHORT_A_OUTCOME_OBSERVATION_V1",
                  "PROSPECTIVE_OBSERVATION",
                  "SCANNER_SETUP"):
        # CREATE/INSERT/UPDATE/DELETE on those tables never appears.
        assert not re.search(
            rf"(INSERT INTO|UPDATE |DELETE FROM|CREATE TABLE)[^;]*\bRESEARCH\.{table}\b",
            upper), table


def test_no_order_routing_or_paper_live_surface():
    # Strip comments before scanning: the header mentions paper/live safety.
    body = "\n".join(
        line for line in migration_sql().lower().splitlines()
        if not line.lstrip().startswith("--"))
    for forbidden in ("place_order", "create_order", "submit_order",
                      "paper_runner", "live_runner", "position_side"):
        assert forbidden not in body


def test_runtime_reader_references_only_the_two_intake_tables():
    src = RUNTIME_SOURCES.read_text(encoding="utf-8")
    block = src.split("class DbSourceIntakeCandidateSource", 1)[1] \
        .split("class CompositeCandidateSource", 1)[0]
    read_tables = re.search(r"read_tables = \((.*?)\)", block, re.S)
    assert read_tables is not None
    values = set(re.findall(r'"([a-z0-9_.]+)"', read_tables.group(1)))
    assert values == {
        "research.me_short_a_source_intake_v1",
        "research.me_short_a_source_intake_activation_v1",
    }
