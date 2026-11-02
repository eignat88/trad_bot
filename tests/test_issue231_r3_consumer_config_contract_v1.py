"""ISSUE231_R3_MINIMAL_CLOSURE_IMPLEMENTATION — consumer coverage regression.

Offline, deterministic coverage for the four R3 wiring fixes:

* config resolution — every fixed consumer resolves the SAME R2 contract
  (config.yaml two-factor activation + strict validation) instead of an
  implicit default ``Settings()``;
* default-off — without a config the limiter stays off for every consumer;
* one shared state_path and identical G/B parameters across consumers;
* fail-closed on config errors (broken limiter params / absent required
  config) — never a silent fallback, never an HTTP request;
* zero unexpected HTTP — ``urlopen`` is poisoned for the whole module;
* no research-logic change — the observer's frozen CSV replay path never
  touches the config contract, and the outcome runtime protocol is only
  observed via its client-construction helper (identity/session/registry
  untouched).

No network, no database, no Bybit requests, no production state.
"""
from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.analytics.cli as analytics_cli
import app.config as config_module
import app.exchange.bybit_client as bybit_client_module
import app.research.htf_baseline_short_phase2_observer_runner_v1 as observer
import app.research.me_short_a_forward_outcome_runtime_runner_v1 as outcome_runner
from app.config import LimiterConfig, Settings
from app.exchange.bybit_client import BybitClient as RealBybitClient

#: every field of the shared limiter contract (activation flags + parameters)
CONTRACT_FIELDS = tuple(
    field.name for field in dataclass_fields(Settings)
    if field.name.startswith(("global_limiter_", "global_bybit_rate_limiter_"))
)

# the contract overlay must never silently match an empty field set
assert "global_bybit_rate_limiter_enabled" in CONTRACT_FIELDS
assert "global_limiter_state_path" in CONTRACT_FIELDS
assert "global_limiter_g_rps" in CONTRACT_FIELDS


@pytest.fixture(autouse=True)
def _offline_and_isolated_env(monkeypatch):
    """No HTTP ever; ENV activation starts from a clean slate per test."""
    def _poisoned(*args, **kwargs):
        raise AssertionError("unexpected HTTP request — R3 tests stay offline")

    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ENABLED", raising=False)
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ALLOW_ENV", raising=False)


def _write_config(tmp_path: Path, payload: dict) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


def _assert_same_contract(settings: Settings, reference: Settings,
                          label: str) -> None:
    """Both objects carry the exact same resolved limiter contract."""
    for name in CONTRACT_FIELDS:
        assert getattr(settings, name) == getattr(reference, name), (
            label, name, getattr(settings, name), getattr(reference, name))
    assert LimiterConfig.from_settings(settings) == \
        LimiterConfig.from_settings(reference), label


def test_all_fixed_consumers_share_one_r2_contract(tmp_path, monkeypatch):
    """Analytics / observer / outcome / reconcile all resolve ONE contract.

    Config resolution + shared state_path and G/B: with one config.yaml
    every fixed consumer must produce the identical limiter contract —
    the proof that they would share one state file and one bucket.
    """
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, {
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_g_rps": 4.5,
        "global_limiter_burst": 9.0,
        "global_limiter_state_path": str(tmp_path / "shared_state.json"),
    })
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("DB_HOST", "analytics-env-host")

    reference = config_module.load_settings()
    analytics = analytics_cli.load_analytics_settings()

    source = observer.build_candle_source(
        SimpleNamespace(candle_source="bybit"), observer.RunnerConfig())
    assert isinstance(source, observer.BybitPhase2CandleSource)

    outcome_client = outcome_runner._build_bybit_client()

    from tools.bybit_order_submit_reconcile_v1 import _load_settings
    reconcile = _load_settings(str(tmp_path / "config.yaml"), required=True)

    consumers = {
        "reference": reference,
        "analytics": analytics,
        "observer": source.client.settings,
        "outcome": outcome_client.settings,
        "reconcile": reconcile,
    }
    for label, settings in consumers.items():
        assert settings.global_bybit_rate_limiter_enabled is True, label
        assert settings.global_limiter_g_rps == 4.5, label
        assert settings.global_limiter_burst == 9.0, label
        assert settings.global_limiter_state_path.endswith(
            "shared_state.json"), (label, settings.global_limiter_state_path)
        _assert_same_contract(settings, reference, label)

    # analytics-owned ENV settings stay authoritative (not overridden)
    assert analytics.db_host == "analytics-env-host"
    # every construction above ran with urlopen poisoned -> zero HTTP


