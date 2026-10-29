"""Issue #226 review regressions: Windows persistence and corrupt cache taxonomy.

No network, service, or production writes. Only tmp_path cache files.
"""
import json
import os
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.models import Candle
from app.research import htf_baseline_short_phase2_incremental_cache_v1 as cache_module
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    CacheIntegrityError,
    IncrementalPhase2CandleSource,
)


def _source(tmp_path):
    # _read/_write are exercised without a Bybit transport or a lock.
    return object.__new__(IncrementalPhase2CandleSource)


@pytest.mark.parametrize("raw", [
    b"{truncated",
    b"null",
    b"[]",
    b'"string"',
    b"\xff",
    b'{"schema": "HTF_P2_INCREMENTAL_CACHE_V1", "symbol": "BTCUSDT"}',
])
def test_malformed_cache_documents_are_integrity_errors(tmp_path, raw):
    path = tmp_path / "BTCUSDT.json"
    path.write_bytes(raw)
    before = path.read_bytes()
    with pytest.raises(CacheIntegrityError):
        _source(tmp_path)._read(path, "BTCUSDT")
    assert path.read_bytes() == before


@pytest.mark.parametrize("row", [
    [],
    [1, 2, 3],
    [1, 2, 3, 4, 5, 6, 7],
    {"timestamp": 1},
    [None, 2, 3, 4, 5, 6],
    [1, "not_a_number", 3, 4, 5, 6],
])
def test_checksum_valid_malformed_candle_rows_fail_closed(tmp_path, row):
    path = tmp_path / "BTCUSDT.json"
    payload = {"schema": "HTF_P2_INCREMENTAL_CACHE_V1",
               "symbol": "BTCUSDT", "bars": [row]}
    payload["sha256"] = sha256(cache_module._canonical(payload).encode()).hexdigest()
    path.write_text(cache_module._canonical(payload), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(CacheIntegrityError):
        _source(tmp_path)._read(path, "BTCUSDT")
    assert path.read_bytes() == before


def test_windows_save_does_not_call_unavailable_fchmod(tmp_path, monkeypatch):
    """Simulated Windows persistence branch without patching global os.name.

    Do not call the lock here: the Windows lock is separately tested on Windows.
    """
    class WindowsOsProxy:
        name = "nt"

        def __getattr__(self, key):
            if key == "fchmod":
                raise AssertionError("Windows must not request os.fchmod")
            return getattr(os, key)

    monkeypatch.setattr(cache_module, "os", WindowsOsProxy())
    path = tmp_path / "BTCUSDT.json"
    bars = [Candle(300_000, 10, 11, 9, 10, 1)]
    src = _source(tmp_path)
    src._write(path, "BTCUSDT", bars)
    assert src._read(path, "BTCUSDT") == bars


def test_corrupt_cache_does_not_trigger_cold_refetch(tmp_path):
    class Upstream:
        page_limit = 200
        min_history_bars = 12
        load_calls = 0

        def load(self, *args, **kwargs):
            self.load_calls += 1
            raise AssertionError("corrupt cache must not request upstream data")

    up = Upstream()
    src = IncrementalPhase2CandleSource(
        up, tmp_path, max_incremental_bars=10, max_cache_bars=22)
    (tmp_path / "BTCUSDT.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(CacheIntegrityError):
        src.load("BTCUSDT")
    assert up.load_calls == 0
