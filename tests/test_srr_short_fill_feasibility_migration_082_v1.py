"""Migration 082 SQL contract: DISABLED-by-default lifecycle, strict
prospective boundary, atomic-claim evidence table (Issue #259).

Text-level contract tests (no PostgreSQL): they pin the safety properties of
``sql/migrations/082_srr_short_fill_feasibility_v1.sql`` and keep the capture
module's claim/finalize column contracts in sync with the table definition.
"""
import re
from pathlib import Path

import app.research.srr_short_fill_feasibility_v1 as mod

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sql" / "migrations" / "082_srr_short_fill_feasibility_v1.sql"
MODULE = ROOT / "app" / "research" / "srr_short_fill_feasibility_v1.py"


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def code_body() -> str:
    """Migration text without ``--`` comments (headers mention paper/live)."""
    return "\n".join(line for line in migration_sql().splitlines()
                     if not line.strip().startswith("--"))


def table_body() -> str:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS research\.srr_short_fill_feasibility_v1 "
        r"\((.*?)\n\);",
        migration_sql(), re.S)
    assert match is not None, "evidence table definition missing"
    return match.group(1)


def test_srr_short_fill_migrations_are_exactly_082_and_083():
    files = sorted(p.name for p in
                   (ROOT / "sql" / "migrations").glob("*srr_short_fill*"))
    assert files == ["082_srr_short_fill_feasibility_v1.sql",
                     "083_srr_short_fill_coverage_v1.sql"]


