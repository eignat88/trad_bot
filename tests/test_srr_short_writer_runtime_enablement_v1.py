from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import SrrShortWriterSettings, load_settings
from app.research.srr_short_runtime_wiring import prepare_srr_short_writer
from app.research.srr_short_writer_connection_manager import (
    open_srr_outcome_writer,
)

APPROVED_TS = "2030-01-01T00:00:00.000000Z"


@pytest.fixture
def isolated_config(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"symbols": ["BTCUSDT"]}), encoding="utf-8")
    return config


def _load(monkeypatch, config: Path, **env):
    monkeypatch.delenv("SRR_SHORT_WRITER_ENABLED", raising=False)
    monkeypatch.delenv("SRR_SHORT_WRITER_ACTIVATION_TS", raising=False)
    monkeypatch.delenv("SRR_SHORT_WRITER_APPROVAL_REFERENCE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(config, env_file=config.parent / "missing.env")


def test_srr_writer_configuration_is_disabled_by_default(
    monkeypatch, isolated_config
):
    settings = _load(monkeypatch, isolated_config)
    assert settings.srr_short_writer == SrrShortWriterSettings()


def test_missing_enabled_flag_keeps_writer_disabled(
    monkeypatch, isolated_config
):
    settings = _load(
        monkeypatch,
        isolated_config,
        SRR_SHORT_WRITER_ACTIVATION_TS=APPROVED_TS,
        SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
    )
    assert settings.srr_short_writer.enabled is False


@pytest.mark.parametrize("value", ["maybe", "2", ""])
def test_invalid_enabled_flag_fails_closed(monkeypatch, isolated_config, value):
    with pytest.raises(ValueError, match="SRR_SHORT_WRITER_ENABLED"):
        _load(
            monkeypatch,
            isolated_config,
            SRR_SHORT_WRITER_ENABLED=value,
        )


def test_explicit_disable_overrides_enabled_timestamp(monkeypatch, isolated_config):
    settings = _load(
        monkeypatch,
        isolated_config,
        SRR_SHORT_WRITER_ENABLED="off",
        SRR_SHORT_WRITER_ACTIVATION_TS=APPROVED_TS,
        SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
    )
    assert settings.srr_short_writer.enabled is False


@pytest.mark.parametrize("timestamp", ["", None])
def test_enabled_requires_explicit_timestamp(
    monkeypatch, isolated_config, timestamp
):
    with pytest.raises(ValueError, match="ACTIVATION_TS"):
        _load(
            monkeypatch,
            isolated_config,
            SRR_SHORT_WRITER_ENABLED="true",
            **(
                {"SRR_SHORT_WRITER_ACTIVATION_TS": timestamp}
                if timestamp is not None
                else {}
            ),
            SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2030-01-01T00:00:00",
        "2030-01-01T02:00:00+02:00",
        "2030-01-01T00:00:00.1Z",
        "not-a-timestamp",
    ],
)
def test_enabled_rejects_noncanonical_or_naive_timestamp(
    monkeypatch, isolated_config, timestamp
):
    with pytest.raises(ValueError):
        _load(
            monkeypatch,
            isolated_config,
            SRR_SHORT_WRITER_ENABLED="true",
            SRR_SHORT_WRITER_ACTIVATION_TS=timestamp,
            SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2030-01-01T00:00:00.000000Z",
        "2030-01-01T00:00:00.123456Z",
    ],
)
def test_enabled_accepts_microsecond_precision(
    monkeypatch, isolated_config, timestamp
):
    settings = _load(
        monkeypatch,
        isolated_config,
        SRR_SHORT_WRITER_ENABLED="true",
        SRR_SHORT_WRITER_ACTIVATION_TS=timestamp,
        SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
    )
    assert settings.srr_short_writer.activation_ts == timestamp


