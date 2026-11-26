"""Issue #312 — H2 prospective runtime: closed 5m candle adapter (FR-02).

Acceptance criteria covered:

* 5  — an unclosed 5m candle is never used (ASOF truncation);
* 14 — Bybit / API-limit failures fail closed: no partial history, bounded
  retry/backoff, and the production scanner is never affected;
* 17 — an unconfirmed API budget keeps the provider OFF (no request at all);
plus the FR-02 grid/ASOF contracts: UTC 5m grid anchors at microsecond
precision, canonical 120-bucket warmup range, per-cycle / per-hour request
caps and explicit budget accounting.
"""
from __future__ import annotations

import pytest

from app.models import Candle
from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    CandleApiBudget,
    BybitClosedCandleProvider,
    REASON_BUDGET_EXHAUSTED,
    REASON_FETCH_ERROR,
    REASON_OK,
    REASON_PROVIDER_DISABLED,
    observation_grid,
    observation_window_elapsed,
    pages_for_range,
    required_fetch_range,
    rows_as_mappings,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    CANONICAL_LOOKBACK_5M,
    provide_5m_candles,
)

BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MS = CANDLE_5M_MS


def candle_rows(start_bucket: int, count: int, base_ms: int = BASE_MS):
    """A contiguous crafted series of ``count`` closed 5m candles."""
    rows = []
    for i in range(count):
        close = 100.0 + i
        rows.append({
            "open_ms": base_ms + (start_bucket + i) * MS,
            "open": close - 1.0,
            "high": close + 1.0,
            "low": close - 2.0,
            "close": close,
            "volume": 1.0,
        })
    return rows


def as_candles(rows):
    return [
        Candle(int(r["open_ms"]), r["open"], r["high"], r["low"], r["close"],
               r["volume"])
        for r in rows
    ]


class TestObservationGrid:
    def test_signal_mid_bucket_anchors(self):
        # signal inside bucket 18 -> O_0 = 17, O_1 = 18
        signal_us = (BASE_MS + 18 * MS + 60_000) * 1000
        grid = observation_grid(signal_us)
        assert grid.base_open_ms == BASE_MS + 17 * MS
        assert grid.first_obs_open_ms == BASE_MS + 18 * MS

    def test_signal_exactly_at_bucket_close(self):
        # the candle closing exactly at signal_time is O_0 (at-or-before)
        signal_us = (BASE_MS + 19 * MS) * 1000
        grid = observation_grid(signal_us)
        assert grid.base_open_ms == BASE_MS + 18 * MS
        assert grid.first_obs_open_ms == BASE_MS + 19 * MS

    def test_microsecond_precision_no_float_rounding(self):
        # one microsecond before a close boundary must NOT move O_0/O_1
        signal_us = (BASE_MS + 19 * MS) * 1000 - 1
        grid = observation_grid(signal_us)
        assert grid.base_open_ms == BASE_MS + 17 * MS
        assert grid.first_obs_open_ms == BASE_MS + 18 * MS

    def test_observation_opens_are_grid_ordered(self):
        grid = observation_grid((BASE_MS + 18 * MS + 60_000) * 1000)
        assert grid.observation_open_ms(0) == BASE_MS + 18 * MS
        assert grid.observation_open_ms(2) == BASE_MS + 20 * MS

    def test_absurd_instants_fail_closed(self):
        for bad in (0, -1, True, "x", 1.5):
            with pytest.raises(ValueError):
                observation_grid(bad)


class TestWindowElapsed:
    def _grid(self):
        return observation_grid((BASE_MS + 18 * MS + 60_000) * 1000)

    def test_window_not_elapsed_before_last_close(self):
        grid = self._grid()
        # window 3 -> O_3 = bucket 20, closes at BASE + 21*MS
        assert not observation_window_elapsed(
            grid, observation_window=3, evaluation_asof_ms=BASE_MS + 21 * MS - 1
        )

    def test_window_elapsed_exactly_at_last_close(self):
        grid = self._grid()
        assert observation_window_elapsed(
            grid, observation_window=3, evaluation_asof_ms=BASE_MS + 21 * MS
        )


class TestFetchRange:
    def test_canonical_window_is_120_buckets_ending_at_base(self):
        grid = observation_grid((BASE_MS + 18 * MS + 60_000) * 1000)
        start_ms, end_ms = required_fetch_range(
            grid, evaluation_asof_ms=BASE_MS + 25 * MS
        )
        assert start_ms == grid.base_open_ms - (CANONICAL_LOOKBACK_5M - 1) * MS
        assert end_ms == BASE_MS + 24 * MS  # newest bucket closed at ASOF


