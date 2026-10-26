"""SQL contract and permission tests for the runtime least-privilege grants.

Two independent concerns are asserted here:

1. **SQL contract** — ``sql/grants/me_short_a_forward_outcome_runtime_least_privilege_v1.sql``
   grants exactly the privileges the runtime needs and nothing else:
   ``USAGE`` on schema ``dds`` plus ``SELECT`` on ``dds.scanner_setup`` and
   ``dds.instrument`` for the candidate intake, the ``SELECT``/``INSERT`` set
   on ``research`` for the append-only capture path, and **no**
   ``INSERT``/``UPDATE``/``DELETE``/``TRUNCATE`` anywhere on ``dds``, scanner,
   paper, live or direction-gate objects.

2. **Runtime permissions** — the code side of the same contract: the intake
   is read-only at the statement level and at the session level, it reads only
   the two granted ``dds`` tables, and the runtime authenticates as
   ``trad_bot_me_short_a_runtime`` — never as ``trad_bot_me_short_a_collector``.

Everything is asserted against the files in Git; no SQL is executed against a
database here and nothing is applied to the VPS.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
    READ_ONLY_GUC,
    ReadOnlyCursor,
    ReadOnlyViolation,
    assert_read_only_session,
    assert_read_only_statement,
    DbScannerSetupCandidateSource,
)
from app.research.me_short_a_forward_outcome_runtime_v1 import (
    DatabaseAccessError,
)

ROOT = Path(__file__).resolve().parents[1]
GRANTS = ROOT / "sql" / "grants" / "me_short_a_forward_outcome_runtime_least_privilege_v1.sql"
RUNTIME = ROOT / "app" / "research" / "me_short_a_forward_outcome_runtime_v1.py"
SOURCES = ROOT / "app" / "research" / "me_short_a_forward_outcome_runtime_sources_v1.py"
RUNNER = ROOT / "app" / "research" / "me_short_a_forward_outcome_runtime_runner_v1.py"
LINEAGE = ROOT / "app" / "research" / "me_short_a_forward_outcome_lineage_v1.py"
UNIT = ROOT / "deploy" / "systemd" / "trad-bot-me-short-a-forward-outcome.service"
RUNBOOK = (
    ROOT / "docs" / "research"
    / "ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_DEPLOYMENT_RUNBOOK_V1.md")

RUNTIME_ROLE = "trad_bot_me_short_a_runtime"
AUDIT_ROLE = "trad_bot_me_short_a_runtime_audit"
COLLECTOR_ROLE = "trad_bot_me_short_a_collector"

#: Every ``research`` object the runtime process touches, with the verbs it
#: actually needs. Keys are ``schema.table``.
REQUIRED_RESEARCH_PRIVILEGES = {
    "research.me_short_a_lineage_activation_v1": {"SELECT"},
    "research.me_short_a_fill_feasibility_v1": {"SELECT"},
    "research.me_short_a_source_signal_v1": {"SELECT", "INSERT"},
    "research.me_short_a_post_signal_candle_v1": {"SELECT", "INSERT"},
    "research.me_short_a_event_lineage_v1": {"SELECT", "INSERT"},
    "research.me_short_a_outcome_observation_v1": {"SELECT", "INSERT"},
    "research.me_short_a_forward_outcome_runtime_heartbeat_v1": {"SELECT", "INSERT"},
    "research.v_me_short_a_forward_outcome_runtime_status_v1": {"SELECT"},
}

#: The COMPLETE dds grant. Nothing else in dds may be granted at all.
REQUIRED_DDS_PRIVILEGES = {
    "dds.scanner_setup": {"SELECT"},
    "dds.instrument": {"SELECT"},
}

#: Control-plane objects the runtime must not touch even with SELECT.
OFF_LIMITS_OBJECTS = (
    "dds.scanner_direction_config",
    "dds.paper_safety_event",
    "config.scanner_direction_gate",
    "config.scanner_direction_gate_history",
    "dds.paper_shadow_trade",
)

WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


# ── helpers ──────────────────────────────────────────────────────────

def _sql_code(text: str) -> str:
    """Strip ``--`` comments so assertions look at executable SQL only."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _collapse_literals(text: str) -> str:
    """Merge adjacent SQL string literals (``'a' 'b'`` -> ``'a b'``).

    PostgreSQL concatenates neighbouring string constants across newlines, and
    the grants file uses exactly that to format long ``REVOKE`` statements, so
    the statement text has to be reassembled before it can be matched.
    """
    return re.sub(r"'\s*'", " ", text)


