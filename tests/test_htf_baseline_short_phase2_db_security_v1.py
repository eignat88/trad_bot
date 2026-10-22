"""Issue #176 Phase-2 DB privilege hardening (CP4): integration tests for
migration 074 -> 075 on DISPOSABLE PostgreSQL databases only.

SAFETY: connects to the local test server (P2_TEST_DB_*), creates/drops only
``trad_bot_p2_it_*`` databases and ephemeral roles; production ``trad_bot`` is
blocked by name. Connection targets are fail-closed: the server must be
loopback (or explicitly confirmed isolated via P2_TEST_DB_ISOLATION_ACK=yes)
and every test database name must match ``trad_bot_p2_it_*``. The module
skips when no local test server is reachable.

Covers: least-privilege observer role, SECURITY DEFINER insert wall, denied
direct DML, admin-only activation, freeze-registry isolation, unchanged grants
on pre-existing research objects, concurrency, eligibility rejection, pg8000
driver path (SCRAM-capable) and transaction rollback.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
from psycopg2 import sql as pgsql  # noqa: E402

from app.research.htf_baseline_short_phase2_persistence_v1 import (
    insert_observation_raw,
)

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _assert_isolation_confirmed() -> None:
    """Fail-closed connection guard: never run DB-creating tests against a
    server that is not confirmed as an isolated test cluster."""
    if DB_HOST not in _LOOPBACK and os.environ.get("P2_TEST_DB_ISOLATION_ACK") != "yes":
        raise RuntimeError(
            "P2_TEST_DB_HOST is not loopback and isolation is not confirmed; "
            "refusing to run database-creating tests. Point P2_TEST_DB_* at an "
            "isolated test cluster (e.g. 127.0.0.1:55432) or explicitly set "
            "P2_TEST_DB_ISOLATION_ACK=yes for a known-isolated cluster."
        )


def _assert_safe_database(name: str, *, maintenance: bool = False) -> None:
    """Fail-closed database-name guard: production names are always refused."""
    if maintenance:
        if name != "postgres":
            raise RuntimeError(f"refusing non-maintenance database target: {name}")
        return
    if name in _BLOCKED or not _DB_PATTERN.match(name):
        raise RuntimeError(f"refusing unsafe test database name: {name}")


# Fail closed at import time on an unconfirmed, non-loopback target.
_assert_isolation_confirmed()

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_074 = ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
MIGRATION_075 = ROOT / "sql" / "migrations" / "075_htf_baseline_short_phase2_privilege_hardening.sql"

EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
FINGERPRINT = "0" * 64
OBSERVER_ROLE = "trad_bot_htf_p2_observer"
ADMIN_ROLE = "trad_bot_htf_p2_admin"


def _future_freeze() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


def _signal_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _first_touch_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


def _admin_connect():
    _assert_isolation_confirmed()
    _assert_safe_database("postgres", maintenance=True)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=5)
    conn.autocommit = True
    return conn


def _server_available() -> bool:
    try:
        conn = _admin_connect()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(),
    reason=f"no local test PostgreSQL at {DB_HOST}:{DB_PORT}; security IT skipped",
)


class _Provisioned:
    """Bundle exposed by ``_provisioned_db`` (internal, test-injectable)."""

    def __init__(self, conn, name: str, admin, preexisting: set):
        self.conn = conn
        self.name = name
        self.admin = admin
        self.preexisting = preexisting


def _maybe_fail(fail_at: str | None, point: str) -> None:
    if fail_at == point:
        raise RuntimeError(f"injected failure at {point}")


def _drop_test_roles(admin, preexisting: set) -> None:
    """Drop only roles this run created; never touch pre-existing roles."""
    for role in (OBSERVER_ROLE, ADMIN_ROLE):
        if role in preexisting:
            continue
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                continue
            try:
                cur.execute(f"DROP OWNED BY {role}")  # clear residual ACL deps
            except Exception:  # pragma: no cover - best effort
                pass
            cur.execute(f"DROP ROLE {role}")


def _cleanup(admin, name: str, preexisting: set, conn=None) -> None:
    """Guaranteed teardown: drop the disposable database and created roles."""
    if conn is not None:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                for role in (OBSERVER_ROLE, ADMIN_ROLE):
                    if role not in preexisting:
                        try:
                            cur.execute(f"DROP OWNED BY {role}")
                        except Exception:  # pragma: no cover - role may not exist
                            pass
            conn.commit()
        except Exception:  # pragma: no cover - broken connection is fine
            pass
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (name,),
        )
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
    _drop_test_roles(admin, preexisting)


@contextlib.contextmanager
def _provisioned_db(fail_at: str | None = None, name: str | None = None):
    """Core of the ``db`` fixture: disposable database with 074 applied.

    Cleanup is guaranteed via try/finally for every failure stage:
    after CREATE DATABASE, at first connect, during migration 074, during the
    test body and during/after migration 075. ``fail_at`` injects a failure at
    a named point so the cleanup regression test can exercise each stage.
    """
    _assert_isolation_confirmed()
    name = name or f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    _assert_safe_database(name)
    admin = _admin_connect()
    preexisting = _existing_roles(admin)
    conn = None
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {name}")
        _maybe_fail(fail_at, "after_create_database")
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5
        )
        _maybe_fail(fail_at, "at_connect")
        with conn.cursor() as cur:
            cur.execute(MIGRATION_074.read_text(encoding="utf-8"))
        conn.commit()
        _maybe_fail(fail_at, "during_m74")
        yield _Provisioned(conn, name, admin, preexisting)
    finally:
        try:
            _cleanup(admin, name, preexisting, conn)
        finally:
            admin.close()


@pytest.fixture()
def db():
    """Disposable database with 074 applied; roles tracked for teardown."""
    with _provisioned_db() as prov:
        yield prov.conn


def _existing_roles(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles")
        return {row[0] for row in cur.fetchall()}


def apply_075(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_075.read_text(encoding="utf-8"))
    conn.commit()


class _RedactedSecret(str):
    """String whose str/repr never reveal the value (traceback/log safety).

    NOTE: psycopg2's connection-info builder converts non-exact ``str``
    values through ``str()``, so passing this object directly to
    ``psycopg2.connect(password=...)`` sends ``<redacted>`` and breaks
    SCRAM authentication. Always cross driver boundaries with ``reveal()``
    (a plain ``str`` carrying the real value; drivers never log it).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<redacted>"

    def __str__(self) -> str:
        return "<redacted>"

    def reveal(self) -> str:
        """Plain ``str`` with the real value — for driver boundaries only."""
        return self + ""


