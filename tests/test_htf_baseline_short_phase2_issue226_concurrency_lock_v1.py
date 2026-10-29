"""Issue #226 — concurrency safety for the incremental Phase-2 5m cache.

Covers the per-symbol inter-process lock that spans the WHOLE cache cycle
(read -> fetch -> validate -> atomic write):

* concurrent writers are excluded (in-process and inter-process);
* a crashed writer leaves no stale lock (OS lock dies with the process) and
  the cache file stays SHA-256-intact (atomic save semantics);
* a leftover/stale lock FILE never blocks (metadata is advisory only);
* lock acquisition is strictly bounded — never an infinite wait;
* staggered concurrent writers never lose an update (no stale write may
  clobber a newer cache tip).

Local fakes + real subprocesses only: no network, no DB, no production state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import BAR_MS
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    CacheIntegrityError,
    CacheLockError,
    IncrementalPhase2CandleSource,
    _SymbolLock,
)

BAR = BAR_MS
REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = 1_760_000_000_000 - (1_760_000_000_000 % BAR)   # aligned deterministic base

WORKER = textwrap.dedent("""
    import os, sys, time
    repo_root, cache_dir, now_ms, slow_s, barrier, lock_wait_s = sys.argv[1:7]
    now_ms = int(now_ms); slow_s = float(slow_s); lock_wait_s = float(lock_wait_s)
    sys.path.insert(0, repo_root)
    from app.models import Candle
    from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import BAR_MS
    from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
        CacheIntegrityError, IncrementalPhase2CandleSource)

    BAR = BAR_MS

    class GenUpstream:
        page_limit = 200
        max_attempts = 3
        min_history_bars = 8
        def __init__(self):
            self.now = now_ms
            self.last_evaluation_asof_ms = None
            self.last_server_time_ms = None
            self.last_load_metrics = {'pages_fetched': 1}
        def load(self, symbol, *, resume_open_ms=None):
            if slow_s:
                time.sleep(slow_s)
            self.last_evaluation_asof_ms = self.now
            self.last_server_time_ms = self.now
            end_open = (self.now - BAR) // BAR * BAR
            return [Candle(t, 10, 11, 9, 10, 1)
                    for t in range(end_open - 14 * BAR, end_open + BAR, BAR)]
        def _fetch_server_time_ms(self):
            return self.now
        def _resolve_asof(self, ms):
            return ms
        def _fetch_page(self, symbol, start, end, *, window_end, attempts):
            if slow_s:
                time.sleep(slow_s)
            return ([[str(t), '10', '11', '9', '10', '1']
                     for t in range(start, end + BAR, BAR)], self.now)
        def _merge_rows(self, symbol, rows, start, end, asof, by_open, unclosed):
            for row in rows:
                t = int(row[0])
                by_open[t] = Candle(t, *map(float, row[1:]))
            return 0

    deadline = time.monotonic() + 15
    while not os.path.exists(barrier):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.01)

    src = IncrementalPhase2CandleSource(
        GenUpstream(), cache_dir,
        max_incremental_bars=5, max_cache_bars=30, lock_wait_s=lock_wait_s)
    try:
        src.load('BTCUSDT')
    except CacheIntegrityError as exc:
        print(f'RESULT FAIL_CLOSED {exc}', file=open(result_path := sys.argv[7], 'w'))
        sys.exit(2)
    open(sys.argv[7], 'w').write('RESULT OK')
""")

HOLDER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    repo_root, cache_dir, marker = sys.argv[1:4]
    sys.path.insert(0, repo_root)
    from app.research.htf_baseline_short_phase2_incremental_cache_v1 import _SymbolLock
    lock = _SymbolLock(Path(cache_dir), 'BTCUSDT', wait_s=5.0)
    lock.acquire()
    open(marker, 'w').write('locked')
    time.sleep(120)
""")


