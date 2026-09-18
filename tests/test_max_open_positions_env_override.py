"""Tests for MAX_OPEN_POSITIONS environment variable override.

Covers:
- Priority: ENV > config.yaml > application default (3)
- Invalid values: zero, negative, non-numeric
"""
from __future__ import annotations

import json

import pytest

from app.config.settings import load_settings


# ---------------------------------------------------------------------------
# 1. Application default — no config, no ENV
# ---------------------------------------------------------------------------

class TestApplicationDefault:
    def test_default_is_three(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
        config = tmp_path / "config.json"
        config.write_text("{}", encoding="utf-8")
        settings = load_settings(path=config, env_file=tmp_path / "missing.env")
        assert settings.max_open_positions == 3


# ---------------------------------------------------------------------------
# 2. config.yaml override
# ---------------------------------------------------------------------------

class TestConfigOverride:
    def test_config_value_used(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"max_open_positions": 6}), encoding="utf-8")
        settings = load_settings(path=config, env_file=tmp_path / "missing.env")
        assert settings.max_open_positions == 6


# ---------------------------------------------------------------------------
# 3. ENV override — the key acceptance test
# ---------------------------------------------------------------------------

class TestEnvOverride:
    def test_env_beats_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "10")
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"max_open_positions": 6}), encoding="utf-8")
        settings = load_settings(path=config, env_file=tmp_path / "missing.env")
        assert settings.max_open_positions == 10

    def test_env_without_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "8")
        config = tmp_path / "config.json"
        config.write_text("{}", encoding="utf-8")
        settings = load_settings(path=config, env_file=tmp_path / "missing.env")
        assert settings.max_open_positions == 8


# ---------------------------------------------------------------------------
# 4. Invalid zero → ValueError
# ---------------------------------------------------------------------------

class TestInvalidZero:
    def test_zero_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "0")
        config = tmp_path / "config.json"
        config.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError):
            load_settings(path=config, env_file=tmp_path / "missing.env")


# ---------------------------------------------------------------------------
# 5. Invalid string → ValueError
# ---------------------------------------------------------------------------

class TestInvalidString:
    def test_non_numeric_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAX_OPEN_POSITIONS", "abc")
        config = tmp_path / "config.json"
        config.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError):
            load_settings(path=config, env_file=tmp_path / "missing.env")
