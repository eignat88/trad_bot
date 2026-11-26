"""Issue #322 stable candle identity; no database or network.

The identity lives on the ME_SHORT-scoped adapter
(:class:`ScannerSignalIntegrityObserver`), never on the frozen
:class:`ProspectiveOOSObserver`.
"""

import os
import subprocess
import sys

import pytest

from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)


def key(candle, timeframe="5m"):
    return ScannerSignalIntegrityObserver._make_candle_source_key(
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


def test_symbol_and_direction_are_part_of_identity():
    base = ScannerSignalIntegrityObserver._make_candle_source_key(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    )
    other_symbol = ScannerSignalIntegrityObserver._make_candle_source_key(
        "MOMENTUM_EXHAUSTION", "ETHUSDT", "SHORT", "5m", 1795680000000
    )
    other_direction = ScannerSignalIntegrityObserver._make_candle_source_key(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "LONG", "5m", 1795680000000
    )
    assert base != other_symbol
    assert base != other_direction


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "0"])
def test_missing_candle_fails_closed(invalid):
    assert key(invalid) is None


@pytest.mark.parametrize("bad_tf", [None, "", "   ", 5])
def test_invalid_timeframe_fails_closed(bad_tf):
    assert key(1795680000000, bad_tf) is None


def test_identity_survives_process_restart():
    code = """
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)
print(ScannerSignalIntegrityObserver._make_candle_source_key(
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
            timeout=30,
        )
        results.add(int(completed.stdout.strip()))

    assert len(results) == 1
    assert next(iter(results)) < 0


def test_candle_identity_uses_separate_bigint_range():
    value = key(1795680000000)
    # Below the legacy/HTF 31-bit band, inside a signed PostgreSQL BIGINT.
    assert -(2**63) <= value < -(2**31)
    assert isinstance(value, int)