class GenUpstream:
    page_limit = 200
    max_attempts = 3
    min_history_bars = 8

    def __init__(self, now_ms, *, slow_s=0.0, hook=None):
        self.now = now_ms
        self.slow_s = slow_s
        self.hook = hook
        self.load_calls = 0
        self.page_calls = 0
        self.last_evaluation_asof_ms = None
        self.last_server_time_ms = None
        self.last_load_metrics = {'pages_fetched': 1}

    def load(self, symbol, *, resume_open_ms=None):
        self.load_calls += 1
        if self.slow_s:
            time.sleep(self.slow_s)
        self.last_evaluation_asof_ms = self.now
        self.last_server_time_ms = self.now
        end_open = (self.now - BAR) // BAR * BAR
        return [Candle(t, 10, 11, 9, 10, 1)
                for t in range(end_open - 14 * BAR, end_open + BAR, BAR)]

    def _fetch_server_time_ms(self):
        return self.now

    def _resolve_asof(self, ms):
        return ms

    def _fetch_page(self, symbol, start, end, *, window_end, attempts):
        self.page_calls += 1
        if self.hook is not None:
            self.hook()
        if self.slow_s:
            time.sleep(self.slow_s)
        return ([[str(t), '10', '11', '9', '10', '1']
                 for t in range(start, end + BAR, BAR)], self.now)

    def _merge_rows(self, symbol, rows, start, end, asof, by_open, unclosed):
        for row in rows:
            t = int(row[0])
            by_open[t] = Candle(t, *map(float, row[1:]))
        return 0


def make_source(up, cache_dir, *, lock_wait_s=0.0):
    return IncrementalPhase2CandleSource(
        up, cache_dir,
        max_incremental_bars=5, max_cache_bars=30, lock_wait_s=lock_wait_s)


