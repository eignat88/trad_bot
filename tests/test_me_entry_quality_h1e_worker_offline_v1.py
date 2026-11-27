"""Issue #324 / H1-E — C.4 offline tests: isolated worker / systemd proposal.

Default OFF with NO SQL writes and NO API calls; explicit gates fail
closed; import-time is side-effect free; the health probe is read-only;
existing services are untouched by the new unit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.research.me_entry_quality_h1e_infra_v1.worker import (
    H1E_WORKER_VERSION,
    WorkerConfig,
    WorkerGateError,
    load_config_from_env,
    main,
)

REPO = Path(__file__).resolve().parents[1]
UNIT = REPO / "deploy/systemd/trad-bot-h1e-observer.service"
ENV_EXAMPLE = REPO / "deploy/env/h1e-observer.env.example"

FULL_GATES = {
    "H1E_WORKER_ENABLE": "1",
    "H1E_DEPLOY_GATE": "1",
    "H1E_DDL_APPLIED": "1",
    "H1E_PG_DSN": "host=localhost dbname=h1e_test",
    "H1E_ACTIVATION_ID": "ME_ENTRY_QUALITY_H1E_PROSPECTIVE_OBSERVER_V1",
    "H1E_FREEZE_TS_US": "1798761600000000",
    "H1E_PROTOCOL_FINGERPRINT": "fp-test",
    "H1E_APPROVAL_REF": "issue-324-go",
    "H1E_APPROVED_BY": "owner",
}


class ExplodingFactory:
    """Fails the test if the worker ever touches the database or API."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("worker touched the database/API without gates")


# ─── default OFF ───────────────────────────────────────────────────────────


def test_worker_default_off_exits_without_db_or_api():
    factory = ExplodingFactory()
    code = main(argv=[], env={}, connection_factory=factory,
                client_factory=factory)
    assert code == 3  # repo convention: disabled / not activated
    assert factory.calls == 0  # NO SQL, NO API


def test_worker_missing_enable_is_off():
    factory = ExplodingFactory()
    code = main(argv=[], env={"H1E_WORKER_ENABLE": "0"},
                connection_factory=factory, client_factory=factory)
    assert code == 3
    assert factory.calls == 0


# ─── gates fail closed ─────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", [
    "H1E_WORKER_ENABLE",
    "H1E_DEPLOY_GATE",
    "H1E_DDL_APPLIED",
    "H1E_PG_DSN",
    "H1E_ACTIVATION_ID",
    "H1E_FREEZE_TS_US",
    "H1E_PROTOCOL_FINGERPRINT",
    "H1E_APPROVAL_REF",
    "H1E_APPROVED_BY",
])
def test_each_missing_gate_fails_closed(missing):
    env = dict(FULL_GATES)
    env.pop(missing)
    factory = ExplodingFactory()
    if missing == "H1E_WORKER_ENABLE":
        code = main(argv=[], env=env, connection_factory=factory,
                    client_factory=factory)
    else:
        code = main(argv=[], env=env, connection_factory=factory,
                    client_factory=factory)
    assert code in (2, 3)  # refused, nothing started
    assert factory.calls == 0


def test_gate_flags_must_be_exactly_one():
    for name in ("H1E_WORKER_ENABLE", "H1E_DEPLOY_GATE", "H1E_DDL_APPLIED"):
        env = dict(FULL_GATES)
        env[name] = "true"  # not exactly '1'
        with pytest.raises(WorkerGateError):
            load_config_from_env(env)


def test_config_load_is_fail_closed():
    config = load_config_from_env(FULL_GATES)
    assert config.freeze_ts_us == 1798761600000000
    capture = config.capture_config()
    assert capture.allow_fixture_freeze is False  # never in production
    assert capture.retry_batch_limit == 50  # bounded by default
    bad = dict(FULL_GATES, H1E_FREEZE_TS_US="not-an-int")
    with pytest.raises(WorkerGateError):
        load_config_from_env(bad)


# ─── import-time safety ────────────────────────────────────────────────────


def test_module_import_performs_no_work():
    result = subprocess.run(
        [sys.executable, "-c",
         "import app.research.me_entry_quality_h1e_infra_v1.worker"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # no logging noise, no start


# ─── health probe is read-only ─────────────────────────────────────────────


def test_healthcheck_is_read_only():
    statements: list[str] = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            statements.append(sql)
            self._rows = [("APPROVED",)] if "h1e_activation" in sql else [(1,)]

        def fetchone(self):
            return self._rows[0]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    factory = ExplodingFactory()
    code = main(argv=["--health"], env=dict(FULL_GATES),
                connection_factory=lambda dsn: lambda: FakeConnection(),
                client_factory=factory)
    assert code == 0
    assert statements, "health probe connected"
    assert all(sql.lstrip().upper().startswith("SELECT")
               for sql in statements)  # read-only
    assert factory.calls == 0  # health never calls the API


# ─── existing services untouched ───────────────────────────────────────────


def test_systemd_unit_is_disabled_by_default_template():
    unit = UNIT.read_text(encoding="utf-8")
    assert UNIT.exists()
    assert "TEMPLATE ONLY" in unit
    assert "SuccessExitStatus=3" in unit
    assert "-m app.research.me_entry_quality_h1e_infra_v1.worker" in unit
    # the unit NEVER references the frozen H1-D package by name
    assert "me_entry_quality_h1d" not in unit


def test_env_example_ships_all_gates_off():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line, expected in (
        ("H1E_WORKER_ENABLE=0", True),
        ("H1E_DEPLOY_GATE=0", True),
        ("H1E_DDL_APPLIED=0", True),
    ):
        assert expected and line in env_text
    assert "me_entry_quality_h1d" not in env_text


def test_existing_services_are_isolated():
    """No existing unit or env example gained H1-E wiring."""
    scanned = list((REPO / "deploy/systemd").iterdir())
    scanned += list((REPO / "deploy/env").iterdir())
    for path in scanned:
        if not path.is_file():
            continue
        if path in (UNIT, ENV_EXAMPLE):
            continue
        text = path.read_text(encoding="utf-8")
        assert "me_entry_quality_h1e_infra_v1" not in text, path
        assert "tradbot_h1e_observer" not in text, path


def test_worker_version_is_declared():
    assert H1E_WORKER_VERSION == "H1E_WORKER_V1"
    assert isinstance(load_config_from_env(FULL_GATES), WorkerConfig)