@pytest.mark.parametrize(
    "timestamp",
    [
        "2030-01-01T00:00:00.0000000Z",
        "2030-01-01T00:00:00.000000001Z",
        "2030-01-01T00:00:00.1234567Z",
    ],
)
def test_enabled_rejects_precision_beyond_microseconds(
    monkeypatch, isolated_config, timestamp
):
    with pytest.raises(ValueError, match="microsecond precision"):
        _load(
            monkeypatch,
            isolated_config,
            SRR_SHORT_WRITER_ENABLED="true",
            SRR_SHORT_WRITER_ACTIVATION_TS=timestamp,
            SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
        )


def test_enabled_accepts_zero_fractional_digits(monkeypatch, isolated_config):
    settings = _load(
        monkeypatch,
        isolated_config,
        SRR_SHORT_WRITER_ENABLED="true",
        SRR_SHORT_WRITER_ACTIVATION_TS="2030-01-01T00:00:00Z",
        SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
    )
    assert settings.srr_short_writer.activation_ts == (
        "2030-01-01T00:00:00.000000Z"
    )


def test_enabled_requires_approval_reference(monkeypatch, isolated_config):
    with pytest.raises(ValueError, match="APPROVAL_REFERENCE"):
        _load(
            monkeypatch,
            isolated_config,
            SRR_SHORT_WRITER_ENABLED="true",
            SRR_SHORT_WRITER_ACTIVATION_TS=APPROVED_TS,
        )


def test_enabled_canonical_configuration_is_scoped_to_srr(monkeypatch, isolated_config):
    settings = _load(
        monkeypatch,
        isolated_config,
        SRR_SHORT_WRITER_ENABLED="1",
        SRR_SHORT_WRITER_ACTIVATION_TS=APPROVED_TS,
        SRR_SHORT_WRITER_APPROVAL_REFERENCE="OP-1",
    )
    assert settings.srr_short_writer == SrrShortWriterSettings(
        enabled=True,
        activation_ts=APPROVED_TS,
        approval_reference="OP-1",
    )


class FakeConnection:
    def __init__(self, *, autocommit: bool = False):
        self._autocommit = autocommit
        self.closed = False
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    # pg8000 connections expose the DB-API autocommit property. The fake
    # models the property explicitly so assignment is observable and cannot
    # silently create a per-instance attribute unlike the production driver.
    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._autocommit = bool(value)


def test_writer_connection_sets_autocommit_false_from_initial_true():
    reader = FakeConnection()
    writer_conn = FakeConnection(autocommit=True)

    with open_srr_outcome_writer(
        host="127.0.0.1",
        port=5432,
        database="isolated",
        user="writer",
        reader_conn=reader,
        connect_factory=lambda **_: writer_conn,
    ) as writer:
        assert writer_conn.autocommit is False
        assert writer._conn is writer_conn

    assert writer_conn.closed is True
    assert writer_conn.rollbacks >= 1


class AutocommitLockingConnection(FakeConnection):
    @FakeConnection.autocommit.setter
    def autocommit(self, value: bool) -> None:
        if not value:
            raise RuntimeError("driver refused transactional mode")
        self._autocommit = True


def test_writer_connection_fails_closed_when_transactional_mode_unavailable():
    reader = FakeConnection()
    writer_conn = AutocommitLockingConnection(autocommit=True)

    with pytest.raises(RuntimeError, match="transactional mode"):
        with open_srr_outcome_writer(
            host="127.0.0.1",
            port=5432,
            database="isolated",
            user="writer",
            reader_conn=reader,
            connect_factory=lambda **_: writer_conn,
        ):
            pass

    assert writer_conn.closed is True


def test_disabled_runtime_never_opens_writer_connection(monkeypatch):
    connect = Mock()
    with prepare_srr_short_writer(
        reader_conn=object(),
        host="127.0.0.1",
        port=5432,
        database="isolated",
        user="writer",
        connect_factory=connect,
    ) as writer:
        assert writer is None
    connect.assert_not_called()


def test_runtime_evaluator_constructor_requires_writer_in_write_mode():
    from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
        SrrShortExecutionRExpansionProspectiveEvaluator,
    )
    from inspect import signature

    constructor = signature(
        SrrShortExecutionRExpansionProspectiveEvaluator.__init__
    )
    assert "write_outcomes" in constructor.parameters
    assert "dry_run" in constructor.parameters
