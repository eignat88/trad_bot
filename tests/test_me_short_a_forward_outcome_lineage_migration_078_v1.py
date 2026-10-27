"""Migration 078 SQL contract: freeze, append-only, lineage, finalization.

Text-level contract tests (no PostgreSQL): they pin the safety properties of
``sql/migrations/078_me_short_a_forward_only_outcome_lineage_v1.sql`` and keep
the collector module's INSERT column contracts in sync with the table
definitions. They also pin the isolation guarantees required by Issue #208:
no mutation of the frozen OOS cohort, no retro-fitting of Phase 1
``source_observation_ids``, and no order/paper/live surface.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "sql" / "migrations"
MIGRATION = MIGRATIONS / "078_me_short_a_forward_only_outcome_lineage_v1.sql"
GRANTS = (ROOT / "sql" / "grants" /
          "me_short_a_forward_outcome_lineage_least_privilege_v1.sql")
AUDIT_SQL = (ROOT / "sql" / "research" /
             "me_short_a_forward_outcome_lineage_audit_v1.sql")
MODULE = ROOT / "app" / "research" / "me_short_a_forward_outcome_lineage_v1.py"
REGISTRY = (ROOT / "app" / "research" /
            "me_short_a_forward_outcome_lineage_registry_v1.json")

CAPTURE_TABLES = (
    "me_short_a_source_signal_v1",
    "me_short_a_post_signal_candle_v1",
    "me_short_a_event_lineage_v1",
    "me_short_a_outcome_observation_v1",
)


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def module_sql() -> str:
    return MODULE.read_text(encoding="utf-8")


def _table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS research\.{re.escape(table)} \((.*?)\n\);",
        sql, re.S)
    assert match is not None, f"table {table} not found"
    return match.group(1)


# ── identity / isolation ───────────────────────────────────────────────


def test_migration_078_is_the_only_forward_lineage_migration():
    files = sorted(p.name for p in MIGRATIONS.glob("*forward_only_outcome_lineage*"))
    assert files == ["078_me_short_a_forward_only_outcome_lineage_v1.sql"]


def test_migration_number_follows_the_highest_existing_migration():
    numbers = sorted(
        int(p.name.split("_", 1)[0]) for p in MIGRATIONS.glob("[0-9]*.sql")
        if p.name.split("_", 1)[0].isdigit())
    # 078 is exactly one past the previous maximum and is used exactly once.
    assert numbers.count(78) == 1
    assert max(n for n in numbers if n < 78) == 77
    # Later migrations (079+) may exist; the sequence must not go backwards.
    assert numbers == sorted(numbers)
    assert numbers[-1] >= 78


def test_migration_is_transactional_and_idempotent():
    sql = migration_sql()
    assert sql.lstrip().startswith("--")
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert sql.count("IF NOT EXISTS (") >= 4      # trigger existence guards
    assert "ON CONFLICT (experiment_id) DO NOTHING" in sql
    assert "DO $$" in sql


def test_migration_depends_on_077_and_fails_loudly_without_it():
    sql = migration_sql()
    assert "to_regclass('research.me_short_a_fill_feasibility_v1')" in sql
    assert "migration 077" in sql
    assert "RAISE EXCEPTION" in sql


def test_the_only_insert_seeds_an_inactive_activation_row():
    sql = migration_sql()
    assert sql.count("INSERT INTO") == 1
    seed = sql.split("INSERT INTO")[1].split(";")[0]
    assert "'READY_TO_START'" in seed
    assert "'RUNNING'" not in seed
    assert "freeze_ts" not in seed
    assert "ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1" in seed


def test_migration_never_writes_to_frozen_or_phase1_tables():
    sql = migration_sql()
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--"))
    for forbidden in (
        "me_short_a_fill_feasibility_v1 (",      # not a CREATE/INSERT target
        "INSERT INTO research.me_short_a_fill",
        "UPDATE research.me_short_a_fill",
        "DELETE FROM research.me_short_a_fill",
        "INSERT INTO research.prospective_observation",
        "UPDATE research.prospective_observation",
        "INSERT INTO research.prospective_outcome",
        "UPDATE research.prospective_outcome",
        "DELETE FROM research.prospective_outcome",
    ):
        assert forbidden not in body, forbidden
    # the only reference to the Phase 1 table is a read: the view's LEFT JOIN
    assert sql.count("LEFT JOIN research.me_short_a_fill_feasibility_v1") == 1
    assert sql.count("FROM research.me_short_a_fill_feasibility_v1") == 0


def test_no_order_paper_live_or_direction_gate_surface():
    body = "\n".join(
        line for line in migration_sql().lower().splitlines()
        if not line.strip().startswith("--"))
    for forbidden in ("paper", "live", "order_id", "insert into public",
                      "update public", "direction_gate"):
        assert forbidden not in body


# ── lifecycle / freeze ─────────────────────────────────────────────────


def test_activation_state_machine_is_guarded():
    sql = migration_sql()
    assert "fn_guard_me_short_a_lineage_activation" in sql
    assert "trg_me_short_a_lineage_activation_guard" in sql
    assert "fn_block_me_short_a_lineage_activation_delete" in sql
    assert "trg_me_short_a_lineage_activation_block_delete" in sql
    assert "freeze_ts is immutable once set" in sql
    assert "protocol is immutable" in sql
    assert "OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED')" in sql
    assert "OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED')" in sql
    assert "NEW.status = 'RUNNING' AND NEW.freeze_ts IS NULL" in sql


def test_every_capture_table_enforces_the_strict_post_freeze_boundary():
    sql = migration_sql()
    for table in CAPTURE_TABLES:
        body = _table_body(sql, table)
        assert "freeze_ts timestamptz NOT NULL" in body, table
        assert "CHECK (signal_time > freeze_ts)" in body, table


def test_every_capture_table_is_append_only():
    sql = migration_sql()
    assert "fn_block_me_short_a_lineage_mutation" in sql
    assert "trg_me_short_a_lineage_block_mutation" in sql
    assert "BEFORE UPDATE OR DELETE ON research.%I" in sql
    assert "append-only" in sql
    assert "FOREACH tbl IN ARRAY ARRAY[" in sql
    for table in CAPTURE_TABLES:
        assert f"'{table}'" in sql, table


# ── source signal: no lookahead, explicit missing ──────────────────────


def test_source_signal_no_lookahead_invariants():
    body = _table_body(migration_sql(), "me_short_a_source_signal_v1")
    assert "CHECK (recent_high_window_end IS NULL OR recent_high_window_end <= signal_time)" in body
    assert "CHECK (signal_candle_close_time IS NULL OR signal_candle_close_time <= signal_time)" in body
    assert "CHECK (captured_at >= signal_time)" in body
    assert "missing_fields jsonb NOT NULL" in body
    assert "no_lookahead_flags jsonb NOT NULL" in body
    assert "trigger_5m_close numeric" in body
    assert "entry_zone_low numeric" in body
    assert "entry_zone_high numeric" in body
    assert "recent_high_window jsonb" in body


def test_source_signal_frozen_geometry_price_invariants():
    body = _table_body(migration_sql(), "me_short_a_source_signal_v1")
    assert "CHECK (reference_price > 0)" in body
    assert "CHECK (stop_price > reference_price)" in body
    assert "CHECK (target_price > 0 AND target_price < reference_price)" in body
    assert "CHECK (structural_r = stop_price - reference_price)" in body
    assert "CHECK (direction = 'SHORT')" in body
    assert "CHECK (scanner_name = 'MOMENTUM_EXHAUSTION')" in body


# ── post-signal candles: closed only, candle touch ≠ fill ─────────────


def test_post_signal_candles_are_closed_only_and_no_lookahead():
    body = _table_body(migration_sql(), "me_short_a_post_signal_candle_v1")
    assert "is_closed boolean NOT NULL" in body
    assert "CHECK (is_closed)" in body
    assert "CHECK (candle_open_time > signal_time)" in body
    assert "CHECK (candle_close_time > candle_open_time)" in body
    assert "CHECK (candle_close_time <= retrieved_at)" in body
    assert "PRIMARY KEY (event_key, candle_open_time)" in body


def test_touch_semantics_never_claim_an_orderbook_fill():
    body = _table_body(migration_sql(), "me_short_a_post_signal_candle_v1")
    assert "CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL" in body
    assert "CHECK (touch_semantics = 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL')" in body
    assert "high_touches_reference boolean" in body
    assert "ORDER_BOOK_FILL" not in body.replace("NOT_ORDERBOOK_FILL", "")


# ── lineage: deterministic key, quarantine ────────────────────────────


def test_lineage_key_and_quarantine_contract():
    body = _table_body(migration_sql(), "me_short_a_event_lineage_v1")
    assert "event_key text PRIMARY KEY" in body
    assert "event_key_recomputed text NOT NULL" in body
    assert "event_key_integrity boolean NOT NULL" in body
    assert "link_status text NOT NULL CHECK (link_status IN ('PENDING','LINKED','QUARANTINED'))" in body
    assert "CHECK ((event_key_integrity = TRUE) OR (link_status = 'QUARANTINED'))" in body
    assert "CHECK ((link_status <> 'QUARANTINED') OR (quarantine_reason IS NOT NULL))" in body
    assert "CHECK ((NOT phase1_linked) OR (phase1_event_key = event_key))" in body
    assert "protocol_version text NOT NULL" in body
    assert "protocol_hash text NOT NULL" in body


def test_lineage_documentation_names_the_deterministic_formula():
    sql = migration_sql()
    assert "sha256(symbol|SHORT|<utc-iso signal_time>|<ref>|<stop>|<target>)" in sql
    assert "BYTE-IDENTICAL" in sql


# ── outcome: controlled finalization, source coverage ─────────────────


def test_outcome_finalization_requires_closed_complete_clean_source():
    body = _table_body(migration_sql(), "me_short_a_outcome_observation_v1")
    assert "is_final boolean NOT NULL DEFAULT FALSE" in body
    assert "finalization_candle_close_time timestamptz" in body
    assert "asof_time timestamptz NOT NULL" in body
    assert "coverage_complete boolean NOT NULL" in body
    assert "asof_ok boolean NOT NULL" in body
    assert "CHECK (coverage_complete = (candles_present = candles_expected))" in body
    assert "CHECK (asof_ok = (max_candle_close_time IS NOT NULL" in body
    assert "finalization_candle_close_time >= horizon_end_time" in body
    assert "finalization_candle_close_time <= asof_time" in body
    assert "CHECK (horizon_end_time = signal_time + INTERVAL '240 minutes')" in body
    assert "CHECK (horizon_minutes = 240)" in body
    assert "CHECK (intrabar_policy = 'STOP_FIRST')" in body


def test_outcome_never_finalizes_prematurely_or_without_source():
    body = _table_body(migration_sql(), "me_short_a_outcome_observation_v1")
    assert "CHECK ((is_final = FALSE) OR (" in body
    assert "CHECK ((is_final = TRUE) OR (outcome_status <> 'MATCHED_FINALIZED'))" in body
    assert "CHECK ((outcome_status <> 'MATCHED_FINALIZED') OR (" in body
    assert "CHECK ((outcome_status <> 'OUTCOME_UNMATURED')" in body
    assert "CHECK ((outcome_status <> 'SOURCE_INCOMPLETE') OR (" in body
    assert "net_r_elevated IS NULL" in body
    assert "return_at_240m IS NULL" in body


def test_outcome_stop_first_semantics_are_enforced():
    body = _table_body(migration_sql(), "me_short_a_outcome_observation_v1")
    assert "CHECK (tp_before_sl = FALSE OR sl_before_tp = FALSE)" in body
    assert "CHECK (ambiguous_intrabar = FALSE" in body
    assert "(sl_before_tp = TRUE AND tp_before_sl = FALSE)" in body
    assert "path_class <> 'STOP_HIT' OR sl_before_tp" in body
    assert "path_class <> 'TARGET_HIT' OR tp_before_sl" in body
    assert "gross_r = -1" in body


def test_outcome_statuses_are_the_documented_four():
    body = _table_body(migration_sql(), "me_short_a_outcome_observation_v1")
    enum = re.search(
        r"outcome_status text NOT NULL\s*CHECK \(outcome_status IN \(([^)]*)\)",
        body, re.S)
    assert enum is not None
    values = set(re.findall(r"'([A-Z_]+)'", enum.group(1)))
    assert values == {"OUTCOME_UNMATURED", "SOURCE_INCOMPLETE", "MATCHED_FINALIZED"}
    path = re.search(
        r"path_class text\s*CHECK \(path_class IS NULL\s*OR path_class IN \(([^)]*)\)",
        body, re.S)
    assert path is not None
    assert set(re.findall(r"'([A-Z_0-9]+)'", path.group(1))) == {
        "STOP_HIT", "TARGET_HIT", "TIMEOUT_240M", "UNRESOLVED"}


def test_exit_and_partial_fill_are_structurally_unknown():
    body = _table_body(migration_sql(), "me_short_a_outcome_observation_v1")
    assert "CHECK (exit_fill_semantics = 'MODELED_UNKNOWN')" in body
    assert "CHECK (partial_fill_status = 'UNKNOWN_NOT_INDEPENDENTLY_CAPTURED')" in body
    assert "CHECK (touch_basis = 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL')" in body


# ── lineage status view ───────────────────────────────────────────────


def test_status_view_exposes_exactly_the_four_documented_statuses():
    sql = migration_sql()
    view = sql.split("CREATE OR REPLACE VIEW research.v_me_short_a_lineage_status_v1 AS", 1)[1]
    for status in ("MISSING_LINEAGE", "OUTCOME_UNMATURED",
                   "SOURCE_INCOMPLETE", "MATCHED_FINALIZED"):
        assert f"'{status}'" in view, status
    # exactly four status literals are produced by the CASE expression
    case = view.split("CASE", 1)[1].split("END", 1)[0]
    assert set(re.findall(r"(?:THEN|ELSE) '([A-Z_]+)'", case)) == {
        "MISSING_LINEAGE", "OUTCOME_UNMATURED", "SOURCE_INCOMPLETE",
        "MATCHED_FINALIZED"}


def test_status_view_requires_all_three_lineage_layers():
    view = migration_sql().split(
        "CREATE OR REPLACE VIEW research.v_me_short_a_lineage_status_v1 AS", 1)[1]
    for table in ("me_short_a_source_signal_v1", "me_short_a_event_lineage_v1",
                  "me_short_a_outcome_observation_v1",
                  "me_short_a_fill_feasibility_v1"):
        assert table in view, table
    assert "l.phase1_linked = FALSE" in view
    assert "p.event_key IS NULL" in view


# ── collector INSERT contract vs table definition ─────────────────────


def test_collector_insert_columns_match_table_definitions():
    sql = migration_sql()
    # the collector builds each statement from adjacent string literals, so
    # flatten the quotes before matching the column contract
    flat = module_sql().replace('"', ' ')
    for table in CAPTURE_TABLES:
        insert = re.search(
            rf"INSERT INTO research\.{re.escape(table)}\s*\((.*?)\)\s*SELECT",
            flat, re.S)
        assert insert is not None, f"no INSERT for {table}"
        columns = [c.strip() for c in insert.group(1).split(",")]
        assert all(columns), f"{table}: empty column name"
        body = _table_body(sql, table)
        for column in columns:
            assert re.search(rf"\b{re.escape(column)}\b", body), \
                f"{table}: missing column {column}"
        select = re.search(
            rf"INSERT INTO research\.{re.escape(table)}\s*\(.*?\)\s*SELECT\s*(.*?)\s*"
            rf"WHERE EXISTS", flat, re.S)
        assert select is not None
        placeholders = select.group(1).count("%s")
        assert placeholders == len(columns), f"{table}: placeholder mismatch"


def test_every_collector_insert_carries_the_activation_guard():
    source = module_sql()
    inserts = re.findall(r'"INSERT INTO research\.[a-z_0-9]+.*?ON CONFLICT[^"]*"',
                         source, re.S)
    assert len(inserts) == 4
    for statement in inserts:
        assert "WHERE EXISTS (SELECT 1 FROM research.me_short_a_lineage_activation_v1" in statement
        assert "status='RUNNING'" in statement
        assert "freeze_ts=%s" in statement
        assert "ON CONFLICT(" in statement and "DO NOTHING" in statement


def test_collector_never_issues_update_delete_or_truncate():
    source = module_sql()
    body = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#") and not line.strip().startswith('"""'))
    for forbidden in ("UPDATE research.", "DELETE FROM research.",
                      "TRUNCATE ", "ALTER TABLE ", "DROP TABLE "):
        assert forbidden not in body, forbidden


