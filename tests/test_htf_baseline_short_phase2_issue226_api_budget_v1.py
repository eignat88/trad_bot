"""Issue #226 — deterministic API-budget verification for the incremental cache.

Five symbols (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT) are driven through
cold / steady-state / restart / bounded-recovery / retry / gap-recovery phases
with EXACT request accounting (server-time probes, kline pages, transport
retries, gap-recovery requests) and compared with the in-repo request budget
model of the Issue #181 dry-run (the budget authority artifact
``docs/research/HTF_BASELINE_SHORT_PHASE2_BYBIT_SOURCE_V1_DRY_RUN_REPORT.json``,
``per_load_budget = max_attempts * (1 + max_pages) + max_gap_windows *
gap_recovery_attempts = 3 * 21 + 8 * 2 = 79``).

Every number asserted here is deterministic; nothing depends on wall time.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BAR_MS,
    DEFAULT_GAP_RECOVERY_ATTEMPTS,
    DEFAULT_MAX_GAP_WINDOWS,
    BybitCandleSourceError,
    BybitPhase2CandleSource,
)
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    CacheIntegrityError,
    IncrementalPhase2CandleSource,
)

BAR = BAR_MS
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT"]

MAX_ATTEMPTS = 3
MAX_PAGES = 20
MIN_HISTORY = 3000
PAGE_LIMIT = 200
PER_LOAD_BUDGET = MAX_ATTEMPTS * (1 + MAX_PAGES) + DEFAULT_MAX_GAP_WINDOWS * DEFAULT_GAP_RECOVERY_ATTEMPTS  # 79


class CountingClient:
    """Read-only Bybit v5 fake tallying every HTTP request of one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.server_time_ms = 0
        self.time_requests = 0
        self.kline_requests = 0
        self.retry_attempts = 0          # transport attempts beyond page success
        self.drop_open_ms = None         # simulate a hole in a kline response
        self.always_drop = False         # hole persists across re-requests
        self.fail_next = 0               # transient transport errors to raise
        self._drop_done = False

    def requests(self) -> int:
        return self.time_requests + self.kline_requests

    def _public_get(self, endpoint: str, **params):
        if endpoint == "/v5/market/time":
            self.time_requests += 1
            return {"retCode": 0, "result": {}, "time": self.server_time_ms}
        self.kline_requests += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            self.retry_attempts += 1
            raise RuntimeError("transient transport failure")
        start, end = int(params["start"]), int(params["end"])
        rows = []
        for ts in range(start, end + BAR, BAR):
            if (self.drop_open_ms is not None and ts == self.drop_open_ms
                    and (self.always_drop or not self._drop_done)):
                self._drop_done = True
                continue
            rows.append([str(ts), "100", "101", "99", "100", "1"])
        rows.reverse()
        return {"retCode": 0, "result": {"list": rows}, "time": self.server_time_ms}


def build(symbol: str, cache_dir):
    client = CountingClient(symbol)
    upstream = BybitPhase2CandleSource(
        client, page_limit=PAGE_LIMIT, max_pages=MAX_PAGES,
        min_history_bars=MIN_HISTORY, retry_backoff_s=0, sleep=lambda _s: None,
    )
    cache = IncrementalPhase2CandleSource(
        upstream, cache_dir, max_incremental_bars=200, max_cache_bars=4000,
    )
    return client, upstream, cache


