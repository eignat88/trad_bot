from __future__ import annotations

import pytest

from app.config import load_settings


def test_paper_safety_gate_mode_defaults_to_enforce(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER_SAFETY_GATE_MODE", raising=False)
    settings = load_settings(path=tmp_path / "missing.json", env_file=tmp_path / "missing.env")
    assert settings.paper_safety_gate_mode == "enforce"


def test_paper_safety_gate_mode_reads_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_SAFETY_GATE_MODE", "observe")
    settings = load_settings(path=tmp_path / "missing.json", env_file=tmp_path / "missing.env")
    assert settings.paper_safety_gate_mode == "observe"


@pytest.mark.parametrize(
    ("raw_mode", "expected_mode"),
    ((" OBSERVE ", "observe"), ("EnFoRcE", "enforce"), ("disabled", "disabled")),
)
def test_paper_safety_gate_mode_is_normalized(monkeypatch, tmp_path, raw_mode, expected_mode):
    monkeypatch.setenv("PAPER_SAFETY_GATE_MODE", raw_mode)

    settings = load_settings(path=tmp_path / "missing.json", env_file=tmp_path / "missing.env")

    assert settings.paper_safety_gate_mode == expected_mode


def test_unknown_paper_safety_gate_mode_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_SAFETY_GATE_MODE", "abc")
    with pytest.raises(ValueError, match="PAPER_SAFETY_GATE_MODE"):
        load_settings(path=tmp_path / "missing.json", env_file=tmp_path / "missing.env")
