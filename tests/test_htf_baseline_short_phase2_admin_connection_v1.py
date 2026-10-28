"""Issue #205 Phase-2 ADMIN connection routing — unit tests (no DB).

Covers the fail-closed contract of the admin channel
(``app.research.htf_baseline_short_phase2_admin_connection_v1``) with a fully
scripted database boundary (real PostgreSQL is exercised in
``test_htf_baseline_short_phase2_admin_connection_postgres_v1.py``):

* independent credentials: ``HTF_P2_ADMIN_DB_*`` only — the observer
  ``HTF_P2_DB_*`` namespace and the shared ``.env`` can NEVER satisfy the
  admin channel, and observer credentials are refused explicitly;
* no default user/host/port/database, no password-less connection;
* pinned target identity (database / host / port / session user / optional
  cluster system identifier) compared against SERVER-REPORTED values;
* reviewed RBAC invariants before any privilege is assumed: admin NOLOGIN and
  non-superuser, observer keeps insert EXECUTE and NEVER activation EXECUTE
  (RBAC expansion is detected and refused), the login may ``SET ROLE``;
* the effective privilege matrix per operation mode: mint/verify/repair get a
  PostgreSQL READ-ONLY session without the activation privilege; only the
  initial claim gets read-write plus EXECUTE on the sanctioned activation
  function; direct DML is refused in BOTH modes;
* fail-closed teardown: any refusal closes the raw connection and never leaks
  the password.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))

import app.research.htf_baseline_short_phase2_admin_connection_v1 as admin  # noqa: E402

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX-only file-mode semantics")

ADMIN_ROLE = "trad_bot_htf_p2_admin"
OBSERVER_ROLE = "trad_bot_htf_p2_observer"
ACTIVATE_SIG = admin.ACTIVATE_FUNCTION_SIGNATURE
INSERT_SIG = admin.INSERT_FUNCTION_SIGNATURE


# --------------------------------------------------------------- scripted DB
class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        for needle, exc in self.conn.failures.items():
            if needle in sql:
                raise exc
        for needle, value in self.conn.responses.items():
            if needle in sql:
                self._row = value(sql, params) if callable(value) else value
                return
        self._row = None

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, responses=None, failures=None):
        self.responses = dict(responses or {})
        self.failures = dict(failures or {})
        self.executed = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.rollback_error = None

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.executed.append(("COMMIT", None))

    def rollback(self):
        if self.rollback_error is not None:
            raise self.rollback_error
        self.rollbacks += 1
        self.executed.append(("ROLLBACK", None))

    def close(self):
        self.closed = True

    def sql(self):
        return [sql for sql, _ in self.executed]


IDENTITY_ROW = ("p2db", "127.0.0.1", 5544, "oper")
IDENTITY_RESPONSES = {
    "inet_server_addr": IDENTITY_ROW,
    "pg_control_system": ("1234567890",),
}


def role_response(role_rows: dict):
    def pick(sql, params):
        return role_rows.get(params[0])
    return pick


def default_rbac_responses():
    return {
        "FROM pg_roles WHERE rolname": role_response(
            {
                ADMIN_ROLE: (False, False, False, False),   # NOLOGIN, non-super
                OBSERVER_ROLE: (True, False, False, False),  # LOGIN, non-super
            }
        ),
        "has_function_privilege": lambda sql, params: (
            ((params[0], params[1])
             in ((OBSERVER_ROLE, INSERT_SIG), (ADMIN_ROLE, ACTIVATE_SIG)),)
        ),
        "pg_has_role": (True, False),
    }


def context_responses(mode: str, **overrides):
    read_only = "on" if mode == admin.READ_ONLY_MODE else "off"
    base = {
        "SELECT current_user": (ADMIN_ROLE, "oper"),
        "has_table_privilege": (True, False, False, False, False),
        "SHOW transaction_read_only": (read_only,),
    }
    base.update(overrides)
    return base


def _set_admin_env(monkeypatch, **over):
    values = {
        "HOST": "127.0.0.1",
        "PORT": "5544",
        "NAME": "p2db",
        "USER": "oper",
        "PASSWORD": "admin-secret",
    }
    values.update(over)
    for key in (
        "HOST", "PORT", "NAME", "USER", "PASSWORD", "PASSWORD_FILE",
        "SYSTEM_IDENTIFIER",
    ):
        monkeypatch.delenv(f"HTF_P2_ADMIN_DB_{key}", raising=False)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(f"HTF_P2_ADMIN_DB_{key}", str(value))


# =========================================================== credentials loader
def test_missing_admin_user_fails_closed(monkeypatch):
    _set_admin_env(monkeypatch, USER=None)
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.load_admin_db_credentials()
    assert "HTF_P2_ADMIN_DB_USER" in str(excinfo.value)


def test_observer_credentials_never_satisfy_admin_channel(monkeypatch):
    """The production incident shape: ONLY the observer env is present."""
    for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        monkeypatch.delenv(f"HTF_P2_ADMIN_DB_{key}", raising=False)
    monkeypatch.setenv("HTF_P2_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("HTF_P2_DB_PORT", "5544")
    monkeypatch.setenv("HTF_P2_DB_NAME", "trad_bot")
    monkeypatch.setenv("HTF_P2_DB_USER", OBSERVER_ROLE)
    monkeypatch.setenv("HTF_P2_DB_PASSWORD", "observer-secret")
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.load_admin_db_credentials()
    assert "HTF_P2_ADMIN_DB_USER" in str(excinfo.value)
    assert "observer" in str(excinfo.value).lower()


def test_observer_password_cannot_satisfy_admin_channel(monkeypatch):
    _set_admin_env(monkeypatch, PASSWORD=None)
    monkeypatch.setenv("HTF_P2_DB_PASSWORD", "observer-secret")
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.load_admin_db_credentials()
    assert "HTF_P2_ADMIN_DB_PASSWORD" in str(excinfo.value)


@pytest.mark.parametrize("bad_user", [OBSERVER_ROLE, ADMIN_ROLE])
def test_observer_or_admin_role_as_login_user_refused(monkeypatch, bad_user):
    _set_admin_env(monkeypatch, USER=bad_user)
    with pytest.raises(admin.AdminConnectionError):
        admin.load_admin_db_credentials()


@pytest.mark.parametrize("missing", ["HOST", "PORT", "NAME"])
def test_no_default_target_every_pin_required(monkeypatch, missing):
    _set_admin_env(monkeypatch, **{missing: None})
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.load_admin_db_credentials()
    assert f"HTF_P2_ADMIN_DB_{missing}" in str(excinfo.value)


@pytest.mark.parametrize("port", ["", "abc", "0", "70000"])
def test_bad_port_refused(monkeypatch, port):
    _set_admin_env(monkeypatch, PORT=port)
    with pytest.raises(admin.AdminConnectionError):
        admin.load_admin_db_credentials()


def test_missing_password_fails_closed(monkeypatch):
    _set_admin_env(monkeypatch, PASSWORD=None)
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.load_admin_db_credentials()
    assert "PASSWORD" in str(excinfo.value)


def test_password_file_used(monkeypatch, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("file-secret\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    _set_admin_env(monkeypatch, PASSWORD=None, PASSWORD_FILE=str(secret))
    assert admin.load_admin_db_credentials()["password"] == "file-secret"


def test_password_file_empty_refused(monkeypatch, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("\n\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    _set_admin_env(monkeypatch, PASSWORD=None, PASSWORD_FILE=str(secret))
    with pytest.raises(admin.AdminConnectionError, match="no password"):
        admin.load_admin_db_credentials()


def test_password_file_non_regular_refused():
    with pytest.raises(admin.AdminConnectionError, match="regular file"):
        admin.check_admin_password_file_stat(stat.S_IFLNK | 0o777, st_uid=1000, euid=1000)
    with pytest.raises(admin.AdminConnectionError, match="regular file"):
        admin.check_admin_password_file_stat(stat.S_IFDIR | 0o700, st_uid=1000, euid=1000)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o777, 0o604, 0o666])
def test_password_file_world_accessible_refused(mode):
    with pytest.raises(admin.AdminConnectionError, match="group/world"):
        admin.check_admin_password_file_stat(stat.S_IFREG | mode, st_uid=1000, euid=1000)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_password_file_private_modes_accepted(mode):
    admin.check_admin_password_file_stat(stat.S_IFREG | mode, st_uid=1000, euid=1000)


@POSIX_ONLY
def test_password_file_wrong_owner_refused():
    with pytest.raises(admin.AdminConnectionError, match="owned"):
        admin.check_admin_password_file_stat(stat.S_IFREG | 0o600, st_uid=1001, euid=1000)
    admin.check_admin_password_file_stat(stat.S_IFREG | 0o600, st_uid=0, euid=1000)


@POSIX_ONLY
def test_password_file_symlink_refused(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.write_text("good-secret\n", encoding="utf-8")
    os.chmod(real, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    _set_admin_env(monkeypatch, PASSWORD=None, PASSWORD_FILE=str(link))
    with pytest.raises(admin.AdminConnectionError, match="symlink"):
        admin.load_admin_db_credentials()


def test_password_never_in_exception_or_log(monkeypatch, caplog):
    _set_admin_env(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(admin.AdminConnectionError) as excinfo:
            # wrong server address -> identity pin refusal
            admin.verify_admin_target_identity(
                _FakeConn({"inet_server_addr": ("p2db", "10.0.0.9", 5544, "oper")}),
                admin.load_admin_db_credentials(),
            )
    assert "admin-secret" not in str(excinfo.value)
    assert "admin-secret" not in caplog.text


def test_password_reaches_driver_but_never_logs(monkeypatch, caplog):
    _set_admin_env(monkeypatch)
    captured = {}

    def fake_connect(**kw):
        captured.update(kw)
        raise RuntimeError("stop before real connection")

    import pg8000.dbapi as dbapi

    monkeypatch.setattr(dbapi, "connect", fake_connect)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(admin.AdminConnectionError):
            admin.connect_admin_db(admin.READ_ONLY_MODE)
    assert captured.get("password") == "admin-secret"
    assert "admin-secret" not in caplog.text


def test_admin_loader_reads_only_admin_env_namespace():
    source = Path(admin.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"os\.environ\.get\(([^,]+),", source)
    assert calls, "expected explicit env reads"
    for expr in calls:
        assert "ADMIN_DB_ENV_PREFIX" in expr, f"non-admin env read: {expr}"
    suffixes = set(re.findall(r'ADMIN_DB_ENV_PREFIX \+ "([A-Z_]+)"', source))
    assert suffixes == {
        "USER", "HOST", "PORT", "NAME", "PASSWORD", "PASSWORD_FILE",
        "SYSTEM_IDENTIFIER",
    }
    # the observer namespace is documented as NEVER consulted; no code path
    # reads it (every os.environ.get call above is admin-prefixed)
    assert not re.search(r"os\.environ\.get\(\s*\"HTF_P2_DB_", source)


def test_freeze_tool_uses_only_the_admin_channel():
    source = (
        ROOT / "tools" / "research" / "htf_baseline_short_phase2_freeze_record_v1.py"
    ).read_text(encoding="utf-8")
    assert "connect_db_from_env" not in source
    assert "os.environ" not in source  # the tool reads no environment at all
    assert "connect_admin_db" in source


def test_admin_module_keeps_point_b_boundary_out():
    source = Path(admin.__file__).read_text(encoding="utf-8")
    assert "2026-10-06T14:00:00Z" not in source


def test_password_file_stat_parity_with_observer_channel():
    """The duplicated hardening must behave identically to the observer one."""
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        check_password_file_stat as observer_check,
    )

    cases = [
        (stat.S_IFREG | 0o600, True),
        (stat.S_IFREG | 0o400, True),
        (stat.S_IFREG | 0o644, False),
        (stat.S_IFREG | 0o666, False),
        (stat.S_IFLNK | 0o777, False),
        (stat.S_IFDIR | 0o700, False),
    ]
    for mode, accepted in cases:
        results = []
        for check in (observer_check, admin.check_admin_password_file_stat):
            try:
                check(mode, st_uid=1000, euid=1000)
                results.append(True)
            except Exception:
                results.append(False)
        assert results == [accepted, accepted], (oct(mode), results)


# =================================================================== target pin
def test_target_identity_accepted(monkeypatch):
    _set_admin_env(monkeypatch)
    creds = admin.load_admin_db_credentials()
    conn = _FakeConn(IDENTITY_RESPONSES)
    admin.verify_admin_target_identity(conn, creds)


@pytest.mark.parametrize(
    "row,needle",
    [
        (("other_db", "127.0.0.1", 5544, "oper"), "database"),
        (("p2db", "127.0.0.1", 5432, "oper"), "port"),
        (("p2db", "10.9.9.9", 5544, "oper"), "server address"),
        (("p2db", None, 5544, "oper"), "server address"),
        (("p2db", "127.0.0.1", 5544, "someone_else"), "session_user"),
    ],
)
def test_target_identity_mismatch_refused(row, needle):
    creds = {
        "host": "127.0.0.1", "port": 5544, "database": "p2db",
        "user": "oper", "password": "x", "system_identifier": None,
    }
    conn = _FakeConn({"inet_server_addr": row})
    with pytest.raises(admin.AdminConnectionError) as excinfo:
        admin.verify_admin_target_identity(conn, creds)
    assert needle in str(excinfo.value)


def test_loopback_spellings_are_equivalent():
    assert admin.server_host_matches("127.0.0.1", "::1")
    assert admin.server_host_matches("localhost", "127.0.0.1")
    assert admin.server_host_matches("127.0.0.1", "127.0.0.1")
    assert not admin.server_host_matches("127.0.0.1", "10.0.0.1")
    assert not admin.server_host_matches("127.0.0.1", None)


def test_system_identifier_pin_verified():
    creds = {
        "host": "127.0.0.1", "port": 5544, "database": "p2db",
        "user": "oper", "password": "x", "system_identifier": "1234567890",
    }
    admin.verify_admin_target_identity(_FakeConn(IDENTITY_RESPONSES), creds)
    wrong = dict(creds, system_identifier="999")
    with pytest.raises(admin.AdminConnectionError, match="system_identifier"):
        admin.verify_admin_target_identity(_FakeConn(IDENTITY_RESPONSES), wrong)


def test_system_identifier_unreadable_fails_closed():
    creds = {
        "host": "127.0.0.1", "port": 5544, "database": "p2db",
        "user": "oper", "password": "x", "system_identifier": "1234567890",
    }
    conn = _FakeConn(
        {"inet_server_addr": IDENTITY_ROW},
        failures={"pg_control_system": RuntimeError("permission denied")},
    )
    with pytest.raises(admin.AdminConnectionError, match="cannot be read"):
        admin.verify_admin_target_identity(conn, creds)


# ============================================================== RBAC invariants
def _rbac_conn(role_rows, fn_grants=None):
    responses = default_rbac_responses()
    responses["FROM pg_roles WHERE rolname"] = role_response(role_rows)
    if fn_grants is not None:
        responses["has_function_privilege"] = fn_grants
    return _FakeConn(responses)


def _creds():
    return {"user": "oper"}


def test_rbac_invariants_accepted():
    admin.verify_admin_rbac_invariants(_rbac_conn({
        ADMIN_ROLE: (False, False, False, False),
        OBSERVER_ROLE: (True, False, False, False),
    }), _creds())


def test_admin_role_missing_refused():
    with pytest.raises(admin.AdminConnectionError, match="RBAC surface missing"):
        admin.verify_admin_rbac_invariants(_rbac_conn({
            OBSERVER_ROLE: (True, False, False, False),
        }), _creds())


def test_admin_role_converted_to_login_refused():
    with pytest.raises(admin.AdminConnectionError, match="NOLOGIN"):
        admin.verify_admin_rbac_invariants(_rbac_conn({
            ADMIN_ROLE: (True, False, False, False),
            OBSERVER_ROLE: (True, False, False, False),
        }), _creds())


def test_admin_role_superuser_refused():
    with pytest.raises(admin.AdminConnectionError, match="unexpected role attributes"):
        admin.verify_admin_rbac_invariants(_rbac_conn({
            ADMIN_ROLE: (False, True, False, False),
            OBSERVER_ROLE: (True, False, False, False),
        }), _creds())


def test_observer_rbac_expansion_refused():
    """Observer granted EXECUTE on activate() = RBAC expansion -> refuse all."""
    def grants(sql, params):
        return (params[1] in (INSERT_SIG, ACTIVATE_SIG),)
    with pytest.raises(admin.AdminConnectionError, match="EXPANDED"):
        admin.verify_admin_rbac_invariants(_rbac_conn({
            ADMIN_ROLE: (False, False, False, False),
            OBSERVER_ROLE: (True, False, False, False),
        }, fn_grants=grants), _creds())


def test_observer_insert_execute_missing_refused():
    def grants(sql, params):
        return (False,)
    with pytest.raises(admin.AdminConnectionError, match="surface drift"):
        admin.verify_admin_rbac_invariants(_rbac_conn({
            ADMIN_ROLE: (False, False, False, False),
            OBSERVER_ROLE: (True, False, False, False),
        }, fn_grants=grants), _creds())


def test_login_without_membership_refused():
    responses = default_rbac_responses()
    responses["pg_has_role"] = (False, False)  # not a member, not superuser
    conn = _FakeConn(responses)
    with pytest.raises(admin.AdminConnectionError, match="SET ROLE"):
        admin.verify_admin_rbac_invariants(conn, _creds())


def test_superuser_login_may_set_role_without_membership():
    responses = default_rbac_responses()
    responses["pg_has_role"] = (False, True)  # member=no, superuser=yes
    conn = _FakeConn(responses)
    admin.verify_admin_rbac_invariants(conn, _creds())


# ========================================================= effective privileges
def _mode_conn(mode, **overrides):
    return _FakeConn({**default_rbac_responses(), **context_responses(mode, **overrides)})


@pytest.mark.parametrize("mode", [admin.READ_ONLY_MODE, admin.ACTIVATE_MODE])
def test_effective_context_accepted(mode):
    conn = _mode_conn(mode)
    admin.verify_effective_admin_context(conn, mode=mode)


def test_unknown_mode_refused():
    with pytest.raises(admin.AdminConnectionError, match="unknown admin connection mode"):
        admin.verify_effective_admin_context(_mode_conn(admin.READ_ONLY_MODE), mode="rw")


def test_wrong_current_user_refused():
    conn = _mode_conn(admin.READ_ONLY_MODE, **{"SELECT current_user": ("postgres",)})
    with pytest.raises(admin.AdminConnectionError, match="SET ROLE"):
        admin.verify_effective_admin_context(conn, mode=admin.READ_ONLY_MODE)


def test_missing_select_refused():
    conn = _mode_conn(
        admin.READ_ONLY_MODE,
        **{"has_table_privilege": (False, False, False, False, False)},
    )
    with pytest.raises(admin.AdminConnectionError, match="lacks SELECT"):
        admin.verify_effective_admin_context(conn, mode=admin.READ_ONLY_MODE)


@pytest.mark.parametrize("row", [(True, True, False, False, False), (True, False, True, False, False),
                                 (True, False, False, True, False), (True, False, False, False, True)])
def test_direct_dml_shortcut_refused(row):
    conn = _mode_conn(admin.ACTIVATE_MODE, **{"has_table_privilege": row})
    with pytest.raises(admin.AdminConnectionError, match="direct DML"):
        admin.verify_effective_admin_context(conn, mode=admin.ACTIVATE_MODE)


def test_activate_mode_requires_activation_execute():
    def grants(sql, params):
        return (False,)  # no EXECUTE for the effective-context form either
    conn = _FakeConn({**context_responses(admin.ACTIVATE_MODE), "has_function_privilege": grants})
    with pytest.raises(admin.AdminConnectionError, match="activation"):
        admin.verify_effective_admin_context(conn, mode=admin.ACTIVATE_MODE)


def test_read_only_mode_does_not_require_activation_execute():
    """mint/verify/repair must NOT need the activation privilege (issue #205)."""
    def grants(sql, params):
        return (False,)
    conn = _FakeConn({**context_responses(admin.READ_ONLY_MODE), "has_function_privilege": grants})
    admin.verify_effective_admin_context(conn, mode=admin.READ_ONLY_MODE)


@pytest.mark.parametrize(
    "mode,read_only_value",
    [
        (admin.READ_ONLY_MODE, "off"),
        (admin.ACTIVATE_MODE, "on"),
    ],
)
def test_transaction_write_mode_pin_enforced(mode, read_only_value):
    conn = _mode_conn(mode, **{"SHOW transaction_read_only": (read_only_value,)})
    with pytest.raises(admin.AdminConnectionError, match="transaction_read_only"):
        admin.verify_effective_admin_context(conn, mode=mode)


# ============================================================ connect orchestration
def _scripted(monkeypatch, responses=None, failures=None):
    conn = _FakeConn(
        {**default_rbac_responses(), **IDENTITY_RESPONSES, **(responses or {})},
        failures,
    )
    opened = []

    def open_raw(creds, driver=None):
        opened.append((dict(creds), driver))
        return conn

    monkeypatch.setattr(admin, "_open_raw_connection", open_raw)
    return conn, opened


def test_connect_read_only_pins_read_only_transaction(monkeypatch):
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.READ_ONLY_MODE))
    out = admin.connect_admin_db(admin.READ_ONLY_MODE)
    assert out is conn
    statements = [sql for sql in conn.sql() if not sql.startswith("SELECT")
                  and not sql.startswith("SHOW")]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert "SET default_transaction_read_only = on" in statements
    assert "SET ROLE trad_bot_htf_p2_admin" in statements
    assert "SET TRANSACTION READ WRITE" not in statements


