"""Issue #324 / H1-E — deploy SQL documentation audit.

Every SQL statement published for operators (deploy runbook, Phase B
readiness report, recovery/rollback scripts) must:

1. be strictly READ-ONLY (the assistant and the docs never write);
2. reference ONLY real tables and columns of the #305 source schema
   (``sql/migrations/094_me_upstream_source_capture_v1.sql``) and of the
   reviewed H1-E DDL proposal.

Guards against a repeat of the reviewed defect (HIGH): a preflight query
addressing the non-existent ``source_event.status`` column.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

RUNBOOK = ROOT / "docs/research/324_h1e_deploy_runbook_v1.md"
PHASE_B = ROOT / "docs/research/324_h1e_phase_b_readiness_report_v1.md"
COMPAT = ROOT / "docs/research/324_h1e_phase_c_compat_matrix_v1.md"
INTEGRATION = ROOT / "docs/research/324_h1e_phase_c_integration_v1.md"
RECOVERY_SQL = ROOT / "deploy/sql/324_h1e_observer_recovery.sql"
ROLLBACK_SQL = (
    ROOT / "sql/proposals/324_h1e_prospective_ledger_v1_rollback.sql")
MIGRATION_094 = ROOT / "sql/migrations/094_me_upstream_source_capture_v1.sql"
H1E_DDL = (
    ROOT / "sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql")

SOURCE_TABLE = "research_me_upstream_coverage.source_event"
H1E_SCHEMA = "research_me_entry_quality_h1e"

WRITE_VERBS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REFRESH)\b", re.IGNORECASE)

SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in",
    "group", "by", "having", "order", "limit", "offset", "as", "distinct",
    "between", "like", "similar", "to", "ilike", "interval", "case", "when",
    "then", "else", "end", "asc", "desc", "nulls", "first", "last", "union",
    "all", "on", "conflict", "do", "nothing", "set", "into", "values",
    "with", "only", "exists", "filter", "over", "partition", "rows",
    "range", "unbounded", "preceding", "following", "current", "row",
    "true", "false", "day", "hour", "minute", "second", "epoch", "join",
    "inner", "outer", "left", "right", "cross", "using", "primary", "key",
    "references", "check", "default", "generated", "always", "identity",
    "constraint", "unique", "index", "table", "schema", "begin", "commit",
    "rollback", "declare", "language", "returns", "trigger", "function",
    "replace", "or", "each", "statement", "before", "after", "insert",
    "update", "delete", "for", "of", "raise", "exception", "if", "loop",
    "end", "perform", "return", "new", "old", "execute", "using", "text",
    "bigint", "integer", "boolean", "numeric", "jsonb", "timestamptz",
    "serial", "now", "count", "min", "max", "sum", "avg", "coalesce",
    "extract", "date_trunc", "round", "floor", "ceil", "greatest", "least",
    "length", "upper", "lower", "digest", "encode", "hex", "pg_catalog",
    "information_schema", "tables", "columns", "table_schema", "table_name",
    "column_name", "percent", "s", "d", "e", "t", "x", "act", "new",
}

SCHEMA_NAMES = {H1E_SCHEMA, "research_me_upstream_coverage", "public"}


def _table_columns(ddl_text: str, table: str) -> set[str]:
    """Extract column names of ``table`` from CREATE TABLE DDL text."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = ddl_text.index(marker) + len(marker)
    end = ddl_text.index("\n);", start)
    block = ddl_text[start:end]
    columns = set()
    for line in block.splitlines():
        match = re.match(r"\s{4}([a-z_][a-z0-9_]*)\s+[A-Z]", line)
        if match:
            columns.add(match.group(1))
    return columns


def _h1e_tables(ddl_text: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
            r"CREATE TABLE IF NOT EXISTS " + H1E_SCHEMA + r"\.(\w+) \(",
            ddl_text):
        name = match.group(1)
        tables[name] = _table_columns(ddl_text, f"{H1E_SCHEMA}.{name}")
    return tables


def _statements(text: str) -> list[str]:
    """Collect operator-facing SQL: fenced blocks, ``psql -c``, commented
    SELECT blocks in .sql docs, and active statements in .sql files."""
    statements: list[str] = []
    statements.extend(re.findall(r"```sql\n(.*?)```", text, re.DOTALL))
    statements.extend(re.findall(r'psql\s+-c\s+"([^"]+)"', text))
    # commented-out operator SQL (``-- SELECT ...``) is still shipped SQL
    statements.extend(
        match.strip()
        for match in re.findall(r"^\s*--\s+((?:SELECT|WITH)\b.*?;)",
                                text, re.DOTALL | re.MULTILINE))
    # active statements in .sql files
    statements.extend(
        match.strip()
        for match in re.findall(r"^\s*(SELECT\b.*?;)", text,
                                re.DOTALL | re.MULTILINE))
    return statements