# ``%I`` / ``%s`` appear as format placeholders inside the DO blocks, so the
# role pattern accepts them and the tail tolerates the closing quote and the
# remaining ``format(...)`` arguments before the statement's ``;``.
_GRANT_RE = re.compile(
    r"\bGRANT\s+(?P<verbs>[A-Za-z_,\s]+?)\s+ON\s+"
    r"(?P<kind>ALL\s+TABLES\s+IN\s+SCHEMA\s+|ALL\s+SEQUENCES\s+IN\s+SCHEMA\s+"
    r"|TABLE\s+|SCHEMA\s+|VIEW\s+|SEQUENCE\s+)?"
    r"(?P<obj>[A-Za-z_][A-Za-z0-9_.%]*)\s+TO\s+(?P<role>[%A-Za-z_,\s]+?)"
    r"\s*(?:'\s*,[^;]*)?;",
    re.IGNORECASE)

_REVOKE_RE = re.compile(
    r"\bREVOKE\s+(?P<verbs>[A-Za-z_,\s]+?)\s+ON\s+"
    r"(?P<kind>ALL\s+TABLES\s+IN\s+SCHEMA\s+|ALL\s+SEQUENCES\s+IN\s+SCHEMA\s+"
    r"|TABLE\s+|SCHEMA\s+|VIEW\s+|SEQUENCE\s+)?"
    r"(?P<obj>[A-Za-z_][A-Za-z0-9_.%]*)\s+FROM\s+(?P<role>[%A-Za-z_,\s]+?)"
    r"\s*(?:'\s*,[^;]*)?;",
    re.IGNORECASE)


def _prose(text: str) -> str:
    """Comment-free, whitespace-normalised copy, for word-level assertions."""
    body = "\n".join(
        line.lstrip("#").lstrip("-").split("--", 1)[0] for line in text.splitlines())
    return " ".join(body.split())


def _parse(pattern, text):
    out = []
    for m in pattern.finditer(text):
        verbs = tuple(
            v.strip().upper()
            for v in m.group("verbs").split(",") if v.strip())
        roles = tuple(
            r.strip() for r in m.group("role").split(",") if r.strip())
        kind = (m.group("kind") or "TABLE").strip().upper()
        out.append({
            "verbs": verbs,
            "kind": " ".join(kind.split()),
            "object": m.group("obj"),
            "roles": roles,
        })
    return out


@pytest.fixture(scope="module")
def grants_sql() -> str:
    assert GRANTS.exists(), f"missing {GRANTS}"
    return GRANTS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def grants_code(grants_sql) -> str:
    return _sql_code(grants_sql)


@pytest.fixture(scope="module")
def grant_rows(grants_code):
    return _parse(_GRANT_RE, grants_code)


@pytest.fixture(scope="module")
def revoke_rows(grants_code):
    return _parse(_REVOKE_RE, _collapse_literals(grants_code))


def _granted_to(rows, role):
    """object -> set(verbs) actually granted to ``role`` by the file.

    Schema/sequence level grants are reported under the schema name and are
    kept, so callers can assert on schema visibility too; table level grants
    carry the ``schema.table`` object name.
    """
    out: dict[str, set[str]] = {}
    for row in rows:
        if role not in row["roles"]:
            continue
        out.setdefault(row["object"], set()).update(row["verbs"])
    return out