def test_connect_activate_pins_read_write_transaction(monkeypatch):
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.ACTIVATE_MODE))
    admin.connect_admin_db(admin.ACTIVATE_MODE)
    statements = [sql for sql in conn.sql() if not sql.startswith("SELECT")
                  and not sql.startswith("SHOW")]
    assert statements[0] == "SET TRANSACTION READ WRITE"
    assert "SET default_transaction_read_only = off" in statements
    assert "SET ROLE trad_bot_htf_p2_admin" in statements
    assert "SET TRANSACTION READ ONLY" not in statements


def test_connect_unknown_mode_refused_before_any_connection(monkeypatch):
    _set_admin_env(monkeypatch)
    conn, opened = _scripted(monkeypatch)
    with pytest.raises(admin.AdminConnectionError, match="unknown admin connection mode"):
        admin.connect_admin_db("read_write")
    assert opened == []
    assert conn.executed == []


def test_connect_missing_credentials_never_opens_connection(monkeypatch):
    _set_admin_env(monkeypatch, USER=None)
    conn, opened = _scripted(monkeypatch)
    with pytest.raises(admin.AdminConnectionError):
        admin.connect_admin_db(admin.ACTIVATE_MODE)
    assert opened == []


def test_connect_refusal_closes_raw_connection(monkeypatch):
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(
        monkeypatch,
        context_responses(admin.READ_ONLY_MODE, **{"SELECT current_user": ("postgres",)}),
    )
    with pytest.raises(admin.AdminConnectionError):
        admin.connect_admin_db(admin.READ_ONLY_MODE)
    assert conn.closed