def test_api_budget_five_symbols_deterministic(tmp_path):
    assert PER_LOAD_BUDGET == 79
    measured: dict[str, dict[str, int]] = {}

    sources = {s: build(s, tmp_path) for s in SYMBOLS}

    # ---- phase COLD: 1 server-time + 3000/200 = 15 kline pages per symbol
    for symbol, (client, _up, cache) in sources.items():
        client.server_time_ms = T0
        bars = cache.load(symbol)
        assert len(bars) == MIN_HISTORY
        assert client.time_requests == 1 and client.kline_requests == 15
        assert cache.last_load_metrics["mode"] == "COLD"
        measured[symbol] = {"cold": client.requests()}

    # ---- phase STEADY-STATE: no new bars -> 1 server-time, 0 pages
    for symbol, (client, _up, cache) in sources.items():
        before = client.requests()
        cache.load(symbol)
        assert client.requests() - before == 1
        assert client.kline_requests == 15
        measured[symbol]["steady"] = client.requests() - before

    # ---- phase RESTART: fresh cache instance, no new bars -> 1 server-time
    for symbol, (client, upstream, _cache) in sources.items():
        before = client.requests()
        cache2 = IncrementalPhase2CandleSource(
            upstream, tmp_path, max_incremental_bars=200, max_cache_bars=4000)
        bars = cache2.load(symbol)
        assert len(bars) == MIN_HISTORY
        assert client.requests() - before == 1
        measured[symbol]["restart"] = client.requests() - before

    # ---- phase RECOVERY: 250 missed closed bars -> 2 bounded pages
    for symbol, (client, _up, cache) in sources.items():
        before = client.requests()
        client.server_time_ms = T0 + 250 * BAR
        bars = cache.load(symbol)
        assert len(bars) == MIN_HISTORY + 250
        assert cache.last_load_metrics["mode"] == "RECOVERY"
        assert cache.last_load_metrics["pages"] == 2
        assert client.requests() - before == 3     # 1 time + 2 pages
        assert client.requests() - before < PER_LOAD_BUDGET
        measured[symbol]["recovery"] = client.requests() - before

    # ---- full 5-symbol cycle totals (deterministic)
    cycle_cold = sum(m["cold"] for m in measured.values())
    cycle_steady = sum(m["steady"] for m in measured.values())
    cycle_restart = sum(m["restart"] for m in measured.values())
    cycle_recovery = sum(m["recovery"] for m in measured.values())
    assert cycle_cold == 5 * 16 == 80
    assert cycle_steady == 5
    assert cycle_restart == 5
    assert cycle_recovery == 5 * 3 == 15
    # Issue #181 envelope: budget = 2 loads per symbol * 79
    assert cycle_cold + cycle_restart <= 2 * len(SYMBOLS) * PER_LOAD_BUDGET

    print("API-BUDGET", {
        "per_symbol": measured,
        "cycle_cold": cycle_cold, "cycle_steady": cycle_steady,
        "cycle_restart": cycle_restart, "cycle_recovery": cycle_recovery,
        "per_load_budget": PER_LOAD_BUDGET,
    })


def test_api_budget_retries_and_gap_recovery_bounded(tmp_path):
    # ---- cold with a transient hole: exactly 1 targeted gap-recovery request
    client, upstream, cache = build("BTCUSDT", tmp_path)
    client.server_time_ms = T0
    client.drop_open_ms = T0 - 500 * BAR          # a bar inside the cold window
    bars = cache.load("BTCUSDT")
    assert len(bars) == MIN_HISTORY
    assert client.requests() == 1 + 15 + 1        # 1 time + 15 pages + 1 repair
    assert client.requests() < PER_LOAD_BUDGET

    # ---- transport retries on an incremental page: 2 failures -> 3 tries
    before = client.requests()
    client.server_time_ms = T0 + 5 * BAR
    client.fail_next = 2
    bars = cache.load("BTCUSDT")
    assert len(bars) == MIN_HISTORY + 5
    assert client.requests() - before == 1 + 3    # 1 time + 3 transport tries
    assert client.retry_attempts == 2

    # ---- a hole in an INCREMENTAL page fails closed with no repair loop
    before = client.requests()
    client.server_time_ms = T0 + 10 * BAR
    client.drop_open_ms = T0 + 6 * BAR
    client._drop_done = False
    with pytest.raises(CacheIntegrityError):
        cache.load("BTCUSDT")
    assert client.requests() - before == 1 + 1    # 1 time + 1 page, fail closed

    # ---- an unrecoverable hole on the COLD path stays inside the bounded
    #      gap-recovery budget (max_gap_windows * gap_recovery_attempts = 16)
    client2, upstream2, cache2 = build("ETHUSDT", tmp_path)
    client2.server_time_ms = T0
    client2.drop_open_ms = T0 - 400 * BAR
    client2.always_drop = True
    with pytest.raises(BybitCandleSourceError):
        cache2.load("ETHUSDT")
    assert client2.requests() == 1 + 15 + DEFAULT_GAP_RECOVERY_ATTEMPTS
    assert client2.requests() <= 1 + MAX_PAGES * MAX_ATTEMPTS + \
        DEFAULT_MAX_GAP_WINDOWS * DEFAULT_GAP_RECOVERY_ATTEMPTS


T0 = 1_760_000_000_000 - (1_760_000_000_000 % BAR)