def test_registry_protocol_hash_matches_the_module_and_the_seed():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    # the frozen seed row carries exactly the registry hash
    assert f"'{registry['protocol_hash']}'" in migration_sql()
    # and the module derives it from the canonical protocol payload
    import importlib
    module = importlib.import_module("app.research.me_short_a_forward_outcome_lineage_v1")
    assert registry["protocol_hash"] == module.PROTOCOL_HASH
    assert registry["protocol_version"] == \
        "ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1"
    for forbidden in ("orders", "paper routing", "live routing",
                      "historical backfill",
                      "retro-fitting source_observation_ids on pre-freeze rows"):
        assert forbidden in registry["forbidden"]


# ── least-privilege grants ────────────────────────────────────────────


def test_grants_are_collector_insert_only_and_audit_select_only():
    sql = GRANTS.read_text(encoding="utf-8")
    assert "trad_bot_me_short_a_collector" in sql
    assert "trad_bot_me_short_a_audit" in sql
    for table in CAPTURE_TABLES:
        assert f"GRANT INSERT\n    ON research.{table}" in sql, table
    assert "GRANT SELECT\n    ON research.me_short_a_lineage_activation_v1" in sql
    assert "GRANT SELECT\n    ON research.v_me_short_a_lineage_status_v1" in sql
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in sql
    # the collector is never given SELECT on the capture tables
    assert "GRANT SELECT, INSERT" not in sql