def test_connect_unexpected_error_fails_closed_and_closes(monkeypatch):
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(
        monkeypatch,
        failures={"inet_server_addr": RuntimeError("server gone")},
    )
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.connect_admin_db(admin.READ_ONLY_MODE)
    assert conn.closed


def test_connect_set_role_before_privilege_checks(monkeypatch):
    """Order matters: identity + RBAC first, THEN SET ROLE, THEN the matrix."""
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.ACTIVATE_MODE))
    admin.connect_admin_db(admin.ACTIVATE_MODE)
    sqls = conn.sql()
    set_role_at = sqls.index("SET ROLE trad_bot_htf_p2_admin")
    assert any("inet_server_addr" in sql for sql in sqls[:set_role_at])
    assert any("pg_has_role" in sql for sql in sqls[:set_role_at])
    assert any("has_table_privilege" in sql for sql in sqls[set_role_at:])


def test_close_admin_db_defensive():
    class _Conn:
        closed = False

        def close(self):
            self.closed = True

    conn = _Conn()
    admin.close_admin_db(conn)
    assert conn.closed
    admin.close_admin_db(None)


# ========================================== issue #205 rollback-role safety
def test_connect_commits_the_set_role_context(monkeypatch):
    """SET ROLE / session write-mode are COMMITTED by connect_admin_db.

    ``SET ROLE`` is transactional in PostgreSQL: left uncommitted it is
    reverted by the first ``ROLLBACK``, silently restoring the operator
    login's original (superuser) privileges — the discovered issue #205
    defect. The context must therefore be committed in its own transaction.
    """
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.ACTIVATE_MODE))
    admin.connect_admin_db(admin.ACTIVATE_MODE)
    sqls = conn.sql()
    assert conn.commits == 1
    assert conn.rollbacks == 1  # preparatory checks are ROLLED BACK, never committed
    assert sqls.index("ROLLBACK") < sqls.index("SET ROLE trad_bot_htf_p2_admin")
    assert sqls.index("SET ROLE trad_bot_htf_p2_admin") < sqls.index("COMMIT")
    first_matrix_check = next(
        i for i, sql in enumerate(sqls) if "has_table_privilege" in sql
    )
    assert sqls.index("COMMIT") < first_matrix_check  # verified AFTER commit