def _new_test_credential() -> str:
    """A REAL random password for the test observer role (never logged)."""
    return _RedactedSecret(secrets.token_urlsafe(32))


def _scram_verifier_matches(verifier: str, password: str) -> bool:
    """Check a pg_authid SCRAM-SHA-256 verifier against a password (RFC 5802).

    Verifies that the password actually stored on the role equals the test
    credential, without ever printing either value. SASLprep is identity for
    the ASCII random credentials used here.
    """
    try:
        import base64
        import hashlib
        import hmac

        scheme, iter_salt, stored_server = verifier.split("$")
        if scheme != "SCRAM-SHA-256":
            return False
        iterations_s, salt_b64 = iter_salt.split(":")
        stored_b64, server_b64 = stored_server.split(":")
        salt = base64.b64decode(salt_b64)
        salted = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations_s)
        )
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        return (
            base64.b64encode(stored_key).decode() == stored_b64
            and base64.b64encode(server_key).decode() == server_b64
        )
    except Exception:
        return False


def _install_observer_login(admin, password: str) -> None:
    """Set a real password on the TEST cluster's observer role.

    Test-cluster only (P2_TEST_DB_* fail-closed guard); production role
    passwords are never touched. The plain value (``reveal()``) is quoted via
    psycopg2.sql.Literal and never echoed. Works for trust (ignored), md5 and
    SCRAM-SHA-256 servers, so the integration tests are real under every
    allowed config.
    """
    with admin.cursor() as cur:
        cur.execute(
            pgsql.SQL("ALTER ROLE {} PASSWORD {}").format(
                pgsql.Identifier(OBSERVER_ROLE), pgsql.Literal(password.reveal())
            )
        )


def _activate(conn, freeze=None, fingerprint=FINGERPRINT):
    freeze = freeze or _future_freeze()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, freeze, fingerprint),
        )
    conn.commit()
    return freeze