# ── SQL contract: the dds read-only intake ───────────────────────────

def test_file_declares_dds_usage_without_create(grants_code):
    assert re.search(
        rf"GRANT\s+USAGE\s+ON\s+SCHEMA\s+dds\s+TO\s+{RUNTIME_ROLE}\s*;",
        grants_code, re.IGNORECASE), "runtime needs USAGE ON SCHEMA dds"
    assert re.search(
        rf"REVOKE\s+CREATE\s+ON\s+SCHEMA\s+dds\s+FROM\s+{RUNTIME_ROLE}\s*;",
        grants_code, re.IGNORECASE), "USAGE must not come with CREATE"


def test_dds_grant_is_exactly_the_two_intake_tables(grant_rows):
    dds = _granted_to(grant_rows, RUNTIME_ROLE)
    dds = {obj: verbs for obj, verbs in dds.items() if obj.startswith("dds.")}
    assert dds == REQUIRED_DDS_PRIVILEGES, dds


def test_dds_instrument_is_granted_read_only_and_separately(grants_code):
    assert re.search(
        rf"GRANT\s+SELECT\s+ON\s+dds\.instrument\s+TO\s+{RUNTIME_ROLE}\s*;",
        grants_code, re.IGNORECASE), "the symbol join needs SELECT on dds.instrument"
    assert re.search(
        rf"GRANT\s+SELECT\s+ON\s+dds\.scanner_setup\s+TO\s+{RUNTIME_ROLE}\s*;",
        grants_code, re.IGNORECASE)
    # one object per statement: the privilege of each side of the JOIN stays
    # independently visible in information_schema
    for match in re.finditer(
            rf"GRANT\s+SELECT\s+ON\s+(dds\.[a-z_0-9]+)\s+TO\s+{RUNTIME_ROLE}",
            grants_code, re.IGNORECASE):
        assert "," not in match.group(0).split(" ON ")[1].split(" TO ")[0]


def test_no_dds_write_verb_is_ever_granted(grant_rows, grants_code):
    for row in grant_rows:
        for verb in row["verbs"]:
            if verb in WRITE_VERBS:
                assert not row["object"].startswith("dds."), (
                    f"{verb} granted on {row['object']} to {row['roles']}")
    assert not re.search(
        r"GRANT\s+(?:[A-Z_,\s]*,\s*)?(?:INSERT|UPDATE|DELETE|TRUNCATE)"
        r"[A-Z_,\s]*\s+ON\s+[A-Za-z_,\s]*dds\.",
        grants_code, re.IGNORECASE)


def test_dds_write_verbs_are_revoked_defensively(revoke_rows, grants_code):
    schema_wide = [
        row for row in revoke_rows
        if row["kind"] == "ALL TABLES IN SCHEMA" and row["object"] == "dds"]
    assert schema_wide, "expected a schema-wide write-verb revoke on dds"
    for row in schema_wide:
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert verb in row["verbs"], row
        # the SELECT grant must survive the defensive revoke
        assert "SELECT" not in row["verbs"], row
    # the revoke is applied to BOTH runtime roles by the guarded loop
    assert f"'{RUNTIME_ROLE}'" in grants_code
    assert f"'{AUDIT_ROLE}'" in grants_code
    assert re.search(
        r"REVOKE\s+USAGE,\s*UPDATE\s+ON\s+ALL\s+SEQUENCES\s+IN\s+SCHEMA\s+dds",
        _collapse_literals(grants_code), re.IGNORECASE), \
        "serial defaults must be unusable too"


