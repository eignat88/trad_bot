"""Issue #176 Phase-2 CP2: hardened database connection credentials tests.

Pure unit tests (no server required): the connector must fail closed on
missing/unsafe credentials, must never default to the postgres superuser or
shared application roles, must support a password file, and must never leak
the password into logs.
"""
from __future__ import annotations

import logging
import os
import stat

import pytest

from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    DbCredentialsError,
    check_password_file_stat,
    close_db_connection,
    connect_db_from_env,
    load_db_credentials,
    main,
)

FORBIDDEN = ("postgres", "trad_bot", "analytics_runner", "analytics_agent")
POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")


def _set_env(monkeypatch, **kv):
    for key in (
        "HTF_P2_DB_HOST", "HTF_P2_DB_PORT", "HTF_P2_DB_USER",
        "HTF_P2_DB_NAME", "HTF_P2_DB_PASSWORD", "HTF_P2_DB_PASSWORD_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in kv.items():
        monkeypatch.setenv(key, value)


def test_missing_user_fails_closed(monkeypatch):
    _set_env(monkeypatch, HTF_P2_DB_NAME="trad_bot", HTF_P2_DB_PASSWORD="x")
    with pytest.raises(DbCredentialsError, match="HTF_P2_DB_USER"):
        load_db_credentials()


@pytest.mark.parametrize("user", FORBIDDEN)
def test_shared_or_admin_user_rejected(monkeypatch, user):
    _set_env(monkeypatch, HTF_P2_DB_USER=user, HTF_P2_DB_NAME="trad_bot",
             HTF_P2_DB_PASSWORD="x")
    with pytest.raises(DbCredentialsError, match="dedicated"):
        load_db_credentials()


def test_no_default_superuser_connection(monkeypatch):
    """Without credentials the connector must never fall back to postgres."""
    _set_env(monkeypatch)
    with pytest.raises(DbCredentialsError):
        connect_db_from_env()
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer")
    with pytest.raises(DbCredentialsError):
        connect_db_from_env()  # missing password + database


def test_missing_database_name_fails_closed(monkeypatch):
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_PASSWORD="x")
    with pytest.raises(DbCredentialsError, match="HTF_P2_DB_NAME"):
        load_db_credentials()


def test_missing_password_fails_closed(monkeypatch):
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot")
    with pytest.raises(DbCredentialsError, match="PASSWORD"):
        load_db_credentials()


def test_password_file_used_and_stripped(monkeypatch, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("  scrypt-hashed-value\nsecond-line\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot",
             HTF_P2_DB_PASSWORD_FILE=str(secret))
    creds = load_db_credentials()
    assert creds["password"] == "scrypt-hashed-value"
    assert creds["user"] == "trad_bot_htf_p2_observer"


def test_password_file_missing_or_empty_fails_closed(monkeypatch, tmp_path):
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot",
             HTF_P2_DB_PASSWORD_FILE=str(tmp_path / "absent"))
    with pytest.raises(DbCredentialsError, match="unreadable"):
        load_db_credentials()
    empty = tmp_path / "empty"
    empty.write_text("\n\n", encoding="utf-8")
    os.chmod(empty, 0o600)
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot",
             HTF_P2_DB_PASSWORD_FILE=str(empty))
    with pytest.raises(DbCredentialsError, match="no password"):
        load_db_credentials()


# --------------------------------------------------- F1: password file modes


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_password_file_mode_accepted(mode):
    check_password_file_stat(stat.S_IFREG | mode, st_uid=1000, euid=1000)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o777, 0o604, 0o666])
def test_password_file_mode_rejected(mode):
    with pytest.raises(DbCredentialsError, match="group/world"):
        check_password_file_stat(stat.S_IFREG | mode, st_uid=1000, euid=1000)


def test_password_file_non_regular_rejected():
    with pytest.raises(DbCredentialsError, match="regular file"):
        check_password_file_stat(stat.S_IFLNK | 0o777, st_uid=1000, euid=1000)
    with pytest.raises(DbCredentialsError, match="regular file"):
        check_password_file_stat(stat.S_IFDIR | 0o700, st_uid=1000, euid=1000)


@POSIX_ONLY
def test_password_file_wrong_owner_rejected():
    with pytest.raises(DbCredentialsError, match="owned"):
        check_password_file_stat(stat.S_IFREG | 0o600, st_uid=1001, euid=1000)
    # root-owned secrets are acceptable for the service user
    check_password_file_stat(stat.S_IFREG | 0o600, st_uid=0, euid=1000)


@POSIX_ONLY
@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_password_file_mode_0600_0400_accepted_e2e(monkeypatch, tmp_path, mode):
    secret = tmp_path / "pw"
    secret.write_text("good-secret\n", encoding="utf-8")
    os.chmod(secret, mode)
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot", HTF_P2_DB_PASSWORD_FILE=str(secret))
    assert load_db_credentials()["password"] == "good-secret"


@POSIX_ONLY
@pytest.mark.parametrize("mode", [0o644, 0o660, 0o777])
def test_password_file_mode_0644_0660_0777_rejected_e2e(monkeypatch, tmp_path, mode):
    secret = tmp_path / "pw"
    secret.write_text("must-not-leak\n", encoding="utf-8")
    os.chmod(secret, mode)
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot", HTF_P2_DB_PASSWORD_FILE=str(secret))
    with pytest.raises(DbCredentialsError) as excinfo:
        load_db_credentials()
    assert "must-not-leak" not in str(excinfo.value)  # never leak the secret