def _enable_observer(conn):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research.htf_baseline_short_phase2_experiment "
            "SET observer_enabled = TRUE WHERE experiment_id = %s",
            (EXP,),
        )
    conn.commit()


def _event_kwargs(suffix: str = "ep1"):
    signal = _signal_time()  # known_at must equal signal_time (table CHECK)
    return {
        "source_event_id": f"{suffix}:FIRST_CONFIRM",
        "episode_id": suffix,
        "level_id": "lvl1",
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "first_touch_time": _first_touch_time(),
        "known_at": signal,
        "level_price": 110.5,
        "entry_reference_price": 109.9,
        "features": None,
    }


def _denied(conn_sql: str, conn, *params):
    """Run SQL expected to fail; return the raised exception."""
    with pytest.raises(Exception) as excinfo:
        with conn.cursor() as cur:
            cur.execute(conn_sql, params)
    conn.rollback()
    return excinfo.value


def _assert_insufficient_privilege(exc: Exception) -> None:
    """Privilege refusal must be SQLSTATE 42501 (locale independent)."""
    code = getattr(exc, "pgcode", None)
    text = str(exc)
    assert code == "42501" or "permission denied" in text or "нет доступа" in text, (
        f"expected insufficient privilege, got: {text[:160]}"
    )


# ------------------------------------------------------------------- roles


def test_075_creates_least_privilege_roles(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
            "WHERE rolname = %s",
            (OBSERVER_ROLE,),
        )
        obs = cur.fetchone()
        cur.execute(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s",
            (ADMIN_ROLE,),
        )
        adm = cur.fetchone()
    assert obs == (True, False, False, False)  # LOGIN, no admin attributes
    assert adm == (False, False)  # NOLOGIN admin procedure holder


def test_075_scoped_to_phase2_surface_static():
    """Migration 075 must never touch non-Phase-2 objects or grant broadly."""
    sql = MIGRATION_075.read_text(encoding="utf-8")
    upper = sql.upper()
    assert "GRANT ALL" not in upper
    assert "ALTER TABLE" not in upper  # no table surgery in a privilege migration
    assert "SECURITY DEFINER" in upper
    assert "SEARCH_PATH = PG_CATALOG, RESEARCH" in upper
    low = sql.lower()
    for forbidden in ("prospective", "srr_", "dds.", "analytics.", "mart.", "config.", "market."):
        assert forbidden not in low, f"075 must not mention {forbidden}"
    # every named schema object is a Phase-2 object
    named = set(re.findall(r"research\.([a-z0-9_]+)", low))
    assert named, "expected explicit research object names"
    for obj in named:
        assert obj.startswith("htf_baseline_short_phase2"), f"out-of-scope object: {obj}"


RUNBOOK = ROOT / "docs" / "research" / "HTF_BASELINE_SHORT_PHASE2_MIGRATION_074_075_RUNBOOK_V1.md"


def _runbook_verification_sql() -> list[str]:
    """Extract the marked verification statements from the migration runbook."""
    doc = RUNBOOK.read_text(encoding="utf-8")
    assert "-- VERIFICATION_SQL_BEGIN" in doc and "-- VERIFICATION_SQL_END" in doc
    block = doc.split("-- VERIFICATION_SQL_BEGIN", 1)[1].split("-- VERIFICATION_SQL_END", 1)[0]
    statements = []
    for raw in block.split(";"):
        lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if lines:
            statements.append("\n".join(lines))
    assert statements, "no verification SQL extracted from runbook"
    return statements


def _run_verification(db) -> list[tuple]:
    mismatches = []
    for stmt in _runbook_verification_sql():
        with db.cursor() as cur:
            cur.execute(stmt)
            for check, expected, actual in cur.fetchall():
                if expected != actual:
                    mismatches.append((check, expected, actual))
    db.rollback()
    return mismatches


def test_runbook_verification_sql_passes_after_075(db):
    """The runbook's own SQL must execute and PASS after a correct 074 -> 075."""
    apply_075(db)
    mismatches = _run_verification(db)
    assert not mismatches, mismatches


