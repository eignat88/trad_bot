"""Opt-in verified, persistent 5m cache for a Phase-2 Bybit candle source.

Research-only.  Does not alter the existing observer or activate a service.
Initial cold-load delegates to the verified full-window source. Subsequent
calls request only closed bars after the cached tip, then return the complete
causal prefix to the observer. Missing, corrupt, stale or unexpected data fails
closed (never advances a state cursor).

Bounded historical recovery (Issue #226): when more than one page of closed
bars is missing (gap > ``max_incremental_bars``, i.e. > 200 closed 5m candles
with the default page size), the cache catches up by loading the missing
closed bars SEQUENTIALLY through the existing ``BybitPhase2CandleSource``
in bounded pages of at most ``max_incremental_bars`` bars each — at most
``ceil(pending / max_incremental_bars)`` requests per call.  Every page is
verified for exact coverage and the merged window is verified for strict
timestamp continuity across the cached seam.

Recovery is fail-closed end to end:

* an incomplete page, a hole in the merged timestamps or an unclosed bar
  raises :class:`CacheIntegrityError`;
* a gap larger than ``max_recovery_bars`` raises :class:`CacheIntegrityError`
  and needs manual recovery (reviewed cold rebuild);
* the durable resume cursor is never silently moved: its full causal lookback
  must survive recovery, and a recovery that would evict it (e.g. by cache
  trimming) fails closed instead of returning a shifted window;
* any failure leaves the persisted cache file BYTE-IDENTICAL.  The cache is
  saved atomically (temp file + fsync + rename) exactly once, after the
  complete window has been verified, so a process restart retries from the
  same durable tip without loss or duplication.

Concurrency safety: every ``load()`` cycle (read -> fetch -> validate ->
atomic write) runs under a portable per-symbol inter-process lock
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows).  The OS advisory
lock is the single source of truth and dies with its process, so a crashed
writer can never leave a stale lock behind; the lock file's pid/timestamp
payload is advisory diagnostics only.  Lock acquisition is strictly bounded
(``lock_wait_s``) and fails closed on contention — never an infinite wait.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BAR_MS, BybitCandleSourceError, BybitPhase2CandleSource,
)

_SCHEMA = 'HTF_P2_INCREMENTAL_CACHE_V1'


class CacheIntegrityError(RuntimeError):
    """Unsafe cache state. Operator must inspect; do not overwrite automatically."""


class CacheLockError(CacheIntegrityError):
    """Another writer holds this symbol's cache lock (bounded wait exhausted)."""