@POSIX_ONLY
def test_password_file_symlink_rejected(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.write_text("good-secret\n", encoding="utf-8")
    os.chmod(real, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot", HTF_P2_DB_PASSWORD_FILE=str(link))
    with pytest.raises(DbCredentialsError, match="symlink"):
        load_db_credentials()


def test_password_never_logged(monkeypatch, caplog):
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot", HTF_P2_DB_PASSWORD="super-secret-pw")
    captured = {}

    def fake_connect(**kw):
        captured.update(kw)
        raise RuntimeError("stop before real connection")

    import pg8000.dbapi as dbapi

    monkeypatch.setattr(dbapi, "connect", fake_connect)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            connect_db_from_env()
    # password reaches the driver (SCRAM handshake material) ...
    assert captured.get("password") == "super-secret-pw"
    # ... but never appears in any log record
    assert "super-secret-pw" not in caplog.text


def test_close_db_connection_defensive():
    class _Conn:
        closed = False

        def close(self):
            self.closed = True

    conn = _Conn()
    close_db_connection(conn)
    assert conn.closed
    close_db_connection(None)  # no-op


# ------------------------------- F2: connection closed exactly once in main()


class _FakeConn:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class _StubRunner:
    run_loop_result = 0
    run_loop_raises = None

    def __init__(self, *args, **kwargs):
        pass

    def install_signal_handlers(self):
        pass

    def run_loop(self):
        if _StubRunner.run_loop_raises is not None:
            raise _StubRunner.run_loop_raises
        return _StubRunner.run_loop_result


def _wire_main(monkeypatch, tmp_path, gate_behavior="accept", connect_raises=None):
    import app.research.htf_baseline_short_phase2_observer_runner_v1 as mod

    conn = _FakeConn()
    calls = {"connect": 0}

    def fake_connect():
        calls["connect"] += 1
        if connect_raises is not None:
            raise connect_raises
        return conn

    def fake_gate(db_conn, config, *, activation, protocol_freeze_ts=None):
        if gate_behavior == "refuse":
            raise mod.NotRunnableError("gate refused")
        return object()

    monkeypatch.setattr(mod, "load_activation_config", lambda p: {"activation": True})
    monkeypatch.setattr(mod, "connect_db_from_env", fake_connect)
    monkeypatch.setattr(mod, "require_runnable", fake_gate)
    monkeypatch.setattr(mod, "Phase2ObserverRunner", _StubRunner)
    _StubRunner.run_loop_result = 0
    _StubRunner.run_loop_raises = None
    return conn, calls


def _argv(tmp_path, *extra):
    return ["--data-dir", str(tmp_path), "--use-registration", *extra]


def test_main_gate_refused_closes_connection_once(monkeypatch, tmp_path):
    conn, calls = _wire_main(monkeypatch, tmp_path, gate_behavior="refuse")
    cfg = tmp_path / "act.json"
    cfg.write_text("{}", encoding="utf-8")
    code = main(_argv(tmp_path, "--activation-config", str(cfg)))
    assert code == 3  # EXIT_NOT_RUNNABLE preserved
    assert calls["connect"] == 1
    assert conn.close_calls == 1


def test_main_gate_accepted_closes_connection_once(monkeypatch, tmp_path):
    conn, calls = _wire_main(monkeypatch, tmp_path, gate_behavior="accept")
    cfg = tmp_path / "act.json"
    cfg.write_text("{}", encoding="utf-8")
    code = main(_argv(tmp_path, "--activation-config", str(cfg)))
    assert code == 0
    assert conn.close_calls == 1


def test_main_run_loop_exception_still_closes(monkeypatch, tmp_path):
    conn, calls = _wire_main(monkeypatch, tmp_path, gate_behavior="accept")
    _StubRunner.run_loop_raises = RuntimeError("boom")
    cfg = tmp_path / "act.json"
    cfg.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="boom"):
        main(_argv(tmp_path, "--activation-config", str(cfg)))
    assert conn.close_calls == 1  # exactly once despite the exception


def test_main_connect_failure_never_closes_unopened(monkeypatch, tmp_path):
    conn, calls = _wire_main(
        monkeypatch, tmp_path, connect_raises=DbCredentialsError("no creds")
    )
    cfg = tmp_path / "act.json"
    cfg.write_text("{}", encoding="utf-8")
    code = main(_argv(tmp_path, "--activation-config", str(cfg)))
    assert code == 3
    assert calls["connect"] == 1
    assert conn.close_calls == 0  # nothing was opened


def test_main_missing_activation_config_no_connect(monkeypatch, tmp_path):
    conn, calls = _wire_main(monkeypatch, tmp_path)
    code = main(_argv(tmp_path))  # no --activation-config
    assert code == 3
    assert calls["connect"] == 0
    assert conn.close_calls == 0


def test_env_only_htf_p2_names_consulted(monkeypatch):
    """Scanner-style variables must never satisfy the credential loader."""
    _set_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "trad_bot")
    monkeypatch.setenv("DB_PASSWORD", "scanner-pw")
    monkeypatch.setenv("PGPASSWORD", "scanner-pw")
    with pytest.raises(DbCredentialsError):
        load_db_credentials()
