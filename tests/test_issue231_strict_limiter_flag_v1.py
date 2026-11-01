"""ISSUE231 R1 — STRICT boolean interpretation of
``global_bybit_rate_limiter_enabled`` (regression suite).

Unsafe behavior being fixed: all three interpretation sites coerced the
flag with ``bool(...)``, so ``bool("false")``/``bool(0)`` silently
ENABLED the shared Bybit rate limiter while ``bool(None)`` silently
disabled an explicitly enabled one. Required contract:

* missing flag → ``False`` (default-off preserved),
* only Python ``bool`` values (``True`` / ``False``) are accepted,
* strings, numbers, ``None`` and other types → explicit configuration
  error (``ValueError``), never coercion (fail-closed),

verified at all three sites: ``load_settings()``,
``BybitClient._limiter_enabled()`` and ``LimiterConfig.from_settings()``.
No other limiter parameter and no trading algorithm is touched.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config.settings import Settings, load_settings, parse_strict_bool_flag
from app.exchange.bybit_client import BybitClient
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    LimiterConfig,
)

FLAG = "global_bybit_rate_limiter_enabled"

#: Non-bool payloads a JSON/YAML config (or a hand-built Settings) may
#: realistically carry. Every one of them must be an explicit error.
INVALID_FLAG_VALUES = [
    # strings — the historical coercion trap: bool("false") is True
    "true", "false", "True", "1", "0", "yes", "off",
    # numbers — bool(0)/bool(1) silently flip the gate
    1, 0, 1.0, 0.0,
    # null
    None,
    # containers
    [], {},
]
INVALID_IDS = [f"{type(v).__name__}_{v}" for v in INVALID_FLAG_VALUES]
BOOL_IDS = ["true", "false"]


# ------------------------------------------------------------ shared parser
@pytest.mark.parametrize("value", [True, False], ids=BOOL_IDS)
def test_strict_parser_accepts_python_bools(value):
    assert parse_strict_bool_flag(value, FLAG) is value


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES, ids=INVALID_IDS)
def test_strict_parser_rejects_non_bools(value):
    with pytest.raises(ValueError, match=FLAG):
        parse_strict_bool_flag(value, FLAG)


# ----------------------------------------------------------- load_settings
def _load(tmp_path, payload):
    """load_settings() against an isolated JSON config and no .env file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(config_path, env_file=tmp_path / ".env.absent")


def test_load_settings_missing_flag_defaults_false(tmp_path):
    settings = _load(tmp_path, {"trading_mode": "paper"})
    assert settings.global_bybit_rate_limiter_enabled is False
    # default-off is also the dataclass default
    assert Settings().global_bybit_rate_limiter_enabled is False


@pytest.mark.parametrize("value", [True, False], ids=BOOL_IDS)
def test_load_settings_accepts_literal_bools(tmp_path, value):
    settings = _load(tmp_path, {FLAG: value})
    assert settings.global_bybit_rate_limiter_enabled is value


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES, ids=INVALID_IDS)
def test_load_settings_rejects_non_bools(tmp_path, value):
    with pytest.raises(ValueError, match=FLAG):
        _load(tmp_path, {FLAG: value})


# -------------------------------------------------- BybitClient gate
def _client(settings) -> BybitClient:
    """Client with a dummy session — construction performs no I/O."""
    return BybitClient(settings, session=object())


@pytest.mark.parametrize("value", [True, False], ids=BOOL_IDS)
def test_limiter_enabled_accepts_bool(value):
    client = _client(Settings(**{FLAG: value}))
    assert client._limiter_enabled() is value


def test_limiter_enabled_missing_attribute_is_false():
    client = _client(SimpleNamespace())          # settings lack the flag
    assert client._limiter_enabled() is False


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES, ids=INVALID_IDS)
def test_limiter_enabled_rejects_non_bools(value):
    client = _client(Settings(**{FLAG: value}))
    with pytest.raises(ValueError, match=FLAG):
        client._limiter_enabled()
    # fail-closed: the request gate never proceeds on a coerced value
    # and never lazily builds a limiter from a misconfigured flag.
    with pytest.raises(ValueError, match=FLAG):
        client._shared_limiter()
    assert client._shared_limiter_obj is None


def test_shared_limiter_default_off_zero_io():
    client = _client(Settings())
    assert client._limiter_enabled() is False
    assert client._shared_limiter() is None
    assert client._shared_limiter_obj is None


def test_shared_limiter_enabled_builds_once():
    client = _client(Settings(**{FLAG: True}))
    limiter = client._shared_limiter()
    assert isinstance(limiter, GlobalBybitRateLimiter)
    assert limiter.config.enabled is True
    assert client._shared_limiter() is limiter    # built once per client


# ------------------------------------------------ LimiterConfig.from_settings
@pytest.mark.parametrize("value", [True, False], ids=BOOL_IDS)
def test_from_settings_accepts_bool(value):
    cfg = LimiterConfig.from_settings(Settings(**{FLAG: value}))
    assert cfg.enabled is value


@pytest.mark.parametrize(
    "settings_obj", [None, SimpleNamespace()], ids=["none", "no_attr"])
def test_from_settings_missing_flag_defaults_false(settings_obj):
    assert LimiterConfig.from_settings(settings_obj).enabled is False
    assert LimiterConfig().enabled is False        # default-off contract


@pytest.mark.parametrize("value", INVALID_FLAG_VALUES, ids=INVALID_IDS)
def test_from_settings_rejects_non_bools(value):
    with pytest.raises(ValueError, match=FLAG):
        LimiterConfig.from_settings(Settings(**{FLAG: value}))


def test_flag_toggles_only_enabled_field():
    """The strict gate must not change any other limiter parameter."""
    plain = LimiterConfig.from_settings(Settings())
    assert plain == LimiterConfig()                 # all defaults unchanged
    on = LimiterConfig.from_settings(Settings(**{FLAG: True}))
    off = LimiterConfig.from_settings(Settings(**{FLAG: False}))
    assert replace(on, enabled=False) == off        # only `enabled` differs


def test_default_off_acquire_is_zero_io(tmp_path):
    """Default-off core grants without ever opening shared state."""
    state_file = tmp_path / "global_limiter_state.json"
    cfg = replace(
        LimiterConfig.from_settings(Settings()),
        state_path=str(state_file),
    )
    limiter = GlobalBybitRateLimiter(cfg)
    outcome = limiter.acquire("P0", deadline=0.0)
    assert outcome.granted
    assert outcome.reason == "disabled"
    assert not state_file.exists()
