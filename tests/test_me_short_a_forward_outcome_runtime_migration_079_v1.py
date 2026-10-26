"""SQL / deployment contract tests for
``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1``.

Guards migration 079, the runtime least-privilege grants, the systemd unit,
the runner exit-code contract and the deployment/rollback runbook.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.research import me_short_a_forward_outcome_runtime_v1 as mod
from app.research import me_short_a_forward_outcome_runtime_runner_v1 as runner

ROOT = Path(__file__).resolve().parent.parent

MIGRATIONS = ROOT / "sql" / "migrations"
MIGRATION = MIGRATIONS / "079_me_short_a_forward_outcome_runtime_integration_v1.sql"
GRANTS = (ROOT / "sql" / "grants" /
          "me_short_a_forward_outcome_runtime_least_privilege_v1.sql")
UNIT = ROOT / "deploy" / "systemd" / "trad-bot-me-short-a-forward-outcome.service"
DOCS = (ROOT / "docs" / "research" /
        "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1.md")
RUNBOOK = (ROOT / "docs" / "research" /
           "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_DEPLOYMENT_RUNBOOK_V1.md")
WORKFLOW = ROOT / ".github" / "workflows" / "me-short-a-execution-ci.yml"

HEARTBEAT_TABLE = "research.me_short_a_forward_outcome_runtime_heartbeat_v1"


# ── migration 079 ──────────────────────────────────────────────────────


def test_migration_exists_with_the_next_free_number():
    assert MIGRATION.exists()
    numbered = sorted(p.name for p in MIGRATIONS.glob("[0-9]*.sql"))
    assert numbered.count(MIGRATION.name) == 1
    numbers = sorted(
        int(p.name.split("_", 1)[0]) for p in MIGRATIONS.glob("[0-9]*.sql")
        if p.name.split("_", 1)[0].isdigit())
    assert numbers.count(79) == 1
    assert max(n for n in numbers if n < 79) == 78
    assert numbers == sorted(numbers)


def test_migration_requires_migration_078_first():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "to_regclass('research.me_short_a_lineage_activation_v1')" in sql
    assert "to_regclass('research.me_short_a_event_lineage_v1')" in sql
    assert "to_regclass('research.me_short_a_outcome_observation_v1')" in sql
    assert "RAISE EXCEPTION" in sql


def test_migration_is_transactional_and_idempotent():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.lstrip().startswith("--")
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE OR REPLACE VIEW" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql


def test_migration_seeds_no_data_at_all():
    body = "\n".join(
        line for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--"))
    assert not re.search(r"\bINSERT\s+INTO\b", body, re.I), \
        "migration 079 must never seed rows"
    assert not re.search(
        r"\bUPDATE\s+(?:ONLY\s+)?[a-z_][a-z0-9_.]*\s+SET\b", body, re.I)
    assert not re.search(r"\bDELETE\s+FROM\b", body, re.I)
    assert not re.search(r"\bTRUNCATE\b", body, re.I)


def test_heartbeat_table_is_append_only():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OR DELETE" in sql
    assert ("trg_me_short_a_runtime_heartbeat_block_mutation" in sql)
    assert ("fn_block_me_short_a_runtime_heartbeat_mutation" in sql)
    assert "USING ERRCODE = 'restrict_violation'" in sql


def test_heartbeat_table_enforces_the_runtime_identity():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "experiment = 'ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1'" in sql
    assert ("runtime_protocol_version = "
            "'ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1'") in sql
    assert "runtime_protocol_hash ~ '^[0-9a-f]{64}$'" in sql
    assert "config_fingerprint ~ '^[0-9a-f]{16}$'" in sql


def test_heartbeat_table_enforces_the_runner_lifecycle_and_counters():
    sql = MIGRATION.read_text(encoding="utf-8")
    for state in mod.RUNNER_STATES:
        assert f"'{state}'" in sql, state
    assert "activation_status IN ('READY_TO_START','RUNNING','COMPLETED','CANCELLED')" in sql
    for counter in mod.COUNTER_NAMES:
        assert counter in sql, counter
        assert f"{counter} >= 0" in sql, counter
    assert "CHECK (observed_at >= started_at)" in sql
    assert ("CHECK (last_event_time IS NULL OR last_event_time <= observed_at)"
            in sql)
    assert "outcomes_matched <= outcomes_finalized" in sql


def test_heartbeat_table_holds_no_research_evidence():
    columns = _create_table_columns(MIGRATION.read_text(encoding="utf-8"))
    for evidence in ("reference_price", "stop_price", "target_price",
                     "exit_price", "gross_r", "net_r_normal", "net_r_elevated",
                     "path_class", "return_at_240m", "mae_240m", "mfe_240m",
                     "event_key", "symbol", "signal_time", "open_price",
                     "high_price", "low_price", "close_price"):
        assert evidence not in columns, evidence


def test_runtime_heartbeat_insert_columns_match_the_table_definition():
    sql = MIGRATION.read_text(encoding="utf-8")
    columns = _create_table_columns(sql)
    module = (ROOT / "app" / "research" /
              "me_short_a_forward_outcome_runtime_v1.py").read_text(encoding="utf-8")
    match = re.search(
        r"INSERT INTO research\.me_short_a_forward_outcome_runtime_heartbeat_v1"
        r"[\s\"]*\(([\s\S]*?)\)[\s\"]*SELECT\s+([\s\S]*?)ON CONFLICT",
        module)
    assert match, "runtime heartbeat INSERT not found"
    inserted = re.sub(r"[\s\"]", "", match.group(1)).split(",")
    expected = [c for c in columns if c not in ("heartbeat_id", "created_at")]
    assert inserted == expected
    binds = re.sub(r"[\s\"]", "", match.group(2))
    assert binds.count("%s") == len(inserted)


def test_status_view_is_derived_and_reports_staleness():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert ("CREATE OR REPLACE VIEW "
            "research.v_me_short_a_forward_outcome_runtime_status_v1") in sql
    assert "DISTINCT ON (h.runner_id)" in sql
    assert "heartbeat_age_seconds" in sql
    assert "is_stale" in sql
    assert "collector_activated" in sql
    assert "research.me_short_a_lineage_activation_v1" in sql
    for state in mod.RUNNER_STATES:
        assert f"'{state}'" in sql, state


def test_migration_revokes_the_broad_default_privileges():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "REVOKE ALL ON research.me_short_a_forward_outcome_runtime_heartbeat_v1" in sql
    for role in ("trad_bot", "analytics_agent", "analytics_runner"):
        assert f"'{role}'" in sql, role


def test_migration_never_writes_phase1_or_the_frozen_oos():
    body = "\n".join(
        line for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--"))
    for forbidden in ("research.me_short_a_fill_feasibility_v1",
                      "research.prospective_observation",
                      "research.prospective_outcome",
                      "dds.scanner_setup"):
        assert forbidden not in body, forbidden


# ── least-privilege grants ─────────────────────────────────────────────


def test_grants_define_separate_runtime_roles():
    sql = GRANTS.read_text(encoding="utf-8")
    assert "CREATE ROLE trad_bot_me_short_a_runtime" in sql
    assert "CREATE ROLE trad_bot_me_short_a_runtime_audit" in sql
    assert sql.count("GRANT") >= 8


def test_grants_are_insert_only_on_the_append_only_evidence():
    sql = GRANTS.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")))
    for table in ("research.me_short_a_source_signal_v1",
                  "research.me_short_a_post_signal_candle_v1",
                  "research.me_short_a_event_lineage_v1",
                  "research.me_short_a_outcome_observation_v1"):
        assert f"GRANT SELECT, INSERT ON {table}" in flat, table
    assert (f"GRANT INSERT ON {HEARTBEAT_TABLE} TO trad_bot_me_short_a_runtime"
            in flat)


def test_grants_never_allow_mutation_of_anything():
    body = "\n".join(
        line for line in GRANTS.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--"))
    assert "GRANT UPDATE" not in body
    assert "GRANT DELETE" not in body
    assert "GRANT TRUNCATE" not in body
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in body


def test_grants_keep_phase1_read_only_and_the_frozen_oos_untouchable():
    body = "\n".join(
        line for line in GRANTS.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--"))
    # Phase 1: SELECT ONLY (shared-event_key linkage lookup).
    phase1_lines = [line for line in body.splitlines()
                    if "me_short_a_fill_feasibility_v1" in line]
    assert phase1_lines
    for line in phase1_lines:
        assert "GRANT SELECT" in line, line
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert verb not in line.replace("SELECT", ""), line
    # The closed frozen OOS cohort: no privilege at all.
    for forbidden in ("research.prospective_observation",
                      "research.prospective_outcome"):
        assert forbidden not in body, forbidden
    # The activation row is read-only for the runtime.
    for line in body.splitlines():
        if "me_short_a_lineage_activation_v1" in line:
            assert "GRANT SELECT" in line, line


def test_grants_revoke_broad_roles_on_the_new_table():
    sql = GRANTS.read_text(encoding="utf-8")
    assert "REVOKE ALL ON research.me_short_a_forward_outcome_runtime_heartbeat_v1" in sql
    for role in ("trad_bot", "analytics_agent", "analytics_runner"):
        assert f"'{role}'" in sql, role


def test_grants_file_is_a_separate_review_only_document():
    sql = GRANTS.read_text(encoding="utf-8")
    assert "REVIEW ONLY" in sql
    assert "AFTER migration 079" in sql
    assert "trad_bot_me_short_a_runtime" in sql


# ── systemd unit ───────────────────────────────────────────────────────


def test_systemd_unit_is_disabled_by_default():
    unit = UNIT.read_text(encoding="utf-8")
    assert "ConditionPathExists=/opt/trad_bot/etc/me_short_a_forward_runtime_enabled" in unit
    assert "User=tradbot" in unit
    assert "RestartPreventExitStatus=3" in unit


def test_systemd_unit_uses_dedicated_least_privilege_credentials():
    unit = UNIT.read_text(encoding="utf-8")
    assert ("EnvironmentFile=/opt/trad_bot/etc/me_short_a_forward_runtime.env"
            in unit)
    assert "trad_bot_me_short_a_runtime" in unit
    assert ".env is" in unit or "shared application .env" in unit


def test_systemd_unit_is_observe_only_and_trades_nothing():
    unit = UNIT.read_text(encoding="utf-8").lower()
    for forbidden in ("paper", "live", "order", "deploy"):
        assert forbidden in unit, forbidden   # documented as forbidden
    assert "never places an order" in unit
    assert "never routes to paper/live" in unit
    for entry in ("paper_runner", "bot.py", "scanner_runner"):
        assert entry not in unit, entry


def test_systemd_unit_runs_the_real_runtime_entrypoint():
    unit = UNIT.read_text(encoding="utf-8")
    assert ("-m app.research.me_short_a_forward_outcome_runtime_runner_v1"
            in unit)
    assert "--candle-source bybit" in unit
    assert "--poll-seconds" in unit
    assert "--once" not in unit            # the runner loops


def test_systemd_unit_is_not_auto_enabled():
    install = ROOT / "deploy" / "systemd"
    installers = [p for p in install.iterdir()
                  if p.suffix in (".sh", ".py") and "install" in p.name.lower()]
    for path in installers:
        text = path.read_text(encoding="utf-8").lower()
        assert "trad-bot-me-short-a-forward-outcome" not in text, path


# ── runner contract ────────────────────────────────────────────────────


def test_runner_exit_codes_are_documented_and_stable():
    assert runner.EXIT_OK == 0
    assert runner.EXIT_NOT_RUNNABLE == 3
    assert runner.EXIT_STATE_ERROR == 4
    assert runner.EXIT_WRITER_ERROR == 5


def test_runner_refuses_to_start_without_the_feature_flag(monkeypatch):
    monkeypatch.delenv("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", raising=False)
    assert runner.main(["--registry", str(_registry_path())]) == \
        runner.EXIT_NOT_RUNNABLE


def test_runner_reports_state_error_on_a_broken_registry(tmp_path):
    broken = tmp_path / "registry.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch_env = {"ME_SHORT_A_LINEAGE_CAPTURE_ENABLED": "1"}
    import os

    previous = os.environ.get("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED")
    os.environ.update(monkeypatch_env)
    try:
        assert runner.main(["--registry", str(broken)]) in (
            runner.EXIT_STATE_ERROR, runner.EXIT_NOT_RUNNABLE)
    finally:
        if previous is None:
            os.environ.pop("ME_SHORT_A_LINEAGE_CAPTURE_ENABLED", None)
        else:
            os.environ["ME_SHORT_A_LINEAGE_CAPTURE_ENABLED"] = previous


def test_runner_never_falls_back_to_application_credentials():
    source = (ROOT / "app" / "research" /
              "me_short_a_forward_outcome_runtime_runner_v1.py").read_text(encoding="utf-8")
    assert "ME_SHORT_A_RUNTIME_DB_HOST" in source
    assert "refuses to fall back to application credentials" in source
    assert "load_dotenv" not in source


def _registry_path():
    return ROOT / "app" / "research" / \
        "me_short_a_forward_outcome_runtime_registry_v1.json"


# ── documentation and runbook ──────────────────────────────────────────


def test_docs_exist_and_cover_the_runtime_contract():
    assert DOCS.exists()
    text = DOCS.read_text(encoding="utf-8")
    for needle in ("ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1",
                   "capture_source_signal", "record_event_lineage",
                   "capture_post_signal_candle", "finalize_outcome",
                   "event_key", "STOP_FIRST", "240m", "CLOSED 5m",
                   "forward-only", "append-only", "idempotency",
                   "heartbeat", "lifecycle",
                   "trad_bot_me_short_a_runtime",
                   "no-lookahead", "Issue #208"):
        assert needle in text, needle


def test_runbook_has_a_deployment_and_a_rollback_section():
    assert RUNBOOK.exists()
    text = RUNBOOK.read_text(encoding="utf-8")
    upper = text.upper()
    assert "DEPLOYMENT" in upper
    assert "ROLLBACK" in upper
    assert "POST-DEPLOY" in upper or "HEALTH" in upper
    for needle in ("ConditionPathExists", "me_short_a_forward_runtime_enabled",
                   "trad-bot-me-short-a-forward-outcome",
                   "freeze_ts", "READY_TO_START", "RUNNING",
                   "Exit 3", "rollback"):
        assert needle.lower() in text.lower(), needle
    # The runbook must state that nothing here is performed by the task.
    assert "not performed" in text.lower() or "not applied" in text.lower()


def test_runbook_documents_the_isolation_guarantees():
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    for needle in ("research.me_short_a_fill_feasibility_v1",
                   "prospective_observation", "prospective_outcome",
                   "scanner", "paper", "live", "no backfill"):
        assert needle in text, needle


# ── CI wiring ──────────────────────────────────────────────────────────


def test_ci_workflow_covers_the_new_runtime_files():
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in ("me_short_a_forward_outcome_runtime_v1.py",
                   "me_short_a_forward_outcome_runtime_sources_v1.py",
                   "me_short_a_forward_outcome_runtime_runner_v1.py",
                   "me_short_a_forward_outcome_runtime_registry_v1.json",
                   "079_me_short_a_forward_outcome_runtime_integration_v1.sql",
                   "me_short_a_forward_outcome_runtime_least_privilege_v1.sql",
                   "test_me_short_a_forward_outcome_runtime_v1.py",
                   "test_me_short_a_forward_outcome_runtime_migration_079_v1.py",
                   "trad-bot-me-short-a-forward-outcome.service"):
        assert needle in text, needle


# ── helper ─────────────────────────────────────────────────────────────


_NOT_A_COLUMN = {
    "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES",
    "OR", "AND", "NOT", "NULL", "DEFAULT", "ON", "WITH", "IN", "IS",
}


def _create_table_columns(sql: str) -> list[str]:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS research\."
        r"me_short_a_forward_outcome_runtime_heartbeat_v1 \((.*?)\n\);",
        sql, re.S)
    assert match, "heartbeat CREATE TABLE not found"
    body = match.group(1)
    columns = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.upper() in _NOT_A_COLUMN:
            continue
        if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            columns.append(name)
    return columns