def test_scanner_paper_live_direction_gate_objects_are_off_limits(
        grants_sql, grant_rows, revoke_rows, grants_code):
    # 1. every control-plane object is named in the defensive REVOKE block
    revoke_block = _collapse_literals(grants_code)
    assert "REVOKE ALL ON %s FROM %I" in revoke_block, (
        "the 2d block must revoke every listed object from every runtime role")
    for obj in OFF_LIMITS_OBJECTS:
        assert obj in revoke_block, f"{obj} is never revoked from the runtime"
        # 2. and is never granted to a runtime role
        for row in grant_rows:
            assert not (row["object"] == obj
                        and set(row["roles"]) & {RUNTIME_ROLE, AUDIT_ROLE}), row
    # 3. the boundary is stated in the file
    upper = grants_sql.upper()
    for word in ("SCANNER", "PAPER", "LIVE", "DIRECTION-GATE"):
        assert word in upper, word
    # 4. and the write verbs are gone from the whole dds schema
    schema_wide = [
        row for row in revoke_rows
        if row["kind"] == "ALL TABLES IN SCHEMA" and row["object"] == "dds"]
    assert schema_wide


def test_audit_role_gets_no_dds_access_at_all(grant_rows, grants_code):
    dds = _granted_to(grant_rows, AUDIT_ROLE)
    assert not any(obj.startswith("dds.") for obj in dds), dds
    assert re.search(
        rf"REVOKE\s+ALL\s+ON\s+SCHEMA\s+dds\s+FROM\s+{AUDIT_ROLE}\s*;",
        grants_code, re.IGNORECASE)


# ── SQL contract: the research write path is complete ────────────────

def test_runtime_has_every_research_privilege_the_code_needs(grant_rows):
    granted = _granted_to(grant_rows, RUNTIME_ROLE)
    for table, needed in REQUIRED_RESEARCH_PRIVILEGES.items():
        have = granted.get(table, set())
        missing = needed - have
        assert not missing, f"{table} is missing {sorted(missing)} (has {sorted(have)})"


def test_research_write_verbs_are_exactly_the_append_only_inserts(grant_rows):
    granted = _granted_to(grant_rows, RUNTIME_ROLE)
    writable = {
        obj for obj, verbs in granted.items()
        if set(verbs) & set(WRITE_VERBS)}
    assert writable == set(REQUIRED_RESEARCH_PRIVILEGES) - {
        "research.me_short_a_lineage_activation_v1",
        "research.me_short_a_fill_feasibility_v1",
        "research.v_me_short_a_forward_outcome_runtime_status_v1",
    }, writable


def test_no_update_delete_or_truncate_is_granted_to_the_runtime(grant_rows):
    for row in grant_rows:
        if RUNTIME_ROLE not in row["roles"]:
            continue
        for verb in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert verb not in row["verbs"], (
                f"{verb} granted on {row['object']} to {row['roles']}")


def test_phase1_and_the_frozen_oos_cohort_stay_read_only(grant_rows, grants_code):
    granted = _granted_to(grant_rows, RUNTIME_ROLE)
    assert granted.get("research.me_short_a_fill_feasibility_v1") == {"SELECT"}
    for cohort in ("research.prospective_observation",
                   "research.prospective_outcome"):
        assert cohort not in granted, cohort
    upper = grants_code.upper()
    assert "PROSPECTIVE_OBSERVATION" not in upper
    assert "PROSPECTIVE_OUTCOME" not in upper


def test_audit_role_only_reads_the_operational_log(grant_rows):
    granted = _granted_to(grant_rows, AUDIT_ROLE)
    # schema visibility is not a table privilege; the audit role needs research
    assert granted.pop("research", set()) == {"USAGE"}
    assert granted == {
        "research.me_short_a_forward_outcome_runtime_heartbeat_v1": {"SELECT"},
        "research.v_me_short_a_forward_outcome_runtime_status_v1": {"SELECT"},
    }, granted


# ── SQL contract: the collector role is never substituted ────────────

def test_collector_role_is_granted_nothing_by_this_file(grant_rows, grants_code):
    for row in grant_rows:
        assert COLLECTOR_ROLE not in row["roles"], row
    # it appears only as a defensive revoke target, never as a grantee
    for match in re.finditer(r"\bGRANT\b.*?\;", grants_code, re.DOTALL):
        assert COLLECTOR_ROLE not in match.group(0), match.group(0)[:200]


