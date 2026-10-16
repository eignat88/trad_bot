"""Issue #152: fail-closed isolated runner activation tests."""

import sys
from unittest.mock import MagicMock, ModuleType, patch

import pytest

from app.research.me_r_long_early_mae_v3 import protocol
from app.research.me_r_long_early_mae_v3 import runner


def test_runner_disabled_without_any_opt_in(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--once"])
    monkeypatch.delenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", raising=False)
    monkeypatch.delenv("ME_EARLY_MAE_V3_ISOLATED_ONCE", raising=False)

    with patch.object(
        runner,
        "MeEarlyMaeV3Pipeline",
        side_effect=AssertionError("PIPELINE_MUST_NOT_START"),
    ):
        with pytest.raises(SystemExit, match="CAPTURE_DISABLED"):
            runner.main()


def test_runner_rejects_production_database():
    with pytest.raises(RuntimeError, match="ISOLATED_DATABASE_REQUIRED"):
        runner.require_isolated_database_name("trad_bot")


def test_runner_rejects_other_test_database():
    with pytest.raises(RuntimeError, match="ISOLATED_DATABASE_REQUIRED"):
        runner.require_isolated_database_name("trad_bot_v3_test")


def test_runner_accepts_exact_isolated_database():
    assert (
        runner.require_isolated_database_name("trad_bot_runner_152_it")
        == "trad_bot_runner_152_it"
    )


def _mock_pg8000(monkeypatch, row):
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = row
    dbapi = ModuleType("pg8000.dbapi")
    dbapi.connect = MagicMock(return_value=conn)
    pg8000 = ModuleType("pg8000")
    pg8000.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "pg8000", pg8000)
    monkeypatch.setitem(sys.modules, "pg8000.dbapi", dbapi)
    return conn, dbapi


def test_connection_identity_mismatch_fails_closed(monkeypatch):
    conn, _ = _mock_pg8000(
        monkeypatch,
        ["trad_bot", "trad_bot", "trad_bot", True],
    )
    with pytest.raises(RuntimeError, match="DATABASE_IDENTITY_MISMATCH"):
        runner.connect_isolated_database()
    conn.close.assert_called_once()


def test_connection_identity_verified(monkeypatch):
    conn, dbapi = _mock_pg8000(
        monkeypatch,
        [
            "trad_bot_runner_152_it",
            "trad_bot_runner_152_it",
            "trad_bot_runner_152_it",
            True,
        ],
    )
    result = runner.connect_isolated_database()
    assert result is conn
    conn.rollback.assert_called_once()
    conn.close.assert_not_called()
    assert dbapi.connect.call_args.kwargs["database"] == "trad_bot_runner_152_it"
    assert dbapi.connect.call_args.kwargs["user"] == "trad_bot_runner_152_it"


def test_preflight_rejects_missing_isolated_opt_in(monkeypatch):
    monkeypatch.delenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", raising=False)
    with pytest.raises(RuntimeError, match="ISOLATED_OPT_IN_REQUIRED"):
        runner.isolated_preflight()


def test_preflight_does_not_connect_without_opt_in(monkeypatch):
    monkeypatch.delenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", raising=False)
    with patch.object(
        runner,
        "connect_isolated_database",
        side_effect=AssertionError("CONNECTION_MUST_NOT_OPEN"),
    ):
        with pytest.raises(RuntimeError, match="ISOLATED_OPT_IN_REQUIRED"):
            runner.isolated_preflight()


def test_cli_preflight_calls_guard_without_pipeline(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--preflight"])
    with patch.object(runner, "isolated_preflight") as preflight:
        with patch.object(
            runner,
            "MeEarlyMaeV3Pipeline",
            side_effect=AssertionError("PIPELINE_MUST_NOT_START"),
        ):
            runner.main()
    preflight.assert_called_once()


def test_cli_once_requires_both_explicit_opt_ins(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--once"])
    monkeypatch.setenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", "true")
    monkeypatch.delenv("ME_EARLY_MAE_V3_ISOLATED_ONCE", raising=False)
    with pytest.raises(RuntimeError, match="ISOLATED_ONCE_OPT_IN_REQUIRED"):
        runner.run_isolated_once()


def test_cli_preflight_once_combination_is_disabled(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--preflight", "--once"])
    with pytest.raises(SystemExit, match="CAPTURE_DISABLED"):
        runner.main()


def test_isolated_fingerprint_matches_operator_approved_values(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(protocol, "FROZEN_AT", datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(protocol, "ACTIVATION_AT", datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(
        protocol,
        "APPROVED_FINGERPRINT",
        "ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a",
    )
    assert protocol.PROTOCOL_VERSION == "3.0.0"
    runner.validate_isolated_fingerprint_config()


def test_isolated_os_user_rejects_wrong_uid(monkeypatch):
    # CI runners do not have the VPS-only service account. Mock identity
    # resolution to test the UID rejection path independently of host users.
    import pwd
    from types import SimpleNamespace

    monkeypatch.setattr(pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=12345))
    monkeypatch.setattr(runner.os, "getuid", lambda: 0)
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="ISOLATED_OS_USER_REQUIRED"):
        runner.require_isolated_os_user()


def test_runtime_requires_strict_activation_boundary(monkeypatch):
    from datetime import datetime, timezone

    boundary = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(protocol, "ACTIVATION_AT", boundary)
    with pytest.raises(RuntimeError, match="ACTIVATION_NOT_EFFECTIVE"):
        runner._require_strict_activation(boundary)
    runner._require_strict_activation(
        datetime(2026, 10, 19, 12, 0, 1, tzinfo=timezone.utc)
    )


def test_isolated_once_rolls_back_evaluator_errors(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", "true")
    monkeypatch.setenv("ME_EARLY_MAE_V3_ISOLATED_ONCE", "true")
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.fetchone.return_value = (1,)
    pipeline = MagicMock()
    pipeline.evaluate_cycle.return_value = {"errors": 1}
    with patch.object(runner, "isolated_preflight"), patch.object(
        runner, "require_runtime_configuration_now"
    ), patch.object(
        runner, "connect_isolated_database", return_value=conn
    ), patch.object(
        runner, "require_isolated_schema"
    ) as schema_guard, patch.object(
        runner, "MeEarlyMaeV3Pipeline", return_value=pipeline
    ):
        with pytest.raises(RuntimeError, match="EVALUATOR_ERRORS"):
            runner.run_isolated_once()
    schema_guard.assert_called_once_with(conn)
    assert conn.rollback.call_count >= 1
    conn.close.assert_called_once()
    conn.commit.assert_not_called()


def test_isolated_once_commits_success(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY", "true")
    monkeypatch.setenv("ME_EARLY_MAE_V3_ISOLATED_ONCE", "true")
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.fetchone.return_value = (1,)
    pipeline = MagicMock()
    pipeline.evaluate_cycle.return_value = {"sources": 0, "errors": 0}
    with patch.object(runner, "isolated_preflight"), patch.object(
        runner, "require_runtime_configuration_now"
    ), patch.object(
        runner, "connect_isolated_database", return_value=conn
    ), patch.object(
        runner, "require_isolated_schema"
    ) as schema_guard, patch.object(
        runner, "MeEarlyMaeV3Pipeline", return_value=pipeline
    ):
        result = runner.run_isolated_once()
    assert result["errors"] == 0
    schema_guard.assert_called_once_with(conn)
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_connection_liveness_rejects_closed_connection():
    conn = MagicMock()
    conn.closed = True
    with pytest.raises(RuntimeError, match="DB_CONNECTION_CLOSED"):
        runner.require_live_connection(conn)