def test_runbook_verification_sql_flags_bad_grants(db):
    """Negative: artificially granted PUBLIC rights MUST be detected (NEW-1)."""
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(
            "GRANT INSERT ON research.htf_baseline_short_phase2_observation TO PUBLIC"
        )
        cur.execute(
            "GRANT EXECUTE ON FUNCTION research.htf_baseline_short_phase2_activate("
            "text,timestamp with time zone,text) TO PUBLIC"
        )
    db.commit()
    mismatches = _run_verification(db)
    flagged = {check for check, _, _ in mismatches}
    assert "pub_tbl" in flagged
    assert "pub_fn" in flagged
    assert "leftover_dml" in flagged


def test_runbook_verification_sql_flags_sequence_grants(db):
    """Negative: sequence grants to PUBLIC/observer MUST be detected (P3-VF1).

    Also proves the full block runs without SQL errors whether or not the
    `trad_bot` role exists (CASE-guarded rows) and that revoking the bad
    grants restores a clean PASS.
    """
    apply_075(db)
    seq = "research.htf_baseline_short_phase2_observation_observation_id_seq"
    with db.cursor() as cur:
        cur.execute(f"GRANT USAGE ON SEQUENCE {seq} TO PUBLIC")
        cur.execute(f"GRANT UPDATE ON SEQUENCE {seq} TO trad_bot_htf_p2_observer")
    db.commit()
    mismatches = _run_verification(db)
    flagged = {check for check, _, _ in mismatches}
    assert "seq_pub" in flagged
    assert "seq_observer" in flagged
    with db.cursor() as cur:
        cur.execute(f"REVOKE USAGE ON SEQUENCE {seq} FROM PUBLIC")
        cur.execute(f"REVOKE UPDATE ON SEQUENCE {seq} FROM trad_bot_htf_p2_observer")
    db.commit()
    assert not _run_verification(db)  # restored -> every check PASS again


_FAILURE_STAGES = (
    "after_create_database",
    "at_connect",
    "during_m74",
    "during_test",
    "during_m75",
)


@pytest.mark.parametrize("stage", _FAILURE_STAGES)
def test_fixture_cleanup_guaranteed_on_early_failure(stage):
    """A failure at ANY provisioning stage must leave zero leftover objects.

    Models errors after CREATE DATABASE but before the first connect, at
    connect time, during migration 074, during the test body and during
    migration 075 (with roles created); the disposable database and every
    fixture-created role must be gone afterwards, while pre-existing roles
    are never dropped.
    """
    snapshot = _admin_connect()
    try:
        preexisting = _existing_roles(snapshot)
    finally:
        snapshot.close()
    name = f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError):
        with _provisioned_db(name=name, fail_at=stage) as prov:
            if stage == "during_test":
                raise RuntimeError("injected failure at during_test")
            if stage == "during_m75":
                apply_075(prov.conn)  # roles exist when the failure hits
                raise RuntimeError("injected failure at during_m75")
    check = _admin_connect()
    try:
        with check.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() is None, "disposable database leaked"
            cur.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                ([OBSERVER_ROLE, ADMIN_ROLE],),
            )
            leaked = {r[0] for r in cur.fetchall()} - preexisting
            assert not leaked, f"roles leaked: {sorted(leaked)}"
    finally:
        check.close()


def test_systemd_unit_matches_exit_code_contract():
    """F5: EXIT_NOT_RUNNABLE=3 must not restart-loop; unit keeps its guards."""
    import app.research.htf_baseline_short_phase2_observer_runner_v1 as mod

    assert mod.EXIT_NOT_RUNNABLE == 3
    assert mod.EXIT_STATE_ERROR == 4
    assert mod.EXIT_WRITER_ERROR == 5
    assert mod.EXIT_OK == 0
    unit = (ROOT / "deploy" / "systemd" / "trad-bot-htf-baseline-short-observer.service").read_text(
        encoding="utf-8"
    )
    assert "RestartPreventExitStatus=3" in unit
    assert "Restart=on-failure" in unit
    assert "TimeoutStopSec=60" in unit
    assert "MemoryMax=512M" in unit  # resource limits preserved
    assert "TasksMax=64" in unit
    assert "LimitNOFILE=4096" in unit
    assert "ConditionPathExists=" in unit  # disabled by default preserved
    assert "UMask=0077" in unit
    assert "--activation-config" in unit
    assert "EnvironmentFile=/opt/trad_bot/etc/htf_phase2_observer.env" in unit


