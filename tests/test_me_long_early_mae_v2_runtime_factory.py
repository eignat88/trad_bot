from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import scanner_runner


@pytest.fixture(autouse=True)
def reset_v2_singletons(monkeypatch):
    monkeypatch.setattr(scanner_runner, "_me_early_mae_v2_pipeline", None)
    monkeypatch.setattr(scanner_runner, "_me_early_mae_v2_conn", None)


def repository_stub():
    return SimpleNamespace(
        _host="127.0.0.1",
        _port=5546,
        _database="me_early_mae_v2_test",
        _user="test",
        _password="test",
    )


def test_capture_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ME_EARLY_MAE_V2_CAPTURE_ENABLED", raising=False)

    with patch("pg8000.connect") as connect:
        assert scanner_runner._get_me_early_mae_v2_pipeline(
            repository_stub()
        ) is None
        connect.assert_not_called()


def test_capture_initializes_dedicated_connection(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V2_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("ME_EARLY_MAE_V2_FREEZE_APPROVED", "1")

    connection = MagicMock()
    pipeline = MagicMock()

    with (
        patch("pg8000.connect", return_value=connection) as connect,
        patch(
            "app.research.me_r_long_early_mae_v2.pipeline.MeEarlyMaeV2Pipeline",
            return_value=pipeline,
        ),
    ):
        result = scanner_runner._get_me_early_mae_v2_pipeline(
            repository_stub()
        )

    assert result is pipeline
    pipeline.initialize.assert_called_once()
    connect.assert_called_once()
    assert scanner_runner._me_early_mae_v2_conn is connection


def test_capture_reuses_existing_pipeline(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V2_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("ME_EARLY_MAE_V2_FREEZE_APPROVED", "1")

    pipeline = MagicMock()
    monkeypatch.setattr(
        scanner_runner, "_me_early_mae_v2_pipeline", pipeline
    )

    with patch("pg8000.connect") as connect:
        assert scanner_runner._get_me_early_mae_v2_pipeline(
            repository_stub()
        ) is pipeline
        connect.assert_not_called()


def test_failed_initialization_closes_connection(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V2_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("ME_EARLY_MAE_V2_FREEZE_APPROVED", "1")

    connection = MagicMock()
    pipeline = MagicMock()
    pipeline.initialize.side_effect = RuntimeError("freeze failed")

    with (
        patch("pg8000.connect", return_value=connection),
        patch(
            "app.research.me_r_long_early_mae_v2.pipeline.MeEarlyMaeV2Pipeline",
            return_value=pipeline,
        ),
    ):
        assert scanner_runner._get_me_early_mae_v2_pipeline(
            repository_stub()
        ) is None

    connection.close.assert_called_once()
    assert scanner_runner._me_early_mae_v2_pipeline is None


def test_capture_refused_without_freeze_approval(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V2_CAPTURE_ENABLED", "1")
    monkeypatch.delenv("ME_EARLY_MAE_V2_FREEZE_APPROVED", raising=False)

    with patch("pg8000.connect") as connect:
        result = scanner_runner._get_me_early_mae_v2_pipeline(
            repository_stub()
        )

    assert result is None
    connect.assert_not_called()
    assert scanner_runner._me_early_mae_v2_pipeline is None