def test_connect_prep_transaction_cannot_commit_business_changes(monkeypatch):
    """The preparatory transaction is rolled back; the committed setup
    transaction contains ONLY the session-context SET statements (issue #205:
    no business changes can ever be committed by the setup)."""
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.ACTIVATE_MODE))
    admin.connect_admin_db(admin.ACTIVATE_MODE)
    sqls = conn.sql()
    rollback_at = sqls.index("ROLLBACK")
    commit_at = sqls.index("COMMIT")
    assert rollback_at < commit_at
    setup = sqls[rollback_at + 1:commit_at]
    assert setup == [
        "SET ROLE trad_bot_htf_p2_admin",
        "SET default_transaction_read_only = off",
    ]


def test_connect_records_session_identity_for_recovery(monkeypatch):
    """connect_admin_db records the operator login used for SET ROLE."""
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.ACTIVATE_MODE))
    admin.connect_admin_db(admin.ACTIVATE_MODE)
    assert conn.admin_session_user == "oper"


def test_connect_read_only_mode_commits_the_read_only_pin(monkeypatch):
    """The mint/verify READ-ONLY pin is session-persistent after the commit."""
    _set_admin_env(monkeypatch)
    conn, _ = _scripted(monkeypatch, context_responses(admin.READ_ONLY_MODE))
    admin.connect_admin_db(admin.READ_ONLY_MODE)
    sqls = conn.sql()
    assert conn.commits == 1
    assert sqls.index("SET default_transaction_read_only = on") < sqls.index("COMMIT")