def spawn(script_path, *args):
    return subprocess.Popen(
        [sys.executable, str(script_path), *[str(a) for a in args]],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_second_writer_in_same_process_is_excluded(tmp_path):
    first = _SymbolLock(tmp_path, 'BTCUSDT')
    second = _SymbolLock(tmp_path, 'BTCUSDT')
    first.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(CacheLockError):
            second.acquire()
        assert time.monotonic() - t0 < 2.0        # bounded, never waits forever
    finally:
        first.release()
    second.acquire()                              # free again after release
    second.release()


def test_lock_spans_whole_read_fetch_validate_write_cycle(tmp_path):
    """A writer that is MID-FETCH already excludes every other writer."""
    holder_errors: list[Exception] = []

    def reentrant():
        inner = make_source(GenUpstream(T0), tmp_path)
        try:
            inner.load('BTCUSDT')
        except Exception as exc:                  # noqa: BLE001 - recorded for assert
            holder_errors.append(exc)

    make_source(GenUpstream(T0), tmp_path).load('BTCUSDT')   # seed the cache
    up = GenUpstream(T0 + BAR, hook=reentrant)
    src = make_source(up, tmp_path)
    bars = src.load('BTCUSDT')                    # outer incremental cycle succeeds
    assert len(bars) == 16
    assert up.page_calls == 1
    assert len(holder_errors) == 1
    assert isinstance(holder_errors[0], CacheLockError)   # excluded during fetch


def test_stale_lock_file_never_blocks(tmp_path):
    src = make_source(GenUpstream(T0), tmp_path)
    lock_file = tmp_path / '.BTCUSDT.cache.lock'
    lock_file.write_text('pid=999999 ts=1')       # stale metadata from a dead pid
    bars = src.load('BTCUSDT')                    # advisory file must not block
    assert len(bars) == 15


def test_interprocess_exclusion_and_crash_recovery(tmp_path):
    script = tmp_path / 'holder.py'
    script.write_text(HOLDER, encoding='utf-8')
    marker = tmp_path / 'holder.locked'
    proc = spawn(script, REPO_ROOT, tmp_path, marker)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert time.monotonic() < deadline, 'holder never acquired the lock'
            assert proc.poll() is None, f'holder died early rc={proc.poll()}'
            time.sleep(0.01)

        src = make_source(GenUpstream(T0), tmp_path, lock_wait_s=0.5)
        t0 = time.monotonic()
        with pytest.raises(CacheLockError):
            src.load('BTCUSDT')                   # excluded across processes
        assert time.monotonic() - t0 < 10.0       # bounded wait only
    finally:
        proc.kill()
        proc.wait(timeout=15)

    # crash recovery: the OS lock died with the process; the stale lock FILE
    # remains but must not block anything.
    src = make_source(GenUpstream(T0), tmp_path)
    bars = src.load('BTCUSDT')
    assert len(bars) == 15
    assert (tmp_path / '.BTCUSDT.cache.lock').exists()


def test_lock_wait_is_strictly_bounded(tmp_path):
    holder = _SymbolLock(tmp_path, 'BTCUSDT')
    holder.acquire()
    try:
        src = make_source(GenUpstream(T0), tmp_path, lock_wait_s=0.2)
        t0 = time.monotonic()
        with pytest.raises(CacheLockError):
            src.load('BTCUSDT')
        elapsed = time.monotonic() - t0
        assert 0.1 <= elapsed < 5.0               # waited the bound, then failed
    finally:
        holder.release()

    src = make_source(GenUpstream(T0), tmp_path)  # default: immediate fail-closed
    t0 = time.monotonic()
    holder.acquire()
    try:
        with pytest.raises(CacheLockError):
            src.load('BTCUSDT')
        assert time.monotonic() - t0 < 2.0
    finally:
        holder.release()


def test_staggered_writers_never_lose_updates(tmp_path):
    """Stale slow writer may never clobber a newer tip: final tip == newest.

    Interleavings: OLD-first -> both writers commit (tip extends to NEW);
    NEW-first -> OLD fails closed ('cache ahead of exchange ASOF') without
    touching the file. Either way the newest update is never lost.
    """
    for slow_first in (True, False):
        cache_dir = tmp_path / f'case_{slow_first}'
        cache_dir.mkdir()
        barrier = tmp_path / f'barrier_{slow_first}'
        r_old, r_new = tmp_path / f'r_old_{slow_first}', tmp_path / f'r_new_{slow_first}'
        script = tmp_path / 'worker.py'
        script.write_text(WORKER, encoding='utf-8')

        old = spawn(script, REPO_ROOT, cache_dir, T0, 0.6 if slow_first else 0.0,
                    barrier, 10.0, r_old)
        new = spawn(script, REPO_ROOT, cache_dir, T0 + 3 * BAR,
                    0.0 if slow_first else 0.6, barrier, 10.0, r_new)
        barrier.write_text('go')
        for proc in (old, new):
            assert proc.wait(timeout=60) in (0, 2)

        src = make_source(GenUpstream(T0 + 3 * BAR), cache_dir)
        bars = src._read(cache_dir / 'BTCUSDT.json', 'BTCUSDT')   # SHA validated
        assert bars is not None
        assert int(bars[-1].timestamp) == T0 + 2 * BAR             # newest tip wins
        assert len(bars) in (15, 18)                               # by interleaving
        results = (r_old.read_text(), r_new.read_text())
        assert any(r.endswith('OK') for r in results)


def test_sha256_integrity_after_concurrent_hammer(tmp_path):
    script = tmp_path / 'worker.py'
    script.write_text(WORKER, encoding='utf-8')
    barrier = tmp_path / 'barrier'
    procs, results = [], []
    for i in range(4):
        results.append(tmp_path / f'r{i}')
        procs.append(spawn(script, REPO_ROOT, tmp_path, T0 + 2 * BAR, 0.2,
                           barrier, 10.0, results[-1]))
    barrier.write_text('go')
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    cache_file = tmp_path / 'BTCUSDT.json'
    src = make_source(GenUpstream(T0 + 2 * BAR), tmp_path)
    bars = src._read(cache_file, 'BTCUSDT')       # checksum + continuity verified
    assert bars is not None and len(bars) == 15
    assert int(bars[-1].timestamp) == T0 + BAR
    assert not list(tmp_path.glob('*.tmp'))       # no torn write residue