def test_075_requires_074_fail_closed(db):
    with db.cursor() as cur:
        cur.execute("DROP TABLE research.htf_baseline_short_phase2_freeze_registry")
    db.commit()
    with pytest.raises(Exception) as excinfo:
        apply_075(db)
    assert "P2_MIGRATION_074_REQUIRED" in str(excinfo.value)
    db.rollback()


# ----------------------------------------------- observer write wall (CP1)


def test_observer_insert_via_function_allowed(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    row_id = insert_observation_raw(db, experiment_id=EXP, **_event_kwargs())
    db.commit()
    assert row_id is not None
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_observer_direct_insert_update_delete_denied(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "INSERT INTO research.htf_baseline_short_phase2_observation "
        "(experiment_id, source_event_id, episode_id, level_id, symbol, "
        " signal_time, first_touch_time, known_at) "
        "VALUES (%s, 'e:FIRST_CONFIRM', 'e', 'l', 'BTCUSDT', now(), now(), now())",
        db, EXP,
    )
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("UPDATE research.htf_baseline_short_phase2_experiment SET observer_enabled = TRUE", db)
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("DELETE FROM research.htf_baseline_short_phase2_observation", db)
    _assert_insufficient_privilege(err)


def test_observer_cannot_activate(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
        db, EXP, _future_freeze(), FINGERPRINT,
    )
    _assert_insufficient_privilege(err)


def test_activation_is_admin_only(db):
    apply_075(db)
    # unprivileged role cannot even execute the activation function
    unauth = f"trad_bot_p2_it_unauth_{uuid.uuid4().hex[:6]}"
    admin = _admin_connect()
    with admin.cursor() as cur:
        cur.execute(f"CREATE ROLE {unauth} NOLOGIN")
    try:
        with db.cursor() as cur:
            cur.execute("GRANT USAGE ON SCHEMA research TO " + unauth)
            cur.execute(f"SET ROLE {unauth}")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            db, EXP, _future_freeze(), FINGERPRINT,
        )
        _assert_insufficient_privilege(err)
        with db.cursor() as cur:
            cur.execute(f"SET ROLE {unauth}")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_insert_observation("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            db, EXP, "e:FIRST_CONFIRM", "e", "l", "BTCUSDT",
            _signal_time(), _first_touch_time(), _signal_time(), 1.0, 1.0,
            False, False, "{}",
        )
        _assert_insufficient_privilege(err)
    finally:
        with db.cursor() as cur:
            cur.execute("RESET ROLE")
            cur.execute(f"DROP OWNED BY {unauth}")
        db.rollback()
        admin = _admin_connect()
        with admin.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {unauth}")
        admin.close()
    # the administrative procedure itself works for the admin role
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {ADMIN_ROLE}")
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, _future_freeze(), FINGERPRINT),
        )
    db.commit()
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT status, freeze_ts FROM research.htf_baseline_short_phase2_experiment "
            "WHERE experiment_id = %s",
            (EXP,),
        )
        status, freeze = cur.fetchone()
    assert status == "RUNNING" and freeze is not None


def test_application_role_revoked_on_p2_surface(db):
    admin = _admin_connect()
    roles = _existing_roles(admin)
    created: list[str] = []
    if "trad_bot" not in roles:
        with admin.cursor() as cur:
            cur.execute("CREATE ROLE trad_bot NOLOGIN")
        created.append("trad_bot")
    admin.close()
    try:
        apply_075(db)
        with db.cursor() as cur:
            cur.execute("SET ROLE trad_bot")
        err = _denied(
            "INSERT INTO research.htf_baseline_short_phase2_observation "
            "(experiment_id, source_event_id, episode_id, level_id, symbol, "
            " signal_time, first_touch_time, known_at) "
            "VALUES (%s, 'e:FIRST_CONFIRM', 'e', 'l', 'BTCUSDT', now(), now(), now())",
            db, EXP,
        )
        _assert_insufficient_privilege(err)
        with db.cursor() as cur:
            cur.execute("SET ROLE trad_bot")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            db, EXP, _future_freeze(), FINGERPRINT,
        )
        _assert_insufficient_privilege(err)
    finally:
        with db.cursor() as cur:
            cur.execute("RESET ROLE")
        db.rollback()
        if created:
            admin = _admin_connect()
            for role in created:
                with admin.cursor() as cur:
                    cur.execute(f"DROP ROLE IF EXISTS {role}")
            admin.close()