def _strip_noise(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)          # line comments
    sql = re.sub(r"'[^']*'", " ", sql)           # string literals
    sql = re.sub(r"\$\$.*?\$\$", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"::\s*[A-Za-z_ ]+", " ", sql)  # casts
    sql = re.sub(r"\b[A-Za-z_]\w*\s*\(", " (", sql)  # function names
    return sql


def _identifiers(sql: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _strip_noise(sql))
    identifiers: set[str] = set()
    skip_next = False
    previous = ""
    for token in tokens:
        low = token.lower()
        if skip_next:
            skip_next = False
            previous = low
            continue
        if low == "as":
            skip_next = True
            previous = low
            continue
        if low in SQL_KEYWORDS or low in SCHEMA_NAMES:
            previous = low
            continue
        if previous in {"from", "join"}:
            # table reference — not a column
            previous = low
            continue
        if low in {"source_event", "h1e_event", "h1e_classification",
                   "h1e_status_event", "h1e_reject_event", "h1e_conflict_event",
                   "h1e_pending_retry", "h1e_checkpoint", "h1e_activation",
                   "h1e_provenance_snapshot", "h1e_coverage_audit",
                   "duplicates"}:
            previous = low
            continue
        identifiers.add(low)
        previous = low
    return identifiers


def _all_statements() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path in (RUNBOOK, PHASE_B, COMPAT, INTEGRATION, RECOVERY_SQL,
                 ROLLBACK_SQL):
        text = path.read_text(encoding="utf-8")
        for statement in _statements(text):
            pairs.append((path.name, statement))
    return pairs


STATEMENTS = _all_statements()


def test_docs_expose_sql_statements():
    # the audit is meaningful only if it sees the operator SQL
    joined = " ".join(statement for _, statement in STATEMENTS)
    assert "research_me_upstream_coverage.source_event" in joined
    assert "research_me_entry_quality_h1e" in joined


def test_every_operator_statement_is_read_only():
    for name, statement in STATEMENTS:
        active = re.sub(r"--[^\n]*", " ", statement)
        match = WRITE_VERBS.search(active)
        assert match is None, (
            f"{name}: write statement shipped to operators: "
            f"{match.group(0)!r} in {statement[:120]!r}")


def test_source_event_queries_reference_only_real_columns():
    allowed = _table_columns(MIGRATION_094.read_text(encoding="utf-8"),
                             SOURCE_TABLE)
    assert {"source_event_key", "source_identity", "signal_time",
            "captured_at", "activation_freeze_ts", "lineage", "lineage_hash"
            } <= allowed
    for name, statement in STATEMENTS:
        if SOURCE_TABLE not in statement:
            continue
        used = _identifiers(statement)
        unknown = sorted(used - allowed)
        assert unknown == [], (
            f"{name}: non-existent {SOURCE_TABLE} column(s) {unknown} in "
            f"{statement[:160]!r}")


def test_h1e_queries_reference_only_real_tables_and_columns():
    ddl = H1E_DDL.read_text(encoding="utf-8")
    tables = _h1e_tables(ddl)
    assert len(tables) == 10
    for name, statement in STATEMENTS:
        if H1E_SCHEMA not in statement:
            continue
        used_tables = {t for t in tables if t in statement}
        if not used_tables:
            # the schema name is only quoted/literal (e.g. information_schema
            # inventory queries) — no H1-E table is actually addressed
            continue
        allowed = set()
        for table in used_tables:
            allowed |= tables[table]
        used = _identifiers(statement)
        unknown = sorted(used - allowed - set(tables))
        assert unknown == [], (
            f"{name}: non-existent H1-E column(s) {unknown} in "
            f"{statement[:160]!r}")


def test_preflight_never_addresses_missing_status_column():
    """Exact reviewed defect (HIGH): ``source_event.status`` never existed."""
    for path in (RUNBOOK, PHASE_B, COMPAT, INTEGRATION, RECOVERY_SQL):
        text = path.read_text(encoding="utf-8")
        assert "SELECT status FROM research_me_upstream_coverage" not in text
        assert re.search(
            r"source_event\s+WHERE\s+status", text) is None, path