def _lock_fd(fd: int) -> None:
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class _SymbolLock:
    """Portable inter-process exclusive lock for one symbol's cache cycle.

    The OS advisory lock (``fcntl.flock`` / ``msvcrt.locking``) is the source
    of truth: it is bound to the open handle and released automatically when
    the holding process dies, so crash recovery needs no stale-lock sweep.
    The lock file's pid/timestamp payload is advisory diagnostics only — its
    mere existence never blocks a writer.
    """

    def __init__(self, cache_dir: Path, symbol: str, *, wait_s: float = 0.0):
        self.path = cache_dir / f'.{symbol}.cache.lock'
        self.wait_s = wait_s
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.monotonic() + max(0.0, self.wait_s)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                raise CacheLockError(f'cannot open cache lock: {exc}') from exc
            try:
                _lock_fd(fd)
            except OSError:
                os.close(fd)
                if time.monotonic() >= deadline:
                    raise CacheLockError(
                        f'cache lock busy: {self.path.name} held by another writer'
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                continue
            self._fd = fd
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, f'pid={os.getpid()} ts={int(time.time() * 1000)}'.encode())
                os.fsync(fd)
            except OSError:
                pass  # advisory metadata only; never blocks the cycle
            return

    def release(self) -> None:
        if self._fd is not None:
            try:
                _unlock_fd(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> '_SymbolLock':
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _bar_data(bar: Candle) -> list:
    return [int(bar.timestamp), float(bar.open), float(bar.high), float(bar.low),
            float(bar.close), float(bar.volume)]


def _closed_bars(bars: list[Candle], asof_ms: int) -> list[Candle]:
    bars = sorted(bars, key=lambda c: int(c.timestamp))
    prev = None
    for c in bars:
        ts = int(c.timestamp)
        if ts % BAR_MS or ts + BAR_MS > asof_ms:
            raise CacheIntegrityError('unaligned or unclosed cached candle')
        if prev is not None and ts != prev + BAR_MS:
            raise CacheIntegrityError('duplicate/noncontiguous cached candle')
        if not (float(c.low) <= min(float(c.open), float(c.close)) <=
                max(float(c.open), float(c.close)) <= float(c.high)):
            raise CacheIntegrityError('invalid OHLC in cached candle')
        prev = ts
    return bars


@dataclass
class IncrementalCacheMetrics:
    mode: str = 'UNINITIALIZED'
    new_bars: int = 0
    pages: int = 0
    oldest_open_ms: int | None = None
    latest_open_ms: int | None = None


class IncrementalPhase2CandleSource:
    """Read-only market-data adapter; writes only its private cache file.

    Exactly one writer instance/process per cache_path is required. For V2 use
    an isolated cache path, *never* the existing V1 checkpoint or data path.
    """

    def __init__(self, upstream: BybitPhase2CandleSource, cache_dir: str | os.PathLike,
                 *, max_incremental_bars: int = 200, max_cache_bars: int = 4000,
                 max_recovery_bars: int | None = None, lock_wait_s: float = 0.0):
        if max_incremental_bars < 1 or max_incremental_bars > upstream.page_limit:
            raise ValueError('max_incremental_bars must fit one Bybit page')
        if max_cache_bars < upstream.min_history_bars + max_incremental_bars:
            raise ValueError('max_cache_bars too small for causal history')
        if not 0.0 <= lock_wait_s <= 30.0:
            raise ValueError('lock_wait_s must be bounded within [0, 30]')
        # The recovery limit bounds how many new closed bars one call may
        # absorb: at most the free capacity of the cache window, so the full
        # causal history (min_history_bars) always survives a recovery.
        if max_recovery_bars is None:
            max_recovery_bars = max_cache_bars - upstream.min_history_bars
        if (max_recovery_bars < max_incremental_bars
                or max_recovery_bars > max_cache_bars - upstream.min_history_bars):
            raise ValueError('max_recovery_bars must lie within '
                             '[max_incremental_bars, max_cache_bars - min_history_bars]')
        self.upstream = upstream
        self.cache_dir = Path(cache_dir)
        self.max_incremental_bars = max_incremental_bars
        self.max_cache_bars = max_cache_bars
        self.max_recovery_bars = max_recovery_bars
        self.lock_wait_s = lock_wait_s
        self.last_evaluation_asof_ms: int | None = None
        self.last_server_time_ms: int | None = None
        self.last_load_metrics: dict[str, Any] = {}

    def _path(self, symbol: str) -> Path:
        if not symbol or not symbol.isascii() or not symbol.isalnum() or symbol.upper() != symbol:
            raise ValueError('invalid symbol')
        return self.cache_dir / f'{symbol}.json'

    def _read(self, path: Path, symbol: str) -> list[Candle] | None:
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(obj, dict):
                raise CacheIntegrityError('cache document must be an object')
            if obj.get('schema') != _SCHEMA or obj.get('symbol') != symbol:
                raise CacheIntegrityError('wrong schema or symbol')
            bars_data = obj.get('bars')
            payload = {'schema': obj['schema'], 'symbol': obj['symbol'], 'bars': bars_data}
            if sha256(_canonical(payload).encode()).hexdigest() != obj.get('sha256'):
                raise CacheIntegrityError('cache checksum mismatch')
            if not isinstance(bars_data, list):
                raise CacheIntegrityError('invalid cache bars')
            if any(not isinstance(row, list) or len(row) != 6 for row in bars_data):
                raise CacheIntegrityError('invalid cache candle row shape')
            bars = [Candle(*row) for row in bars_data]
            # Verify calendar continuity using last bar's close as the ASOF.
            if bars:
                _closed_bars(bars, int(bars[-1].timestamp) + BAR_MS)
            return bars
        except CacheIntegrityError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError,
                KeyError, OverflowError, AttributeError) as exc:
            raise CacheIntegrityError(
                f'unreadable or malformed cache document: {type(exc).__name__}'
            ) from exc

    def _write(self, path: Path, symbol: str, bars: list[Candle]):
        payload = {'schema': _SCHEMA, 'symbol': symbol,
                   'bars': [_bar_data(bar) for bar in bars]}
        payload['sha256'] = sha256(_canonical(payload).encode()).hexdigest()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + symbol + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                # mkstemp is exclusive; POSIX requires restrictive mode.\n                # Windows uses inherited directory/file ACLs (no os.fchmod).\n                if os.name == 'posix':\n                    os.fchmod(handle.fileno(), 0o600)
                handle.write(_canonical(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if os.name == 'posix':
                dirfd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _verify_window(self, bars: list[Candle], *, asof_ms: int,
                       resume_open_ms: int | None, end_open: int) -> None:
        """Final fail-closed contract on a window before it is kept or returned.

        Verifies complete causal history and that the durable resume cursor
        with its FULL lookback survives cache trimming — the cursor is never
        silently moved and its causal context is never evicted.
        """
        if not bars:
            raise CacheIntegrityError('empty cache window')
        if len(bars) < self.upstream.min_history_bars:
            raise CacheIntegrityError('incomplete history below min_history_bars')
        first, last = int(bars[0].timestamp), int(bars[-1].timestamp)
        if last + BAR_MS > asof_ms:
            raise CacheIntegrityError('unclosed candle in cache window')
        if (last - first) // BAR_MS + 1 != len(bars):
            raise CacheIntegrityError('noncontiguous cache window')
        if resume_open_ms is not None:
            if resume_open_ms > end_open + BAR_MS:
                raise CacheIntegrityError('resume cursor beyond the closed window')
            need_start = resume_open_ms - (self.upstream.min_history_bars - 1) * BAR_MS
            if first > need_start:
                raise CacheIntegrityError('resume cursor causal lookback evicted by recovery')

    def load(self, symbol: str, *, resume_open_ms: int | None = None) -> list[Candle]:
        path = self._path(symbol)
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The lock spans the WHOLE cycle — read -> fetch -> validate -> atomic
        # write — so concurrent writers serialize instead of interleaving a
        # stale read-modify-write over a newer tip (no lost updates).
        with _SymbolLock(self.cache_dir, symbol, wait_s=self.lock_wait_s):
            return self._load_locked(path, symbol, resume_open_ms)

    def _load_locked(self, path: Path, symbol: str,
                     resume_open_ms: int | None) -> list[Candle]:
        # A cursor older than the cache window MUST NOT be skipped. Fail closed
        # so a separate reviewed historical replay can restore complete context.
        if resume_open_ms is not None and (resume_open_ms <= 0 or resume_open_ms % BAR_MS):
            raise CacheIntegrityError('invalid resume cursor')
        bars = self._read(path, symbol)
        if bars is None:
            fresh = self.upstream.load(symbol, resume_open_ms=resume_open_ms)
            asof = self.upstream.last_evaluation_asof_ms
            if asof is None:
                raise CacheIntegrityError('cold-load missing ASOF')
            fresh = _closed_bars(fresh, asof)
            if len(fresh) < self.upstream.min_history_bars:
                raise CacheIntegrityError('cold-load history incomplete')
            stored = fresh[-self.max_cache_bars:]
            self._verify_window(stored, asof_ms=asof, resume_open_ms=resume_open_ms,
                                end_open=(asof - BAR_MS) // BAR_MS * BAR_MS)
            self._write(path, symbol, stored)
            self.last_evaluation_asof_ms = asof
            self.last_server_time_ms = self.upstream.last_server_time_ms
            self.last_load_metrics = {'mode': 'COLD', 'pages': self.upstream.last_load_metrics.get('pages_fetched'),
                                      'bars_returned': len(fresh), 'new_bars': len(fresh),
                                      'recovered_bars': 0}
            return fresh
        if len(bars) < self.upstream.min_history_bars:
            raise CacheIntegrityError('cache too short; manual recovery required')
        if resume_open_ms is not None and resume_open_ms < int(bars[0].timestamp) + (self.upstream.min_history_bars - 1)*BAR_MS:
            raise CacheIntegrityError('resume cursor lacks full causal lookback')
        now_ms = self.upstream._fetch_server_time_ms()
        asof_ms = self.upstream._resolve_asof(now_ms)
        end_open = (asof_ms - BAR_MS)//BAR_MS*BAR_MS
        tip = int(bars[-1].timestamp)
        if tip > end_open:
            raise CacheIntegrityError('cache ahead of exchange ASOF')
        if resume_open_ms is not None and resume_open_ms > end_open + BAR_MS:
            raise CacheIntegrityError('resume cursor beyond the closed window')
        pending = (end_open - tip)//BAR_MS
        if pending > self.max_recovery_bars:
            raise CacheIntegrityError('recovery limit exceeded; manual recovery required')
        mode = 'RECOVERY' if pending > self.max_incremental_bars else 'INCREMENTAL'
        pages = 0
        if pending:
            # Bounded historical recovery: load the missing closed bars
            # SEQUENTIALLY in bounded pages through the verified upstream
            # source. page_budget bounds the request count; every page must
            # cover its window exactly (no gaps, no duplicates, all closed).
            page_budget = (pending + self.max_incremental_bars - 1)//self.max_incremental_bars
            strict_asof = asof_ms
            extension: list[Candle] = []
            cursor = tip + BAR_MS
            while cursor <= end_open:
                page_end = min(end_open, cursor + (self.max_incremental_bars - 1)*BAR_MS)
                rows, returned_server_ms = self.upstream._fetch_page(
                    symbol, cursor, page_end, window_end=end_open,
                    attempts=self.upstream.max_attempts)
                pages += 1
                if pages > page_budget:
                    raise CacheIntegrityError('recovery page budget exceeded')
                strict_asof = min(strict_asof, returned_server_ms)
                by_open: dict[int, Candle] = {}
                unclosed: set[int] = set()
                self.upstream._merge_rows(symbol, rows, cursor, page_end, strict_asof, by_open, unclosed)
                expected = list(range(cursor, page_end + BAR_MS, BAR_MS))
                if unclosed or set(by_open) != set(expected):
                    raise CacheIntegrityError('incomplete incremental coverage')
                extension.extend(by_open[t] for t in expected)
                cursor = page_end + BAR_MS
            bars = _closed_bars(bars + extension, strict_asof)
            if len(bars) > self.max_cache_bars:
                bars = bars[-self.max_cache_bars:]
            self._verify_window(bars, asof_ms=strict_asof, resume_open_ms=resume_open_ms,
                                end_open=end_open)
            self._write(path, symbol, bars)
        self.last_evaluation_asof_ms = asof_ms
        self.last_server_time_ms = now_ms
        self.last_load_metrics = {'mode': mode, 'pages': pages,
                                  'new_bars': pending, 'bars_returned': len(bars),
                                  'recovered_bars': pending if mode == 'RECOVERY' else 0}
        return bars