def test_freeze_registry_isolated_from_direct_writes(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "INSERT INTO research.htf_baseline_short_phase2_freeze_registry "
        "(freeze_ts, experiment_id, protocol_fingerprint) VALUES (%s, %s, %s)",
        db, _future_freeze(), EXP, FINGERPRINT,
    )
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("UPDATE research.htf_baseline_short_phase2_freeze_registry SET note = 'x'", db)
    _assert_insufficient_privilege(err)


# ------------------------------------------- grants on pre-existing objects


def test_existing_research_grants_unchanged_by_075(db):
    admin = _admin_connect()
    try:
        unauth = f"trad_bot_p2_it_reader_{uuid.uuid4().hex[:6]}"
        with admin.cursor() as cur:
            cur.execute(f"CREATE ROLE {unauth} NOLOGIN")
    finally:
        admin.close()
    try:
        # decoy pre-existing research object with its own grants
        with db.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_decoy (id INT PRIMARY KEY)")
            cur.execute(f"GRANT SELECT ON research.p2hardening_decoy TO {unauth}")
        db.commit()

        def snapshot() -> dict:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT table_name, grantee, privilege_type FROM "
                    "information_schema.table_privileges WHERE table_schema = 'research'"
                )
                rows = cur.fetchall()
            return {(r[0], r[1], r[2]) for r in rows}

        before = snapshot()
        apply_075(db)
        after = snapshot()
        decoy_before = {s for s in before if s[0] == "p2hardening_decoy"}
        decoy_after = {s for s in after if s[0] == "p2hardening_decoy"}
        assert decoy_before == decoy_after  # untouched
        # Phase-2 tables: direct DML must be gone for everyone but the owner
        p2_inserts = {
            s for s in after
            if s[0].startswith("htf_baseline_short_phase2") and s[2] == "INSERT"
        }
        assert all(s[1] in ("postgres",) for s in p2_inserts)
    finally:
        db.rollback()
        with db.cursor() as cur:
            cur.execute(f"DROP OWNED BY {unauth}")
        db.commit()
        admin = _admin_connect()
        with admin.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {unauth}")
        admin.close()


# ------------------------------------------------- eligibility and runtime


def test_warmup_and_prefreeze_rejected_for_observer(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(db, experiment_id=EXP, warmup_origin=True, **_event_kwargs())
    assert "P2_WARMUP_EVENT_REJECTED" in str(excinfo.value)
    db.rollback()
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **dict(_event_kwargs(), first_touch_time="2000-01-01T00:00:00+00:00"),
        )
    assert "P2_FIRST_TOUCH_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()
    with db.cursor() as cur:
        cur.execute("RESET ROLE")


def test_psycopg2_password_adaptation_preserves_real_secret():
    """Regression: the real password must reach psycopg2; masking kept.

    Hypothesis confirmation (VPS SCRAM failure): psycopg2's connection-info
    builder converts the _RedactedSecret subclass through ``str()``, so the
    driver would send '<redacted>' and SCRAM auth would fail. The driver
    boundary must receive a plain ``str`` with the real value (``reveal()``),
    while str/repr of the secret container stay redacted. No secret is ever
    placed in an assertion expression (only precomputed booleans).
    """
    secret = _new_test_credential()
    real = secret.reveal()
    # masking preserved wherever the secret could be displayed
    assert str(secret) == "<redacted>"
    assert repr(secret) == "<redacted>"
    assert str(secret) != real
    # hypothesis: psycopg2 stringifies the subclass -> wrong password in DSN
    dsn_bad = psycopg2.extensions.make_dsn(password=secret)
    hypothesis_confirmed = ("<redacted>" in dsn_bad) and (real not in dsn_bad)
    assert hypothesis_confirmed is True
    # fix contract: the plain-str boundary carries the real value
    assert type(real) is str
    dsn_good = psycopg2.extensions.make_dsn(password=real)
    real_reached = (real in dsn_good) and ("<redacted>" not in dsn_good)
    assert real_reached is True