def test_recover_admin_db_rolls_back_and_reverifies():
    """Permitted transaction recovery keeps the admin context and re-checks it."""
    conn = _mode_conn(admin.ACTIVATE_MODE)
    admin.recover_admin_db(conn, mode=admin.ACTIVATE_MODE)
    assert conn.rollbacks == 1
    assert not conn.closed
    sqls = conn.sql()
    assert sqls.index("ROLLBACK") < next(
        i for i, sql in enumerate(sqls) if "SELECT current_user" in sql
    )


def test_recover_admin_db_lost_context_fails_closed_and_closes():
    """A lost admin context must never pass silently: refuse + close."""
    conn = _mode_conn(admin.ACTIVATE_MODE, **{"SELECT current_user": ("postgres",)})
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.recover_admin_db(conn, mode=admin.ACTIVATE_MODE)
    assert conn.closed


def test_recover_admin_db_lost_read_only_pin_fails_closed():
    """The mint/verify READ-ONLY pin is part of the recovered context."""
    conn = _mode_conn(admin.READ_ONLY_MODE, **{"SHOW transaction_read_only": ("off",)})
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.recover_admin_db(conn, mode=admin.READ_ONLY_MODE)
    assert conn.closed


def test_recover_admin_db_rollback_error_fails_closed():
    """Even the rollback itself failing closes the connection."""
    conn = _mode_conn(admin.ACTIVATE_MODE)
    conn.rollback_error = RuntimeError("connection gone")
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.recover_admin_db(conn, mode=admin.ACTIVATE_MODE)
    assert conn.closed


