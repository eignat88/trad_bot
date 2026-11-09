"""Regression for the scramp/pg8000 compatibility pin (VPS incident 2026-11-09)."""
from pathlib import Path

import pytest


def test_requirements_pin_compatible_pg8000_and_scramp():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    lines = {line.strip() for line in requirements.splitlines()}
    assert "pg8000==1.31.5" in lines
    assert "scramp==1.4.18" in lines


def test_installed_scramp_channel_binding_when_driver_is_installed():
    pg8000 = pytest.importorskip("pg8000")
    scramp = pytest.importorskip("scramp")
    assert callable(pg8000.connect)
    assert callable(getattr(scramp, "make_channel_binding", None)), (
        "pg8000 1.31.5 requires scramp.make_channel_binding; "
        "scramp 1.5.0 removed it"
    )