def test_analytics_default_off_and_client_constructs_offline(tmp_path,
                                                             monkeypatch):
    """Default-off: no config -> disabled contract; client builds with
    zero HTTP attempts (poisoned urlopen) and stays out of the limiter."""
    monkeypatch.chdir(tmp_path)          # no config.yaml -> built-in defaults
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")

    settings = analytics_cli.load_analytics_settings()
    assert settings.global_bybit_rate_limiter_enabled is False
    assert settings.global_limiter_g_rps == 8.0        # default preserved
    assert LimiterConfig.from_settings(settings).enabled is False

    client = RealBybitClient(settings)   # construction only — no I/O
    assert client._limiter_enabled() is False


def test_analytics_env_activation_honors_config_permission(tmp_path,
                                                           monkeypatch):
    """The two-factor gate reaches analytics: ENV activation participates
    only when the config file grants global_bybit_rate_limiter_allow_env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    monkeypatch.setenv("GLOBAL_BYBIT_RATE_LIMITER_ENABLED", "true")

    _write_config(tmp_path, {"global_limiter_g_rps": 4.5})   # no permission
    assert analytics_cli.load_analytics_settings(
    ).global_bybit_rate_limiter_enabled is False

    _write_config(tmp_path, {
        "global_limiter_g_rps": 4.5,
        "global_bybit_rate_limiter_allow_env": True,
    })
    assert analytics_cli.load_analytics_settings(
    ).global_bybit_rate_limiter_enabled is True


def test_analytics_fails_closed_on_invalid_contract(tmp_path, monkeypatch):
    """A broken limiter parameter propagates out of the settings loader —
    no silent default-off fallback, no client, no HTTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    _write_config(tmp_path, {"global_limiter_g_rps": "fast"})

    with pytest.raises(ValueError, match="global_limiter_g_rps"):
        analytics_cli.load_analytics_settings()


def test_analytics_disabled_short_circuits_before_contract(tmp_path,
                                                           monkeypatch):
    """ANALYTICS_ENABLED=false keeps its fail-fast exit(0) and never
    consults the shared contract (a broken config cannot break a disabled
    pipeline)."""
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, {"global_limiter_g_rps": "fast"})  # broken config
    monkeypatch.setenv("ANALYTICS_ENABLED", "false")

    with pytest.raises(SystemExit) as excinfo:
        analytics_cli.load_analytics_settings()
    assert excinfo.value.code == 0


def test_observer_csv_replay_path_never_touches_config(tmp_path, monkeypatch):
    """Research-logic change guard: the frozen CSV replay branch returns the
    file source WITHOUT resolving any config contract."""
    monkeypatch.chdir(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("csv replay must not resolve the config contract")

    monkeypatch.setattr(config_module, "load_settings", _boom)

    source = observer.build_candle_source(
        SimpleNamespace(candle_source="csv", data_dir=str(tmp_path)),
        observer.RunnerConfig())
    assert isinstance(source, observer.FileCandleSource)


def test_reconcile_required_config_absent_fails_closed(tmp_path):
    """--reconcile never falls back to default Settings(): an absent config
    is a fail-closed FileNotFoundError; offline modes get no settings object
    at all instead of implicit defaults."""
    from tools.bybit_order_submit_reconcile_v1 import _load_settings

    missing = str(tmp_path / "absent.yaml")
    with pytest.raises(FileNotFoundError, match="never falls back"):
        _load_settings(missing, required=True)

    assert _load_settings(missing, required=False) is None


def test_reconcile_present_config_uses_common_contract(tmp_path, monkeypatch):
    """An existing --config is resolved through the common R2 contract
    (same fields, same LimiterConfig as load_settings itself)."""
    monkeypatch.chdir(tmp_path)
    config = _write_config(tmp_path, {
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_g_rps": 4.5,
        "global_limiter_state_path": str(tmp_path / "reconcile_state.json"),
    })

    from tools.bybit_order_submit_reconcile_v1 import _load_settings
    settings = _load_settings(str(config), required=True)

    assert settings.global_bybit_rate_limiter_enabled is True
    assert settings.global_limiter_g_rps == 4.5
    _assert_same_contract(settings, config_module.load_settings(str(config)),
                          "reconcile")