def test_observer_password_verifier_matches_credential(db):
    """Regression: ALTER ROLE must store the REAL password (SCRAM verifier).

    Reads pg_authid (superuser test admin on the isolated test cluster) and
    checks the stored SCRAM-SHA-256 verifier against the credential via
    RFC 5802 math — proof that the password survives adaptation on the SQL
    side as well. Neither the verifier nor the password is ever printed.
    """
    apply_075(db)
    password = _new_test_credential()
    admin = _admin_connect()
    try:
        _install_observer_login(admin, password)
        with admin.cursor() as cur:
            cur.execute(
                "SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                (OBSERVER_ROLE,),
            )
            row = cur.fetchone()
    finally:
        admin.close()
    assert row is not None
    (verifier,) = row
    matched = _scram_verifier_matches(verifier, password.reveal())
    assert matched is True


def test_concurrent_observer_inserts_single_row(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    admin = _admin_connect()
    try:
        password = _new_test_credential()
        _install_observer_login(admin, password)
    finally:
        admin.close()
    plain_password = password.reveal()  # plain str for the driver boundary
    dbname = db.get_dsn_parameters()["dbname"]
    errors: list[str] = []
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, database=dbname,
                user=OBSERVER_ROLE, password=plain_password, connect_timeout=5,
            )
            row = insert_observation_raw(conn, experiment_id=EXP, **_event_kwargs())
            conn.commit()
            conn.close()
            with lock:
                results.append(row)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(str(exc).replace(plain_password, "<redacted>"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len([r for r in results if r is not None]) == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_transaction_rollback_leaves_no_rows(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    insert_observation_raw(db, experiment_id=EXP, **_event_kwargs())
    db.rollback()  # aborted transaction: nothing persists
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 0


def test_pg8000_scram_capable_driver_path_as_observer(db, tmp_path, monkeypatch):
    """Real integration: production credential plumbing + pg8000 SCRAM path.

    A REAL random password is installed on the test observer role (test
    cluster only) and delivered through a closed 0600 temporary file,
    exercising ``load_db_credentials``/``connect_db_from_env`` exactly as
    production does. Works against trust, md5 and SCRAM-SHA-256 servers; the
    observer must connect with minimal privileges: direct DML refused
    (SQLSTATE 42501), the SECURITY DEFINER boundary usable.
    """
    pytest.importorskip("pg8000.dbapi")
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    dbname = db.get_dsn_parameters()["dbname"]
    admin = _admin_connect()
    try:
        password = _new_test_credential()
        _install_observer_login(admin, password)
    finally:
        admin.close()
    pw_file = tmp_path / "observer.pw"
    fd = os.open(str(pw_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password.reveal() + "\n")
    monkeypatch.setenv("HTF_P2_DB_HOST", DB_HOST)
    monkeypatch.setenv("HTF_P2_DB_PORT", str(DB_PORT))
    monkeypatch.setenv("HTF_P2_DB_USER", OBSERVER_ROLE)
    monkeypatch.setenv("HTF_P2_DB_NAME", dbname)
    monkeypatch.setenv("HTF_P2_DB_PASSWORD_FILE", str(pw_file))
    monkeypatch.delenv("HTF_P2_DB_PASSWORD", raising=False)
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        connect_db_from_env,
        load_db_credentials,
    )
    creds = load_db_credentials()
    assert creds["user"] == OBSERVER_ROLE
    assert creds["database"] == dbname
    conn = connect_db_from_env()  # pg8000-first: native SCRAM-SHA-256
    try:
        cur = conn.cursor()  # pg8000 cursors have no context-manager protocol
        try:
            cur.execute("SELECT current_user")
            (who,) = cur.fetchone()
        finally:
            cur.close()
        assert who == OBSERVER_ROLE
        # minimal privileges: direct DML must be refused with SQLSTATE 42501
        denied = None
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO research.htf_baseline_short_phase2_observation "
                "DEFAULT VALUES"
            )
        except Exception as exc:
            denied = exc
        finally:
            cur.close()
        assert denied is not None, "direct INSERT must be denied for observer"
        _assert_insufficient_privilege(denied)
        conn.rollback()
        # and the SECURITY DEFINER boundary must work for the same connection
        row_id = insert_observation_raw(conn, experiment_id=EXP, **_event_kwargs())
        conn.commit()
        assert row_id is not None
    finally:
        conn.close()