def test_migration_number_is_next_and_not_taken():
    numbers = sorted(int(p.name.split("_")[0]) for p in
                     (ROOT / "sql" / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    # 082 (feasibility) + 083 (coverage) are the F3 hardening numbers; this
    # PR adds 084 (paper execution-policy recovery), 085 (inverse-short
    # contour) and 086 (inverse-short close outbox); 087 is the
    # ME_LONG_V2_INVERSE_SHORT_V2 contour migration (stale-entry guard,
    # owner decision Q2 variant B); 088 is the ME_LONG_V2_INVERSE_SHORT_OOS2_V1
    # ladder contour migration (Issue #276).  Every one of those numbers must
    # appear EXACTLY once — an old number is never reused — and the branch's
    # newest migration carries the highest number.
    for taken in (82, 83, 84, 85, 86, 87, 88):
        assert numbers.count(taken) == 1, (
            f"migration number {taken:03d} must be unique"
        )
    assert numbers[-1] == 88


def test_migration_is_transactional_and_idempotent():
    sql = migration_sql()
    assert sql.lstrip().startswith("--")  # header comment block
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE SCHEMA IF NOT EXISTS research;" in sql
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 2
    assert sql.count("IF NOT EXISTS (") >= 4  # trigger existence guards
    assert "ON CONFLICT DO NOTHING" in sql  # seed row is replay-safe


def test_migration_creates_only_the_two_new_research_tables():
    created = re.findall(r"CREATE TABLE IF NOT EXISTS ([\w.]+)", migration_sql())
    assert created == ["research.srr_short_fill_activation_v1",
                       "research.srr_short_fill_feasibility_v1"]
    body = code_body().lower()
    assert "alter table" not in body
    assert "drop " not in body
    assert "truncate" not in body
    # Existing research objects are only READ from / referenced, never edited.
    assert "alter table research.prospective" not in body
    assert "update research.prospective" not in body
    assert "delete from research.prospective" not in body


def test_activation_seed_is_disabled_without_boundary():
    sql = migration_sql()
    assert "INSERT INTO research.srr_short_fill_activation_v1" in sql
    assert "VALUES ('SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1', 'DISABLED')" in sql
    assert "CHECK ((status='DISABLED' AND activation_ts IS NULL)" in sql
    # The only INSERT seeds the inactive state; the migration can never
    # activate capture (no RUNNING insert, no activation timestamp).
    assert sql.count("INSERT INTO") == 1
    seed = sql.split("INSERT INTO")[1].split(";")[0]
    assert "'RUNNING'" not in seed
    assert "activation_ts" not in seed.split("VALUES")[1]


def test_activation_state_machine_is_guarded():
    sql = migration_sql()
    assert "fn_guard_srr_short_fill_activation" in sql
    assert "trg_srr_short_fill_activation_guard" in sql
    assert "fn_block_srr_short_fill_activation_delete" in sql
    assert "trg_srr_short_fill_activation_block_delete" in sql
    # activation_ts immutable once set; no reactivation after terminal state.
    assert "activation_ts is immutable once set" in sql
    assert "OLD.status = 'DISABLED' AND NEW.status IN ('RUNNING', 'CANCELLED')" in sql
    assert "OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED')" in sql
    assert "NEW.status = 'RUNNING' AND NEW.activation_ts IS NULL" in sql


def test_strict_prospective_boundary_enforced_in_db():
    sql = migration_sql()
    assert "CHECK (signal_time > activation_ts)" in sql
    assert "activation_ts timestamptz NOT NULL" in sql  # row pins boundary


def test_evidence_rows_are_append_only_with_single_finalize():
    sql = migration_sql()
    assert "fn_guard_srr_short_fill_event_mutation" in sql
    assert "trg_srr_short_fill_event_guard" in sql
    assert "BEFORE UPDATE OR DELETE ON research.srr_short_fill_feasibility_v1" in sql
    assert "TG_OP = 'DELETE'" in sql
    # Exactly one transition is possible: PENDING claim -> finalized row.
    assert "OLD.feasibility_status <> 'PENDING'" in sql
    # Identity columns are immutable across that transition.
    for column in ("NEW.event_key IS DISTINCT FROM OLD.event_key",
                   "NEW.observation_id IS DISTINCT FROM OLD.observation_id",
                   "NEW.signal_time IS DISTINCT FROM OLD.signal_time",
                   "NEW.activation_ts IS DISTINCT FROM OLD.activation_ts",
                   "NEW.reference_price IS DISTINCT FROM OLD.reference_price"):
        assert column in sql


def test_status_enums_cover_capture_statuses():
    sql = migration_sql()
    feasibility = re.search(
        r"feasibility_status text NOT NULL DEFAULT 'PENDING' CHECK\s*"
        r"\(feasibility_status IN\s*\(([^)]*)\)\)",
        sql, re.S)
    assert feasibility is not None
    feasibility_values = set(re.findall(r"'([A-Z_]+)'", feasibility.group(1)))
    assert feasibility_values == {"PENDING", "OK", "INSUFFICIENT_DEPTH",
                                  "STALE_BOOK", "FUTURE_BOOK", "INVALID_BOOK",
                                  "BELOW_EXCHANGE_MIN", "ERROR"}
    quality = re.search(
        r"quote_quality_status text NOT NULL DEFAULT 'PENDING' CHECK\s*"
        r"\s*\(quote_quality_status IN\s*\('([A-Z_]+)'",
        sql, re.S)
    assert quality is not None
    assert quality.group(1) == "PENDING"
    for value in ("FRESH", "DELAYED", "STALE_BOOK", "FUTURE_BOOK",
                  "INVALID_BOOK", "NO_QUOTE"):
        assert f"'{value}'" in sql
    cost = re.search(r"cost_class text CHECK \(cost_class IS NULL OR\s*"
                     r"cost_class IN \(([^)]*)\)", sql, re.S)
    assert cost is not None
    assert set(re.findall(r"'([A-Z_]+)'", cost.group(1))) == \
        {"NORMAL", "ELEVATED", "ABOVE_ELEVATED"}
    # Eligibility gate: only FRESH + OK rows may enter execution statistics.
    assert "eligible_for_execution_stats = false" in sql
    assert "feasibility_status='OK'" in sql
    assert "quote_quality_status='FRESH'" in sql


def test_event_identity_contract():
    sql = migration_sql()
    assert "event_key text PRIMARY KEY" in sql
    assert "sha256" in sql  # documented identity hash
    # Exactly one observation per research row, FK-bound to the source row.
    assert "observation_id bigint NOT NULL UNIQUE" in sql
    assert "REFERENCES research.prospective_observation(observation_id)" in sql
    # Frozen experiment identity columns.
    assert "experiment_id='SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'" in sql
    assert ("source_experiment_id text NOT NULL\n"
            "    CHECK (source_experiment_id="
            "'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1')") \
        in sql
    assert "scanner_name text NOT NULL CHECK (scanner_name='SUPPORT_RESISTANCE_REACTION')" in sql
    assert "direction text NOT NULL CHECK (direction='SHORT')" in sql
    # Frozen SRR geometry sanity in the schema itself.
    assert "CHECK (reference_price>0)" in sql
    assert "CHECK (invalidation_price>reference_price)" in sql
    assert "CHECK (structural_r>0)" in sql
    assert "CHECK (execution_r>0)" in sql
    assert "CHECK (variant_stop>reference_price)" in sql
    assert "CHECK (variant_target<reference_price)" in sql


def test_timings_and_quality_columns_exist():
    body = table_body()
    for column in ("signal_time", "captured_at", "capture_started_at",
                   "capture_finished_at", "book_time_ms", "book_age_ms",
                   "signal_to_capture_ms", "signal_to_quote_ms",
                   "api_request_ms",
                   "bid", "ask", "spread_bps", "reference_to_bid_bps",
                   "depth_bid_10bps_usdt", "requested_notional_usdt",
                   "requested_qty", "executable_qty", "min_order_qty",
                   "qty_step", "min_notional", "estimated_fill",
                   "entry_slippage_bps", "observed_roundtrip_cost_pct",
                   "observed_cost_r", "frozen_normal_cost_r",
                   "frozen_elevated_cost_r", "extra_cost_r_vs_frozen_normal",
                   "exchange_constraints", "scenario_results",
                   "source_diagnostics", "raw_snapshot",
                   "quote_quality_status", "feasibility_status",
                   "eligible_for_execution_stats", "failure_reason",
                   "activation_ts", "invalidation_price", "structural_r",
                   "execution_r", "variant_stop", "variant_target"):
        assert re.search(rf"\b{column}\b", body), f"missing column {column}"
    assert "CHECK (capture_finished_at >= capture_started_at)" in body


def test_full_quote_latency_is_enforced_by_db_checks():
    """P1 contract: eligibility requires the FULL signal->quote latency."""
    body = table_body()
    assert re.search(r"\bsignal_to_quote_ms\s+bigint\b", body)
    # full latency always contains the capture-start delay
    assert re.search(
        r"CHECK \(signal_to_quote_ms IS NULL\s+OR\s+"
        r"signal_to_quote_ms >= signal_to_capture_ms\)", body)
    # a row without its full latency can never be eligible
    assert re.search(
        r"CHECK \(eligible_for_execution_stats = false\s+OR\s+"
        r"signal_to_quote_ms IS NOT NULL\)", body)


def test_capture_column_contracts_match_table():
    module_sql = MODULE.read_text(encoding="utf-8")
    body = table_body()

    # Anchor BOTH parts on the claim INSERT itself: the module now also
    # contains a coverage-ledger INSERT (F3) whose VALUES must not be
    # mistaken for the evidence-table claim.
    claim = re.search(
        r"INSERT INTO research\.srr_short_fill_feasibility_v1\s*\((.*?)\)\s*"
        r"VALUES\s*(\(.*?\))",
        module_sql, re.S)
    assert claim is not None
    claim_columns = [c.strip() for c in claim.group(1).split(",")]
    for column in claim_columns:
        assert re.search(rf"\b{re.escape(column)}\b", body), \
            f"claim column missing from table: {column}"
    assert claim.group(2).count("%s") == len(claim_columns)
    assert "ON CONFLICT DO NOTHING" in module_sql  # atomic claim/reservation

    # Finalize contract: every column in _FINALIZE_COLUMNS exists and the
    # UPDATE is guarded by the PENDING-only transition.
    for column in mod._FINALIZE_COLUMNS:
        assert re.search(rf"\b{re.escape(column)}\b", body), \
            f"finalize column missing from table: {column}"
    assert "WHERE event_key=%s AND feasibility_status='PENDING'" in module_sql


def test_no_order_routing_or_trading_surface():
    body = code_body().lower()
    for forbidden in ("paper", "live", "order_id", "insert into public",
                      "update public", "dds.", "scanner_setup",
                      "prospective_outcome"):
        assert forbidden not in body, f"unexpected trading object: {forbidden}"


def test_module_constants_match_migration_identity():
    sql = migration_sql()
    assert mod.EXPERIMENT_ID == "SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1"
    assert mod.SOURCE_EXPERIMENT_ID == \
        "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
    assert mod.SCANNER_NAME == "SUPPORT_RESISTANCE_REACTION"
    assert mod.DIRECTION == "SHORT"
    for value in (mod.EXPERIMENT_ID, mod.SOURCE_EXPERIMENT_ID,
                  mod.SCANNER_NAME):
        assert value in sql