def test_grants_revoke_inherited_research_schema_default_privileges():
    sql = GRANTS.read_text(encoding="utf-8")
    assert "REVOKE ALL ON research.%I FROM %I" in sql
    assert "'analytics_agent'" in sql
    assert "'analytics_runner'" in sql
    assert "'trad_bot'" in sql


def test_grants_never_touch_phase1_or_frozen_oos_objects():
    sql = GRANTS.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--"))
    for forbidden in ("me_short_a_fill_feasibility_v1",
                      "me_short_a_fill_activation_v1",
                      "prospective_observation", "prospective_outcome"):
        assert f"ON research.{forbidden}" not in body, forbidden


# ── read-only audit replay ────────────────────────────────────────────


def _audit_statements():
    text = AUDIT_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--"))
    statements = [s.strip() for s in body.split(";") if s.strip()]
    return text, statements


def test_audit_sql_is_strictly_read_only():
    text, statements = _audit_statements()
    assert statements, "audit script produced no statements"
    for statement in statements:
        head = statement.lstrip().upper()
        assert head.startswith(("SELECT", "WITH")), head[:60]
    body = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--"))
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP",
                 "CREATE", "GRANT", "REVOKE", "CALL", "DO"):
        assert not re.search(rf"\b{verb}\b", body), verb


