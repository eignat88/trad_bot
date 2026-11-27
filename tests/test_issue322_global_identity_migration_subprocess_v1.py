"""Issue #322 global identity migration — cross-process determinism (offline).

Spawns real subprocesses with DIFFERENT ``PYTHONHASHSEED`` values and asserts
the canonical key is identical in every process, while the legacy ``hash()``
key is not — proving the migration key is restart-stable and that the legacy
identity is what breaks under rescan + restart.  No database, no network.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    legacy_source_signal_id,
    stable_source_signal_id,
)

ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import json
from app.research.scanner_signal_identity_migration_v1 import (
    legacy_source_signal_id,
    stable_source_signal_id,
)
out = {
    "srr": stable_source_signal_id(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", 1795680000000),
    "me_long": stable_source_signal_id(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "LONG", "5m", 1795680000000),
    "fvg": stable_source_signal_id(
        "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "BTCUSDT", "LONG", "5m", 1795680000000),
    "legacy": legacy_source_signal_id(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", 1795680000000),
}
print(json.dumps(out))
"""


def _probe(seed: str) -> dict:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    import json

    return json.loads(completed.stdout.strip())


@pytest.fixture(scope="module")
def probes() -> dict:
    return {seed: _probe(seed) for seed in ("1", "2", "3")}


def test_stable_key_identical_across_pythonhashseeds(probes):
    for family in ("srr", "me_long", "fvg"):
        values = {probes[seed][family] for seed in probes}
        assert len(values) == 1, f"{family} unstable across processes: {values}"
        assert next(iter(values)) < -(2**31)


def test_stable_key_matches_in_process_derivation(probes):
    in_process = stable_source_signal_id(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    )
    assert {probes[seed]["srr"] for seed in probes} == {in_process}


def test_legacy_key_is_process_dependent(probes):
    # The frozen hash()-based identity differs per PYTHONHASHSEED — this is
    # the instability the migration removes.  (Three independent 31-bit
    # values colliding would be a ~2**-62 accident.)
    values = {probes[seed]["legacy"] for seed in probes}
    assert len(values) > 1, f"legacy key unexpectedly stable: {values}"


def test_legacy_key_band_disjoint_from_stable_band(probes):
    for seed in probes:
        assert 0 <= probes[seed]["legacy"] < 2**31
        assert probes[seed]["srr"] < -(2**31)


def test_same_candle_same_identity_within_process():
    # Same candle -> byte-identical derivation within one process (the frozen
    # legacy formula is deterministic for a fixed PYTHONHASHSEED).
    value = legacy_source_signal_id("X", "Y", "SHORT", 1)
    assert value == legacy_source_signal_id("X", "Y", "SHORT", 1)
    assert value == -abs(hash("X:Y:SHORT:1")) % (2**31)
    assert stable_source_signal_id("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 5) == \
        stable_source_signal_id("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 5)
