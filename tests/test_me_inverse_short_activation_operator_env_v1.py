"""Isolated CLI tests; never connect to real PostgreSQL."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import me_inverse_short_v1_activate as operator

ENV = (
    "ME_INVERSE_SHORT_V1_ENABLED=0\n"
    "DB_HOST=127.0.0.1\nDB_PORT=5432\nDB_NAME=trad_bot\n"
    "DB_USER=mock_user\nDB_PASSWORD=mock_secret\n"
)


def make_env(tmp_path, content=ENV):
    p = tmp_path / "me.env"
    p.write_text(content)
    return p


def test_writes_require_dedicated_env_file(capsys):
    assert operator.main(["--activate", "--freeze-ts", "2026-01-01T00:00:00Z"]) == 1
    assert operator.main(["--drain"]) == 1
    captured = capsys.readouterr()
    assert "--env-file is required" in captured.err


def test_activate_refuses_enabled_runtime_env_before_connection(tmp_path, monkeypatch):
    path = make_env(tmp_path, ENV.replace("ENABLED=0", "ENABLED=1"))
    monkeypatch.setattr(operator, "_connect", lambda *a, **k: pytest.fail("must not connect"))
    assert operator.main([
        "--activate", "--freeze-ts", "2026-01-01T00:00:00Z",
        "--env-file", str(path),
    ]) == 1


def test_operator_show_routes_dedicated_database_env(tmp_path, monkeypatch, capsys):
    path = make_env(tmp_path)
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            assert sql.startswith("SELECT ")
            assert params == ("ME_LONG_V2_INVERSE_SHORT_V1",)
        def fetchone(self):
            return ("READY_TO_START", None, None, None, None)
        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()
        def close(self):
            pass

    monkeypatch.setattr(operator, "load_settings", lambda: SimpleNamespace())
    def connect(settings, db_env=None):
        calls.append(db_env)
        return Connection()
    monkeypatch.setattr(operator, "_connect", connect)

    assert operator.main(["--show", "--env-file", str(path)]) == 0
    assert len(calls) == 1
    assert calls[0]["DB_USER"] == "mock_user"
    assert calls[0]["DB_NAME"] == "trad_bot"
    output = capsys.readouterr().out
    assert "READY_TO_START" in output
    assert "mock_secret" not in output
