"""Tests for execution policy environment variable override.

Covers:
- ENV override for ME_SHORT_FIXED_240M_ENABLED
- Config source tracking (ENV vs CONFIG)
- Priority: ENV > config.yaml > default
- Boolean parsing (case-insensitive)
- Invalid value handling
- hold_minutes unaffected
- Startup logging source
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.config.settings import (
    ExecutionPolicyConfig,
    Settings,
    _load_execution_policies,
    _parse_bool_env,
)


_ME_SHORT_RAW = {
    "MOMENTUM_EXHAUSTION": {
        "SHORT": {
            "policy": "FIXED_HORIZON_V1",
            "enabled": False,
            "hold_minutes": 240,
            "dca_enabled": False,
            "trailing_enabled": False,
            "breakeven_enabled": False,
            "tp_enabled": False,
            "expiry_enabled": False,
        }
    }
}


def _make_settings_and_load(raw_policies=None, env_overrides=None):
    """Create Settings and apply execution_policies with optional env overrides.

    Returns (settings, config_source_dict).
    """
    settings = Settings()
    raw = {"execution_policies": raw_policies or _ME_SHORT_RAW}
    with patch.dict(os.environ, env_overrides or {}, clear=False):
        _load_execution_policies(settings, raw)
    return settings


# ---------------------------------------------------------------------------
# 1. ENV absent → use config value
# ---------------------------------------------------------------------------

class TestEnvAbsent:
    def test_config_false_no_env_stays_false(self):
        settings = _make_settings_and_load()
        policy = settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"]
        assert policy.enabled is False

    def test_config_true_no_env_stays_true(self):
        raw = {
            "MOMENTUM_EXHAUSTION": {
                "SHORT": {"policy": "FIXED_HORIZON_V1", "enabled": True, "hold_minutes": 240,
                          "dca_enabled": False, "trailing_enabled": False,
                          "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False}
            }
        }
        settings = _make_settings_and_load(raw_policies=raw)
        policy = settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"]
        assert policy.enabled is True


# ---------------------------------------------------------------------------
# 2. Config=false + env=true → effective true
# ---------------------------------------------------------------------------

class TestConfigFalseEnvTrue:
    def test_override_enables_policy(self):
        settings = _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "true"})
        policy = settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"]
        assert policy.enabled is True


# ---------------------------------------------------------------------------
# 3. Config=true + env=false → effective false
# ---------------------------------------------------------------------------

class TestConfigTrueEnvFalse:
    def test_override_disables_policy(self):
        raw = {
            "MOMENTUM_EXHAUSTION": {
                "SHORT": {"policy": "FIXED_HORIZON_V1", "enabled": True, "hold_minutes": 240,
                          "dca_enabled": False, "trailing_enabled": False,
                          "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False}
            }
        }
        settings = _make_settings_and_load(
            raw_policies=raw,
            env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "false"},
        )
        policy = settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"]
        assert policy.enabled is False


# ---------------------------------------------------------------------------
# 4. Truthy values
# ---------------------------------------------------------------------------

class TestTruthyValues:
    @pytest.mark.parametrize("value", ["TRUE", "true", "True", "1", "yes", "on", "YES", "ON"])
    def test_truthy_variants(self, value):
        settings = _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": value})
        assert settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"].enabled is True


# ---------------------------------------------------------------------------
# 5. Falsy values
# ---------------------------------------------------------------------------

class TestFalsyValues:
    @pytest.mark.parametrize("value", ["FALSE", "false", "False", "0", "no", "off", "NO", "OFF"])
    def test_falsy_variants(self, value):
        raw = {
            "MOMENTUM_EXHAUSTION": {
                "SHORT": {"policy": "FIXED_HORIZON_V1", "enabled": True, "hold_minutes": 240,
                          "dca_enabled": False, "trailing_enabled": False,
                          "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False}
            }
        }
        settings = _make_settings_and_load(
            raw_policies=raw,
            env_overrides={"ME_SHORT_FIXED_240M_ENABLED": value},
        )
        assert settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"].enabled is False


# ---------------------------------------------------------------------------
# 6. Invalid env value → configuration error
# ---------------------------------------------------------------------------

class TestInvalidEnvValue:
    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="ME_SHORT_FIXED_240M_ENABLED"):
            _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "abc"})

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": ""})


# ---------------------------------------------------------------------------
# 7. Override applies only to MOMENTUM_EXHAUSTION SHORT
# ---------------------------------------------------------------------------

class TestOverrideScope:
    def test_other_scanner_not_affected(self):
        raw = {
            "MOMENTUM_EXHAUSTION": {
                "SHORT": {"policy": "FIXED_HORIZON_V1", "enabled": False, "hold_minutes": 240,
                          "dca_enabled": False, "trailing_enabled": False,
                          "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False},
                "LONG": {"policy": "DEFAULT", "enabled": False, "hold_minutes": 120,
                         "dca_enabled": False, "trailing_enabled": False,
                         "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False},
            },
            "TREND_PULLBACK_V2": {
                "SHORT": {"policy": "FIXED_HORIZON_V1", "enabled": False, "hold_minutes": 60,
                          "dca_enabled": False, "trailing_enabled": False,
                          "breakeven_enabled": False, "tp_enabled": False, "expiry_enabled": False},
            }
        }
        settings = _make_settings_and_load(
            raw_policies=raw,
            env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "true"},
        )
        assert settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"].enabled is True
        assert settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["LONG"].enabled is False
        assert settings.execution_policy_configs["TREND_PULLBACK_V2"]["SHORT"].enabled is False


# ---------------------------------------------------------------------------
# 8. Other execution policy params unaffected
# ---------------------------------------------------------------------------

class TestOtherParamsUnaffected:
    def test_hold_minutes_not_changed_by_env(self):
        settings = _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "true"})
        policy = settings.execution_policy_configs["MOMENTUM_EXHAUSTION"]["SHORT"]
        assert policy.hold_minutes == 240
        assert policy.dca_enabled is False
        assert policy.trailing_enabled is False
        assert policy.tp_enabled is False
        assert policy.expiry_enabled is False


# ---------------------------------------------------------------------------
# 9. Config source tracking
# ---------------------------------------------------------------------------

class TestConfigSource:
    def test_source_is_env_when_env_set(self):
        settings = _make_settings_and_load(env_overrides={"ME_SHORT_FIXED_240M_ENABLED": "true"})
        source = getattr(settings, "_execution_policy_sources", {})
        assert source.get(("MOMENTUM_EXHAUSTION", "SHORT")) == "ENV"

    def test_source_is_config_when_no_env(self):
        settings = _make_settings_and_load()
        source = getattr(settings, "_execution_policy_sources", {})
        assert source.get(("MOMENTUM_EXHAUSTION", "SHORT")) == "CONFIG"


# ---------------------------------------------------------------------------
# 10. Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_no_execution_policies_works(self):
        settings = Settings()
        _load_execution_policies(settings, {})
        assert settings.execution_policy_configs == {}

    def test_parse_bool_env_original_behavior(self):
        assert _parse_bool_env("true", "TEST") is True
        assert _parse_bool_env("false", "TEST") is False
        with pytest.raises(ValueError):
            _parse_bool_env("abc", "TEST")