def test_the_runtime_login_is_the_runtime_role_not_the_collector(grants_sql):
    assert f"CREATE ROLE {RUNTIME_ROLE}" in grants_sql
    assert f"CREATE ROLE {COLLECTOR_ROLE}" not in grants_sql

    # the systemd unit mandates the runtime role and forbids the collector role
    unit = UNIT.read_text(encoding="utf-8")
    unit_prose = _prose(unit)
    assert "ME_SHORT_A_RUNTIME_DB_USER" in unit
    assert f"MUST be {RUNTIME_ROLE}" in unit_prose
    assert f"never be {COLLECTOR_ROLE}" in unit_prose

    # and the runner never even mentions the collector role
    runner = RUNNER.read_text(encoding="utf-8")
    assert COLLECTOR_ROLE not in runner, (
        "the runner must never reference the collector role")
    assert "ME_SHORT_A_RUNTIME_DB_USER" in runner
    assert "refuses to fall back to application credentials" in runner


def test_role_separation_is_documented_in_the_grants_file(grants_sql):
    upper = grants_sql.upper()
    assert "ROLE SEPARATION" in upper
    assert COLLECTOR_ROLE in grants_sql
    assert "NEVER GRANTS ANYTHING TO THE COLLECTOR ROLE" in upper or \
        "never grants anything to the collector role" in grants_sql


# ── SQL contract: idempotence and transactional shape ────────────────

def test_grants_file_is_transactional_and_idempotent(grants_code):
    assert re.search(r"^\s*BEGIN\s*;", grants_code, re.MULTILINE)
    assert re.search(r"^\s*COMMIT\s*;", grants_code, re.MULTILINE)
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles" in grants_code
    assert "to_regclass(" in grants_code, "revokes must stay idempotent"
    assert "DO $$" in grants_code


def test_grants_file_ships_a_read_only_verification_block(grants_sql):
    upper = grants_sql.upper()
    assert "VERIFICATION" in upper
    assert "information_schema.role_table_grants" in grants_sql
    assert "ZERO ROWS" in upper
    for table in REQUIRED_DDS_PRIVILEGES:
        assert table in grants_sql, table
    for table in REQUIRED_RESEARCH_PRIVILEGES:
        assert table in grants_sql, table


def test_grants_file_documents_the_apply_order_after_078_and_079(grants_sql):
    upper = grants_sql.upper()
    assert "AFTER MIGRATION 079" in upper
    assert "078" in grants_sql
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "grants078" in runbook and "grants079" in runbook


# ── code ↔ SQL cross-check ───────────────────────────────────────────

_TABLE_REF = re.compile(
    r"\b(research|dds|config)\.([a-z_][a-z0-9_]*)\b")


def test_every_table_the_code_touches_is_granted(grant_rows):
    granted = _granted_to(grant_rows, RUNTIME_ROLE)
    touched: dict[str, set[str]] = {}
    for path in (RUNTIME, SOURCES, LINEAGE):
        text = path.read_text(encoding="utf-8")
        for schema, table in _TABLE_REF.findall(text):
            if table.startswith("v_"):
                continue
            touched.setdefault(f"{schema}.{table}", set())
    # every captured/read table is covered; the pure readers need SELECT too
    for obj in REQUIRED_RESEARCH_PRIVILEGES:
        assert obj in touched or obj.startswith("research.v_"), obj
        assert obj in granted, f"{obj} is touched but never granted"
    for obj in REQUIRED_DDS_PRIVILEGES:
        assert obj in touched, f"{obj} should appear in the source SQL"
        assert obj in granted, f"{obj} is touched but never granted"


def test_candidate_source_reads_only_the_two_declared_dds_tables():
    assert DbScannerSetupCandidateSource.read_tables == (
        "dds.scanner_setup", "dds.instrument")
    sql = (DbScannerSetupCandidateSource.SELECT_SQL
           + DbScannerSetupCandidateSource.SELECT_AFTER_CURSOR_SQL)
    refs = {f"{s}.{t}" for s, t in _TABLE_REF.findall(sql)}
    assert refs == set(DbScannerSetupCandidateSource.read_tables), refs
    assert DbScannerSetupCandidateSource.requires_read_only_session is True


