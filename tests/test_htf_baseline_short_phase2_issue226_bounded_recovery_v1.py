"""Issue #226 — bounded historical recovery tests for the incremental Phase-2
5m cache (``app.research.htf_baseline_short_phase2_incremental_cache_v1``).

Local fakes only: no network, no database, no production state.  Covers:

* sequential bounded multi-page recovery after a gap larger than one page
  (more than ``max_incremental_bars`` closed 5m candles);
* timestamp continuity and absence of gaps across the cached seam;
* preservation of the causal lookback and the durable resume cursor;
* fail-closed behaviour on incomplete coverage and on exceeding the recovery
  limit, with the persisted cache left byte-identical (atomic-save semantics);
* restart behaviour: the recovered cache is reused without refetch, and a
  restart after a failed recovery completes it without loss or duplication;
* five-symbol API-budget bound for recovery.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BAR_MS,
    BybitCandleSourceError,
)
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    CacheIntegrityError,
    IncrementalPhase2CandleSource,
)

BAR = BAR_MS


class FakeUpstream:
    """Deterministic stand-in for ``BybitPhase2CandleSource`` (same surface)."""

    page_limit = 200
    max_attempts = 3
    min_history_bars = 12

    def __init__(self):
        self.now = BAR * 101
        self.load_calls = 0
        self.page_calls = 0
        self.load_calls_by_symbol = {}
        self.page_calls_by_symbol = {}
        self.page_windows = []
        self.drop_opens = set()          # simulate missing rows in a page
        self.fail_on_page_call = set()   # 1-based global page-call indices that fail
        self.last_evaluation_asof_ms = None
        self.last_server_time_ms = None
        self.last_load_metrics = {'pages_fetched': 1}

    def load(self, symbol, *, resume_open_ms=None):
        self.load_calls += 1
        self.load_calls_by_symbol[symbol] = self.load_calls_by_symbol.get(symbol, 0) + 1
        self.last_evaluation_asof_ms = self.now
        self.last_server_time_ms = self.now
        return [Candle(BAR * i, 10, 11, 9, 10, 1) for i in range(89, 101)]

    def _fetch_server_time_ms(self):
        return self.now

    def _resolve_asof(self, ms):
        return ms

    def _fetch_page(self, symbol, start, end, *, window_end, attempts):
        self.page_calls += 1
        self.page_calls_by_symbol[symbol] = self.page_calls_by_symbol.get(symbol, 0) + 1
        if self.page_calls in self.fail_on_page_call:
            raise BybitCandleSourceError('BYBIT_API_ERROR', 'simulated outage')
        self.page_windows.append((symbol, start, end))
        rows = [[str(t), '10', '11', '9', '10', '1']
                for t in range(start, end + BAR, BAR) if t not in self.drop_opens]
        return rows, self.now

    def _merge_rows(self, symbol, rows, start, end, asof, by_open, unclosed):
        for row in rows:
            t = int(row[0])
            by_open[t] = Candle(t, *map(float, row[1:]))
        return 0


def make_source(up, cache_dir, *, max_incremental_bars=10, max_cache_bars=30,
                max_recovery_bars=None):
    return IncrementalPhase2CandleSource(
        up, cache_dir,
        max_incremental_bars=max_incremental_bars,
        max_cache_bars=max_cache_bars,
        max_recovery_bars=max_recovery_bars,
    )


def assert_contiguous(bars):
    assert all(
        int(bars[i].timestamp) - int(bars[i - 1].timestamp) == BAR
        for i in range(1, len(bars))
    ), 'NON_CONTIGUOUS_TIMESTAMPS'


def test_bounded_recovery_sequential_pages_preserve_lookback(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)          # recovery limit = 30 - 12 = 18 bars
    cold = src.load('BTCUSDT', resume_open_ms=BAR * 100)
    assert len(cold) == 12

    up.now += BAR * 15                       # 15 missing closed bars > one page (10)
    bars = src.load('BTCUSDT', resume_open_ms=BAR * 100)

    assert len(bars) == 27                   # 12 cached + 15 recovered
    assert_contiguous(bars)
    assert [int(b.timestamp) for b in bars[:12]] == [BAR * i for i in range(89, 101)]
    # sequential bounded pages: 10 + 5 bars, exactly one request each
    assert up.page_windows == [
        ('BTCUSDT', BAR * 101, BAR * 110),
        ('BTCUSDT', BAR * 111, BAR * 115),
    ]
    assert src.last_load_metrics['mode'] == 'RECOVERY'
    assert src.last_load_metrics['pages'] == 2
    assert src.last_load_metrics['new_bars'] == 15
    assert src.last_load_metrics['recovered_bars'] == 15
    # the durable resume cursor keeps its FULL causal lookback
    assert int(bars[0].timestamp) <= BAR * 100 - 11 * BAR
    # causal parity: closed window at the new ASOF == baseline suffix
    assert [int(b.timestamp) for b in bars[-12:]] == [BAR * i for i in range(104, 116)]


def test_restart_reuses_recovered_cache_without_refetch(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)
    src.load('BTCUSDT')
    up.now += BAR * 15
    recovered = src.load('BTCUSDT')
    assert up.page_calls == 2

    up2 = FakeUpstream()
    up2.now = up.now
    src2 = make_source(up2, tmp_path)        # process restart, same cache dir
    again = src2.load('BTCUSDT')

    assert up2.page_calls == 0 and up2.load_calls == 0   # nothing refetched
    assert [int(b.timestamp) for b in again] == [int(b.timestamp) for b in recovered]
    assert_contiguous(again)
    assert src2.last_load_metrics['new_bars'] == 0


def test_restart_after_failed_recovery_completes_without_loss(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)
    src.load('BTCUSDT')
    cache = tmp_path / 'BTCUSDT.json'
    before = cache.read_bytes()

    up.now += BAR * 15
    up.fail_on_page_call = {2}               # outage on the 2nd recovery page
    with pytest.raises(BybitCandleSourceError):
        src.load('BTCUSDT')
    assert cache.read_bytes() == before      # atomic save: nothing persisted

    up2 = FakeUpstream()                     # restarted with a healed transport
    up2.now = up.now
    src2 = make_source(up2, tmp_path)
    bars = src2.load('BTCUSDT')

    assert len(bars) == 27 and up2.page_calls == 2
    assert_contiguous(bars)
    assert [int(b.timestamp) for b in bars] == [BAR * i for i in range(89, 116)]
    assert cache.read_bytes() != before


def test_incomplete_coverage_fails_closed_and_recovers_after_heal(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)
    src.load('BTCUSDT')
    cache = tmp_path / 'BTCUSDT.json'
    before = cache.read_bytes()

    up.now += BAR * 15
    up.drop_opens = {BAR * 113}              # a hole inside the second page
    with pytest.raises(CacheIntegrityError, match='coverage'):
        src.load('BTCUSDT')
    assert cache.read_bytes() == before      # cache untouched, cursor not moved
    assert up.page_calls == 2                # bounded: no endless re-requests

    up.drop_opens = set()                    # data gap healed at the source
    bars = src.load('BTCUSDT')
    assert len(bars) == 27 and up.page_calls == 4
    assert_contiguous(bars)


def test_recovery_limit_exceeded_fails_closed_and_preserves_cache(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)          # recovery limit = 30 - 12 = 18 bars
    src.load('BTCUSDT')
    cache = tmp_path / 'BTCUSDT.json'
    before = cache.read_bytes()

    up.now += BAR * 19                       # gap beyond the recovery limit
    with pytest.raises(CacheIntegrityError, match='recovery limit'):
        src.load('BTCUSDT')
    assert cache.read_bytes() == before      # no clobbering on failure
    assert up.page_calls == 0                # no partial download consumed

    with pytest.raises(CacheIntegrityError, match='recovery limit'):
        src.load('BTCUSDT')                  # stable: keeps failing closed
    assert cache.read_bytes() == before

    cache.unlink()                           # manual operator recovery: cold rebuild
    bars = src.load('BTCUSDT')
    assert len(bars) == 12


def test_recovery_never_evicts_resume_cursor_lookback(tmp_path):
    up = FakeUpstream()
    src = make_source(up, tmp_path)          # cache cap 30, limit 18
    src.load('BTCUSDT')
    up.now += BAR * 17
    src.load('BTCUSDT')                      # 29 cached
    up.now += BAR
    src.load('BTCUSDT')                      # 30 cached (cap reached)
    cache = tmp_path / 'BTCUSDT.json'
    before = cache.read_bytes()

    resume = BAR * 100                       # lookback start == cache start
    up.now += BAR * 18                       # exactly at the recovery limit
    with pytest.raises(CacheIntegrityError, match='lookback'):
        src.load('BTCUSDT', resume_open_ms=resume)
    assert cache.read_bytes() == before      # fail closed, never trim the lookback

    bars = src.load('BTCUSDT')               # same recovery without a cursor is fine
    assert len(bars) == 30
    assert int(bars[0].timestamp) == BAR * 107
    assert_contiguous(bars)


def test_five_symbol_recovery_api_budget(tmp_path):
    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT']
    up = FakeUpstream()
    src = make_source(up, tmp_path)

    for s in symbols:                        # cold phase: one delegated window load
        assert len(src.load(s)) == 12
    assert up.page_calls == 0

    up.now += BAR * 15
    budget = 2                               # ceil(15 / max_incremental_bars=10)
    for s in symbols:
        bars = src.load(s)
        assert len(bars) == 27
        assert src.last_load_metrics['mode'] == 'RECOVERY'
    assert up.page_calls == budget * len(symbols)
    assert up.page_calls_by_symbol == {s: budget for s in symbols}
    assert up.load_calls_by_symbol == {s: 1 for s in symbols}

    for s in symbols:                        # steady state: zero extra API pages
        src.load(s)
    assert up.page_calls == budget * len(symbols)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
