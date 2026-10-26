"""Migration 077 SQL contract: lifecycle, idempotency, append-only evidence.

Text-level contract tests (no PostgreSQL): they pin the safety properties of
``sql/migrations/077_me_short_a_fill_feasibility_v1.sql`` and keep the capture
module's INSERT column contract in sync with the table definition.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sql" / "migrations" / "077_me_short_a_fill_feasibility_v1.sql"
MODULE = ROOT / "app" / "research" / "me_short_a_fill_feasibility_v1.py"


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_077_is_the_only_fill_feasibility_migration():
    files = sorted(p.name for p in (ROOT / "sql" / "migrations").glob("*me_short_a_fill*"))
    assert files == ["077_me_short_a_fill_feasibility_v1.sql"]


def test_migration_is_transactional_and_idempotent():
    sql = migration_sql()
    assert sql.lstrip().startswith("--")  # header comment block
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert sql.count("IF NOT EXISTS (") >= 3  # trigger existence guards
    assert "ON CONFLICT DO NOTHING" in sql  # seed row is replay-safe


def test_activation_seed_is_inactive_without_freeze():
    sql = migration_sql()
    assert "VALUES ('ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1','READY_TO_START')" in sql
    assert "CHECK ((status='READY_TO_START' AND freeze_ts IS NULL)" in sql
    # The only INSERT in the migration seeds the inactive state; the migration
    # itself can never start capture (no RUNNING insert, no freeze timestamp).
    assert sql.count("INSERT INTO") == 1
    seed = sql.split("INSERT INTO")[1].split(";")[0]
    assert "'RUNNING'" not in seed
    assert "freeze_ts" not in seed


def test_activation_state_machine_is_guarded():
    sql = migration_sql()
    assert "fn_guard_me_short_a_fill_activation" in sql
    assert "trg_me_short_a_fill_activation_guard" in sql
    assert "fn_block_me_short_a_fill_activation_delete" in sql
    assert "trg_me_short_a_fill_activation_block_delete" in sql
    # freeze_ts immutable once set.
    assert "freeze_ts is immutable once set" in sql
    # Strict transitions only; terminal states are final.
    assert "OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED')" in sql
    assert "OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED')" in sql
    assert "NEW.status = 'RUNNING' AND NEW.freeze_ts IS NULL" in sql


def test_evidence_rows_are_append_only():
    sql = migration_sql()
    assert "fn_block_me_short_a_fill_event_mutation" in sql
    assert "trg_me_short_a_fill_event_block_mutation" in sql
    assert "BEFORE UPDATE OR DELETE ON research.me_short_a_fill_feasibility_v1" in sql
    assert "append-only" in sql


def test_strict_post_freeze_boundary_enforced_in_db():
    sql = migration_sql()
    assert "CHECK (signal_time>freeze_ts)" in sql
    assert "freeze_ts timestamptz NOT NULL" in sql  # evidence row pins boundary


def test_feasibility_status_enum_covers_capture_statuses():
    sql = migration_sql()
    enum = re.search(
        r"feasibility_status text NOT NULL CHECK\(feasibility_status IN\s*\(([^)]*)\)",
        sql, re.S)
    assert enum is not None
    values = set(re.findall(r"'([A-Z_]+)'", enum.group(1)))
    assert values == {"OK", "INSUFFICIENT_DEPTH", "STALE_BOOK", "INVALID_BOOK",
                      "BELOW_EXCHANGE_MIN", "ERROR"}
    cost = re.search(r"cost_class text CHECK \(cost_class IS NULL OR\s*cost_class IN \(([^)]*)\)", sql, re.S)
    assert cost is not None
    cost_values = set(re.findall(r"'([A-Z_]+)'", cost.group(1)))
    assert cost_values == {"NORMAL", "ELEVATED", "ABOVE_ELEVATED"}


def test_event_key_is_idempotent_primary_key():
    sql = migration_sql()
    assert "event_key text PRIMARY KEY" in sql
    assert "sha256" in sql  # documented identity hash


def test_capture_insert_columns_match_table_definition():
    module_sql = MODULE.read_text(encoding="utf-8")
    insert = re.search(
        r"INSERT INTO research\.me_short_a_fill_feasibility_v1\s*\((.*?)\)\s*SELECT",
        module_sql, re.S)
    assert insert is not None
    columns = [c.strip() for c in insert.group(1).split(",")]
    table = re.search(
        r"CREATE TABLE IF NOT EXISTS research\.me_short_a_fill_feasibility_v1 \((.*?)\n\);",
        migration_sql(), re.S)
    assert table is not None
    body = table.group(1)
    for column in columns:
        assert re.search(rf"\b{re.escape(column)}\b", body), f"missing column {column}"
    # Placeholder count must match the column list exactly (the WHERE EXISTS
    # guard adds two more bind params: experiment_id and the frozen boundary).
    select = re.search(r"SELECT (%s.*?)\s*WHERE EXISTS", module_sql, re.S)
    assert select is not None
    placeholders = select.group(1).count("%s")
    assert placeholders == len(columns)


def test_no_order_routing_or_paper_live_surface():
    # Strip comments before scanning: the header mentions paper/live safety.
    body = "\n".join(
        line for line in migration_sql().lower().splitlines()
        if not line.strip().startswith("--"))
    for forbidden in ("paper", "live", "order_id", "insert into public", "update public"):
        assert forbidden not in body