def test_recover_admin_db_invalid_inputs_refused():
    with pytest.raises(admin.AdminConnectionError, match="unknown admin connection mode"):
        admin.recover_admin_db(_mode_conn(admin.ACTIVATE_MODE), mode="rw")
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.recover_admin_db(None, mode=admin.ACTIVATE_MODE)


def test_verify_effective_context_checks_session_user_identity():
    """session_user must be an operator login — never the NOLOGIN admin role."""
    conn = _mode_conn(
        admin.ACTIVATE_MODE, **{"SELECT current_user": (ADMIN_ROLE, ADMIN_ROLE)}
    )
    with pytest.raises(admin.AdminConnectionError, match="session_user"):
        admin.verify_effective_admin_context(conn, mode=admin.ACTIVATE_MODE)


def test_verify_effective_context_requires_recorded_login():
    """A session_user differing from the recorded login fails closed."""
    conn = _mode_conn(
        admin.ACTIVATE_MODE, **{"SELECT current_user": (ADMIN_ROLE, "someone_else")}
    )
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.verify_effective_admin_context(
            conn, mode=admin.ACTIVATE_MODE, expected_session_user="oper"
        )


def test_recover_admin_db_reverifies_recorded_session_identity():
    """Transaction recovery re-checks the login identity recorded at connect."""
    conn = _mode_conn(admin.ACTIVATE_MODE)
    conn.admin_session_user = "oper"
    admin.recover_admin_db(conn, mode=admin.ACTIVATE_MODE)
    assert not conn.closed

    conn = _mode_conn(
        admin.ACTIVATE_MODE, **{"SELECT current_user": (ADMIN_ROLE, "someone_else")}
    )
    conn.admin_session_user = "oper"
    with pytest.raises(admin.AdminConnectionError, match="fail closed"):
        admin.recover_admin_db(conn, mode=admin.ACTIVATE_MODE)
    assert conn.closed