class TestBudget:
    def test_per_cycle_cap_is_enforced(self):
        budget = CandleApiBudget(max_requests_per_cycle=2, max_requests_per_hour=10)
        budget.start_cycle()
        assert budget.try_consume()
        assert budget.try_consume()
        assert not budget.try_consume()
        assert budget.budget_skips == 1

    def test_hourly_cap_is_enforced(self):
        wall_now = [0.0]
        budget = CandleApiBudget(
            max_requests_per_cycle=100,
            max_requests_per_hour=3,
            wall=lambda: wall_now[0],
            sleep=lambda _s: None,
        )
        budget.start_cycle()
        for _ in range(3):
            assert budget.try_consume()
        assert not budget.try_consume()
        wall_now[0] = 3601.0  # next hour window
        assert budget.try_consume()

    def test_retries_are_counted(self):
        budget = CandleApiBudget(sleep=lambda _s: None)
        budget.note_retry()
        budget.backoff(1)
        assert budget.retries == 1

    # ── review P1-budget: ACTUAL request accounting ────────────────────────

    def test_multi_request_reservation_is_all_or_nothing(self):
        budget = CandleApiBudget(
            max_requests_per_cycle=10, max_requests_per_hour=100
        )
        budget.start_cycle()
        assert budget.try_consume(3)
        assert budget.api_requests == 3
        # a 8-request reservation would exceed the cycle cap -> nothing spent
        assert not budget.try_consume(8)
        assert budget.api_requests == 3
        assert budget.try_consume(7)

    def test_pages_map_to_http_requests(self):
        budget = CandleApiBudget(page_size=1000, requests_per_page=1)
        assert budget.requests_for_pages(1) == 1
        assert budget.requests_for_pages(3) == 3
        conservative = CandleApiBudget(requests_per_page=3)
        assert conservative.requests_for_pages(2) == 6

    def test_hour_window_survives_restart(self):
        # persisted state -> new budget object (process restart) keeps the
        # consumed hourly allowance (review P1-budget restart table)
        wall_now = [1_000.0]
        saved: dict = {}

        budget = CandleApiBudget(
            max_requests_per_cycle=100,
            max_requests_per_hour=5,
            wall=lambda: wall_now[0],
            on_change=lambda state: saved.update(state),
            sleep=lambda _s: None,
        )
        budget.start_cycle()
        for _ in range(4):
            assert budget.try_consume()
        assert saved["requests_in_hour"] == 4

        restarted = CandleApiBudget(
            max_requests_per_cycle=100,
            max_requests_per_hour=5,
            wall=lambda: wall_now[0],
            sleep=lambda _s: None,
        )
        restarted.hydrate(saved)
        restarted.start_cycle()
        assert restarted.try_consume()  # 5th within the hour
        assert not restarted.try_consume()  # hour exhausted -> no request

    def test_persistence_failure_is_fail_closed(self):
        def boom(_state):
            raise RuntimeError("store down")

        budget = CandleApiBudget(on_change=boom, sleep=lambda _s: None)
        budget.start_cycle()
        assert budget.try_consume()  # consumed, but the save failed
        assert budget.persistence_failed
        assert not budget.try_consume()  # fail closed: no unpersisted budget
        assert budget.api_requests == 1


