"""Issue #322 stable candle identity; no database or network."""

import os
import subprocess
import sys

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver


def key(candle, timeframe="5m"):
    return ProspectiveOOSObserver._make_candle_source_key(
        "MOMENTUM_EXHAUSTION",
        "BTCUSDT",
        "SHORT",
        timeframe,
        candle,
    )


def test_same_candle_has_same_identity():
    assert key(1795680000000) == key(1795680000000)


def test_new_candle_has_new_identity():
    assert key(1795680000000) != key(1795680300000)


def test_timeframe_is_part_of_identity():
    assert key(1795680000000, "5m") != key(
        1795680000000, "15m"
    )


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "0"])
def test_missing_candle_fails_closed(invalid):
    assert key(invalid) is None


def test_identity_survives_process_restart():
    code = """
from app.research.prospective_observer import ProspectiveOOSObserver
print(ProspectiveOOSObserver._make_candle_source_key(
    "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT",
    "5m", 1795680000000,
))
"""
    results = set()

    for seed in ("1", "2", "3"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        results.add(int(completed.stdout.strip()))

    assert len(results) == 1
    assert next(iter(results)) < 0

def test_candle_identity_uses_separate_bigint_range():
    value = key(1795680000000)
    assert -(2**63) <= value < -(2**31)