def test_audit_sql_replays_the_lineage_and_the_safety_invariants():
    text, _ = _audit_statements()
    for needle in (
        "research.v_me_short_a_lineage_status_v1",
        "research.me_short_a_event_lineage_v1",
        "research.me_short_a_source_signal_v1",
        "research.me_short_a_post_signal_candle_v1",
        "research.me_short_a_outcome_observation_v1",
        "research.me_short_a_fill_feasibility_v1",
        "event_key_recomputed",
        "MISSING_LINEAGE",
        "MATCHED_FINALIZED",
        "MODELED_UNKNOWN",
    ):
        assert needle in text, needle


def test_audit_sql_checks_no_lookahead_and_no_economics_on_incomplete():
    text, _ = _audit_statements()
    assert "source_window_lookahead" in text
    assert "candle_lookahead" in text
    assert "outcome_premature_final" in text
    assert "incomplete_rows_with_economics" in text
    assert "stop_first_violations" in text
    assert "phase1_rows_without_source_ids" in text
    assert "gate_minimum_n_met" in text


def test_docs_exist_and_document_freeze_cutover_and_holdout():
    docs = (ROOT / "docs" / "research" /
            "ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1.md")
    assert docs.exists()
    text = docs.read_text(encoding="utf-8")
    for needle in ("ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1",
                   "STOP_FIRST", "240m", "event_key", "append-only",
                   "no-lookahead", "MISSING_LINEAGE", "OUTCOME_UNMATURED",
                   "SOURCE_INCOMPLETE", "MATCHED_FINALIZED", "holdout",
                   "no backfill", "Issue #208"):
        assert needle in text, needle