class _ScriptedClient:
    """Fake Bybit client with scripted outcomes per call."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def get_klines_range(self, symbol, interval, *, start_ms, end_ms,
                         page_size, max_pages):
        self.calls.append((symbol, interval, start_ms, end_ms))
        outcome = self._outcomes.pop(0) if self._outcomes else []
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestProvider:
    def _provider(self, client, *, enabled=True, **budget_kwargs):
        budget_kwargs.setdefault("max_retries", 2)
        budget_kwargs.setdefault("request_pause_s", 0.0)
        budget_kwargs.setdefault("retry_backoff_s", 0.0)
        return BybitClosedCandleProvider(
            budget=CandleApiBudget(sleep=lambda _s: None, **budget_kwargs),
            enabled=enabled,
            client_factory=lambda: client,
        )

    def test_disabled_provider_makes_no_request(self):
        # criterion 17 (budget unconfirmed -> provider OFF)
        client = _ScriptedClient([candle_rows(0, 10)])
        provider = self._provider(client, enabled=False)
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert not outcome.ok
        assert outcome.reason == REASON_PROVIDER_DISABLED
        assert client.calls == []
        assert outcome.api_requests == 0

    def test_successful_fetch_builds_verified_identity(self):
        rows = candle_rows(0, 30)
        client = _ScriptedClient([as_candles(rows)])
        provider = self._provider(client)
        provider.budget.start_cycle()
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert outcome.ok and outcome.reason == REASON_OK
        assert outcome.source_identity is not None
        # provenance verification passes against the pure PR #311 provider
        mapped = rows_as_mappings(outcome.rows)
        candle_set = provide_5m_candles(
            mapped,
            evaluation_asof_ms=BASE_MS + 25 * MS,
            source_identity=outcome.source_identity,
        )
        assert candle_set.ok or candle_set.reason in (
            "MISSING_LATEST_CLOSED_CANDLE", "INSUFFICIENT_HISTORY",
            "CANONICAL_WINDOW_INCOMPLETE", "CANDLE_GAP",
        )

    def test_api_failure_is_fail_closed_after_bounded_retries(self):
        # criterion 14: no partial history, bounded retry, explicit reason
        client = _ScriptedClient([RuntimeError("transient network error")] * 5)
        provider = self._provider(client)
        provider.budget.start_cycle()
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert not outcome.ok
        assert outcome.reason == REASON_FETCH_ERROR
        assert outcome.rows == ()
        assert len(client.calls) == 3  # 1 attempt + 2 bounded retries
        assert outcome.retries == 2

    def test_budget_exhaustion_is_fail_closed(self):
        client = _ScriptedClient([as_candles(candle_rows(0, 10))] * 3)
        provider = self._provider(client, max_requests_per_cycle=1)
        provider.budget.start_cycle()
        grid = observation_grid((BASE_MS + 18 * MS + 60_000) * 1000)
        first = provider.fetch_closed_5m(
            symbol="BTCUSDT", grid=grid, evaluation_asof_ms=BASE_MS + 25 * MS
        )
        second = provider.fetch_closed_5m(
            symbol="BTCUSDT", grid=grid, evaluation_asof_ms=BASE_MS + 25 * MS
        )
        assert first.ok
        assert second.ok     # same closed range reused within this cycle
        assert second.api_requests == 0
        assert second.source_identity == first.source_identity
        assert len(client.calls) == 1

        # A new cycle must not reuse old exchange data.
        provider.start_cycle()
        third = provider.fetch_closed_5m(
            symbol="BTCUSDT", grid=grid, evaluation_asof_ms=BASE_MS + 25 * MS
        )
        assert third.ok
        assert len(client.calls) == 2

    def test_empty_payload_is_never_synthesized(self):
        client = _ScriptedClient([[]])
        provider = self._provider(client)
        provider.budget.start_cycle()
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert not outcome.ok
        assert outcome.rows == ()

    def test_multipage_fetch_reserves_every_http_request(self):
        # review P1-budget acceptance: pagination with max_pages > 1 must
        # put ALL pages in the budget before a single request leaves
        client = _ScriptedClient([as_candles(candle_rows(0, 30))] * 5)
        provider = self._provider(
            client, page_size=10, max_pages=20, max_requests_per_cycle=10_000
        )
        provider.budget.start_cycle()
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert outcome.ok
        # the canonical window spans >10 buckets -> multiple pages -> the
        # full pagination cost is in the budget
        assert outcome.api_requests > 1
        assert outcome.api_requests == provider.budget.api_requests

    def test_multipage_budget_exhaustion_blocks_all_requests(self):
        # a range that cannot be paid for makes NO request at all
        client = _ScriptedClient([as_candles(candle_rows(0, 30))] * 5)
        provider = self._provider(client, page_size=10, max_pages=20)
        provider.budget.start_cycle()
        # cycle cap smaller than one full range reservation
        provider.budget.max_requests_per_cycle = 3
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert not outcome.ok
        assert outcome.reason == REASON_BUDGET_EXHAUSTED
        assert client.calls == []

    def test_retry_attempt_is_fully_recounted(self):
        # each retry re-reserves the FULL page count (fail-closed upper bound)
        client = _ScriptedClient(
            [RuntimeError("transient network error"), as_candles(candle_rows(0, 30))]
        )
        provider = self._provider(
            client, page_size=10, max_pages=20, max_requests_per_cycle=10_000
        )
        provider.budget.start_cycle()
        outcome = provider.fetch_closed_5m(
            symbol="BTCUSDT",
            grid=observation_grid((BASE_MS + 18 * MS + 60_000) * 1000),
            evaluation_asof_ms=BASE_MS + 25 * MS,
        )
        assert outcome.ok
        assert outcome.retries == 1
        assert outcome.api_requests == 2 * pages_for_range(
            outcome.requested_range[0], outcome.requested_range[1], 10
        )


class TestClosedCandleContract:
    def test_unclosed_candle_is_dropped_never_used(self):
        # criterion 5: the forming candle at the ASOF is never used
        asof_ms = BASE_MS + 21 * MS  # bucket 20 is closed exactly at ASOF
        rows = candle_rows(0, 21)  # buckets 0..20
        forming = {
            "open_ms": BASE_MS + 21 * MS,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
        }
        identity_rows = rows + [forming]
        from app.research.me_entry_quality_h1c_v1.htf_provider import (
            SOURCE_KIND_OFFLINE_SNAPSHOT,
            build_source_identity,
        )

        identity = build_source_identity(
            SOURCE_KIND_OFFLINE_SNAPSHOT, "crafted", identity_rows
        )
        candle_set = provide_5m_candles(
            identity_rows,
            evaluation_asof_ms=asof_ms,
            source_identity=identity,
            min_closed_candles=15,
            require_latest_closed=True,
        )
        assert len(candle_set.dropped_unclosed) == 1
        assert candle_set.dropped_unclosed[0].timestamp == BASE_MS + 21 * MS
        assert all(c.timestamp <= BASE_MS + 20 * MS for c in candle_set.candles)