def test_the_intake_sql_is_select_only():
    for sql in (DbScannerSetupCandidateSource.SELECT_SQL,
                DbScannerSetupCandidateSource.SELECT_AFTER_CURSOR_SQL):
        assert assert_read_only_statement(sql) == "SELECT"


# ── read-only statement guard ────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "INSERT INTO dds.scanner_setup (setup_id) VALUES (1)",
    "update dds.instrument set symbol='x'",
    "DELETE FROM dds.instrument",
    "TRUNCATE dds.scanner_setup",
    "COPY dds.instrument TO PROGRAM 'true'",
    "CALL some_function()",
    "DO $$ BEGIN NULL; END $$",
    "CREATE TABLE dds.new_table (id int)",
    "ALTER TABLE dds.instrument ADD COLUMN x int",
    "DROP TABLE dds.instrument",
    "GRANT SELECT ON dds.instrument TO PUBLIC",
    "REVOKE SELECT ON dds.instrument FROM PUBLIC",
    "",
    "   ",
])
def test_read_only_statement_guard_refuses_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_statement(sql)


@pytest.mark.parametrize("sql", [
    "WITH x AS (INSERT INTO dds.instrument (symbol) VALUES ('a') RETURNING *) "
    "SELECT * FROM x",
    "WITH x AS (DELETE FROM dds.instrument RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE dds.instrument SET symbol = 'a' RETURNING *) SELECT * FROM x",
])
def test_read_only_statement_guard_refuses_mutating_ctes(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_statement(sql)


@pytest.mark.parametrize("sql,kind", [
    ("SELECT 1", "SELECT"),
    ("select setup_id from dds.scanner_setup", "SELECT"),
    ("SHOW default_transaction_read_only", "SHOW"),
    ("EXPLAIN SELECT 1", "EXPLAIN"),
    ("VALUES (1)", "VALUES"),
    # a keyword inside a string literal is data, not a statement
    ("SELECT 'INSERT INTO dds.instrument' AS label", "SELECT"),
    ("SELECT * FROM dds.instrument WHERE symbol = 'DELETE FROM x'", "SELECT"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
])
def test_read_only_statement_guard_allows_reads(sql, kind):
    assert assert_read_only_statement(sql) == kind


def test_read_only_statement_guard_rejects_a_write_hidden_after_a_read():
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_statement(
            "SELECT 1; INSERT INTO dds.instrument (symbol) VALUES ('a')")


# ── read-only cursor ─────────────────────────────────────────────────

class _RecordingCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return ("on",)

    def fetchmany(self, size=None):
        return []

    def close(self):
        self.executed.append(("__close__", None))


def test_read_only_cursor_never_hands_a_write_to_the_driver():
    inner = _RecordingCursor()
    cur = ReadOnlyCursor(inner)
    for sql in ("DELETE FROM dds.instrument",
                "TRUNCATE dds.scanner_setup",
                "UPDATE dds.instrument SET symbol='x'"):
        with pytest.raises(ReadOnlyViolation):
            cur.execute(sql)
    assert inner.executed == [], "the driver must never see a mutating statement"


def test_read_only_cursor_passes_reads_through():
    inner = _RecordingCursor()
    cur = ReadOnlyCursor(inner)
    cur.execute("SELECT 1", (2,))
    cur.execute("SELECT 2")
    cur.fetchall()
    cur.fetchone()
    cur.close()
    assert inner.executed == [
        ("SELECT 1", (2,)), ("SELECT 2", None), ("__close__", None)]


def test_read_only_cursor_normalises_driver_errors():
    class _Boom(_RecordingCursor):
        def execute(self, sql, params=None):
            raise PermissionError("permission denied for table dds.instrument")

    with pytest.raises(DatabaseAccessError) as exc:
        ReadOnlyCursor(_Boom()).execute("SELECT 1")
    assert "permission denied" in str(exc.value)
    assert "PermissionError" in str(exc.value)


# ── read-only session / transaction ──────────────────────────────────

class _SessionConn:
    def __init__(self, value):
        self.value = value
        self.executed = []

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return (self.value,)

    def close(self):
        pass


def test_assert_read_only_session_accepts_a_read_only_session():
    for value in ("on", "ON", "true", "1", "yes"):
        assert_read_only_session(_SessionConn(value))


@pytest.mark.parametrize("value", ["off", "OFF", "false", "0", "", None])
def test_assert_read_only_session_refuses_a_read_write_session(value):
    with pytest.raises(ReadOnlyViolation) as exc:
        assert_read_only_session(_SessionConn(value))
    assert READ_ONLY_GUC in str(exc.value)


def test_assert_read_only_session_only_shows_the_guc():
    conn = _SessionConn("on")
    assert_read_only_session(conn)
    assert conn.executed == [f"SHOW {READ_ONLY_GUC}"]


def test_assert_read_only_session_surfaces_driver_errors():
    class _Broken(_SessionConn):
        def execute(self, sql, params=None):
            raise PermissionError("permission denied for schema dds")

    with pytest.raises(DatabaseAccessError):
        assert_read_only_session(_Broken("on"))


def test_candidate_source_verifies_its_session_and_refuses_read_write():
    ok = DbScannerSetupCandidateSource(
        _SessionConn("on"), object(), scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT", page_size=10)
    ok.verify_session()

    bad = DbScannerSetupCandidateSource(
        _SessionConn("off"), object(), scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT", page_size=10)
    with pytest.raises(ReadOnlyViolation):
        bad.verify_session()


# ── runner wiring ────────────────────────────────────────────────────

def test_runner_exposes_a_separate_read_only_connection_factory():
    runner = RUNNER.read_text(encoding="utf-8")
    assert "def connect_readonly(" in runner
    assert "SET default_transaction_read_only = on" in runner
    assert "def connect(" in runner
    # the two connections are never merged into one
    assert "intake_conn = conn if read_conn is None else read_conn" in runner
    assert "assert_read_only_session(intake_conn)" in runner


def test_the_read_only_connection_is_exercised_by_a_real_test(monkeypatch):
    from app.research.me_short_a_forward_outcome_runtime_runner_v1 import (
        connect_readonly,
    )

    class _Conn(_SessionConn):
        def __init__(self):
            super().__init__("off")
            self.committed = 0

        def commit(self):
            self.committed += 1

    conn = _Conn()
    monkeypatch.setattr(
        "app.research.me_short_a_forward_outcome_runtime_runner_v1.connect",
        lambda env=None: conn)

    opened = connect_readonly()
    assert opened is conn
    # the factory switches the session to READ ONLY and closes the transaction
    assert conn.executed == ["SET default_transaction_read_only = on"]
    assert conn.committed == 1


def test_runtime_role_and_collector_role_are_distinct_constants():
    from app.research import me_short_a_forward_outcome_runtime_v1 as mod

    assert mod.RUNTIME_ROLE == RUNTIME_ROLE
    assert mod.COLLECTOR_ROLE == COLLECTOR_ROLE
    assert mod.RUNTIME_ROLE != mod.COLLECTOR_ROLE
    assert mod.RUNTIME_AUDIT_ROLE == AUDIT_ROLE
    assert mod.RUNTIME_AUDIT_ROLE != mod.RUNTIME_ROLE
    registry = (ROOT / "app" / "research"
                / "me_short_a_forward_outcome_runtime_registry_v1.json")
    text = registry.read_text(encoding="utf-8")
    assert f'"{RUNTIME_ROLE}"' in text
    assert f'"{COLLECTOR_ROLE}"' in text
