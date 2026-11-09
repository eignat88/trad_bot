"""Offline, no-DB regression tests for the ME inverse SHORT preactivation audit."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.me_inverse_short_v1_preflight import (
    PreflightRefused, audit_cursor, read_service_env, run_preflight,
)

ENV = (
    "ME_INVERSE_SHORT_V1_ENABLED=0\n"
    "DB_HOST=127.0.0.1\nDB_PORT=5432\nDB_NAME=trad_bot\n"
    "DB_USER=synthetic\nDB_PASSWORD=\"fake-test-only-password\"\n"
)


class Cursor:
    def __init__(self, activation=None, counts=(0, 0, 0)):
        self.activation = activation or (
            "ME_LONG_V2_INVERSE_SHORT_V1", "READY_TO_START", None, None
        )
        self.counts = counts
        self.queries = []
        self.closed = False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if sql.startswith("SELECT experiment_id"):
            self.next_row = self.activation
        elif sql.startswith("SELECT (SELECT count"):
            self.next_row = self.counts
        elif sql == "BEGIN TRANSACTION READ ONLY":
            self.next_row = None
        else:
            raise AssertionError("Unexpected statement")

    def fetchone(self):
        return self.next_row

    def close(self):
        self.closed = True


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def env_path(tmp_path, content=ENV):
    path = tmp_path / "runtime.env"
    path.write_text(content)
    return path


def test_read_only_preflight_success(tmp_path):
    cursor = Cursor()
    conn = Conn(cursor)
    def connect(**kwargs):
        assert kwargs["database"] == "trad_bot"
        assert kwargs["password"] == "fake-test-only-password"
        return conn

    result = run_preflight(env_path(tmp_path), connect=connect)
    assert result["result"] == "PREACTIVATION_DB_PASS"
    assert result["events"] == 0 and result["positions"] == 0
    assert result["runtime_flag"] == "OFF"
    assert result["scanner_capture_effective_flag"] == "NOT_VERIFIED"
    assert result["activation_authorized"] is False
    assert conn.autocommit is True
    assert cursor.queries[0][0] == "BEGIN TRANSACTION READ ONLY"
    assert all(
        sql.startswith("SELECT ") or sql == "BEGIN TRANSACTION READ ONLY"
        for sql, _ in cursor.queries
    )
    assert conn.rollbacks == 1 and conn.closed and cursor.closed
    assert "fake-test-only-password" not in repr(result)


@pytest.mark.parametrize("replace", [
    "ME_INVERSE_SHORT_V1_ENABLED=1",
    "DB_NAME=another_database",
    "DB_PORT=not-a-port",
    "DB_PASSWORD=",
])
def test_bad_environment_fails_before_connect(tmp_path, replace):
    key = replace.split("=", 1)[0]
    lines = [replace if x.startswith(key + "=") else x
             for x in ENV.splitlines()]
    with pytest.raises(PreflightRefused):
        run_preflight(env_path(tmp_path, "\n".join(lines)), connect=lambda **kw: (
            pytest.fail("connection must not occur")
        ))


def test_missing_env_fails_closed(tmp_path):
    with pytest.raises(PreflightRefused, match="SERVICE_ENV_UNREADABLE"):
        read_service_env(tmp_path / "missing")


def test_duplicate_key_fails_closed(tmp_path):
    with pytest.raises(PreflightRefused, match="DUPLICATE"):
        read_service_env(env_path(tmp_path, ENV + "DB_PASSWORD=another\n"))


@pytest.mark.parametrize("activation,counts,reason", [
    (("ME_LONG_V2_INVERSE_SHORT_V1", "RUNNING", "fp", None),
     (0, 0, 0), "ACTIVATION_NOT_READY"),
    (("ME_LONG_V2_INVERSE_SHORT_V1", "READY_TO_START", "fp", None),
     (0, 0, 0), "ALREADY_FROZEN"),
    (("ME_LONG_V2_INVERSE_SHORT_V1", "READY_TO_START", None, None),
     (1, 0, 0), "EXISTING_OOS_DATA"),
    (("ME_LONG_V2_INVERSE_SHORT_V1", "READY_TO_START", None, None),
     (0, 1, 0), "EXISTING_OOS_DATA"),
    (("ME_LONG_V2_INVERSE_SHORT_V1", "READY_TO_START", None, None),
     (0, 0, 1), "EXISTING_OOS_DATA"),
])
def test_preflight_refuses_unsafe_state(tmp_path, activation, counts, reason):
    cursor = Cursor(activation=activation, counts=counts)
    conn = Conn(cursor)
    with pytest.raises(PreflightRefused, match=reason):
        run_preflight(env_path(tmp_path), connect=lambda **kw: conn)
    assert conn.rollbacks == 1 and conn.closed


def test_connection_exception_redacted(tmp_path):
    def failed_connect(**kwargs):
        raise RuntimeError("pretend-secret-leaked-by-driver")
    with pytest.raises(PreflightRefused, match="DATABASE_READ_ONLY_CHECK_FAILED") as exc:
        run_preflight(env_path(tmp_path), connect=failed_connect)
    assert "pretend-secret" not in str(exc.value)


def test_preflight_repeated_calls_do_not_write(tmp_path):
    cursor = Cursor()
    for _ in range(2):
        cursor.queries.clear()
        result = run_preflight(env_path(tmp_path), connect=lambda **kw: Conn(cursor))
        assert result["events"] == 0
        assert all(
            sql.startswith("SELECT ") or sql == "BEGIN TRANSACTION READ ONLY"
            for sql, _ in cursor.queries
        )
