"""Issue #176 Phase-2 CP2: hardened database connection credentials tests.

Pure unit tests (no server required): the connector must fail closed on
missing/unsafe credentials, must never default to the postgres superuser or
shared application roles, must support a password file, and must never leak
the password into logs.
"""
from __future__ import annotations

import logging

import pytest

from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    DbCredentialsError,
    close_db_connection,
    connect_db_from_env,
    load_db_credentials,
)

FORBIDDEN = ("postgres", "trad_bot", "analytics_runner", "analytics_agent")


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
    _set_env(monkeypatch, HTF_P2_DB_USER="trad_bot_htf_p2_observer",
             HTF_P2_DB_NAME="trad_bot",
             HTF_P2_DB_PASSWORD_FILE=str(empty))
    with pytest.raises(DbCredentialsError, match="no password"):
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


def test_env_only_htf_p2_names_consulted(monkeypatch):
    """Scanner-style variables must never satisfy the credential loader."""
    _set_env(monkeypatch)
    monkeypatch.setenv("DB_USER", "trad_bot")
    monkeypatch.setenv("DB_PASSWORD", "scanner-pw")
    monkeypatch.setenv("PGPASSWORD", "scanner-pw")
    with pytest.raises(DbCredentialsError):
        load_db_credentials()
