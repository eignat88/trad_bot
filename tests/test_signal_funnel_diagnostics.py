"""Tests for SIGNAL_FUNNEL_DIAGNOSTICS_V1.

Covers:
  1. FunnelCounters: basic construction and as_dict()
  2. FunnelCollector: increment, get_counters, flush, reset
  3. FunnelCollector: thread-safety (concurrent increments)
  4. FunnelCollector: unknown counter name raises ValueError
  5. V2 _scan_long() integration: funnel counters populated on scan
  6. V2 _scan_long() integration: reject reasons are correct per gate
  7. get_funnel_collector: singleton behavior
  8. reset_all_collectors: clean state
  9. No production behavior change: V2 scan results unchanged
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app.scanners.funnel_diagnostics import (
    FunnelCollector,
    FunnelCounters,
    get_all_collectors,
    get_funnel_collector,
    reset_all_collectors,
)
from app.scanners.momentum_exhaustion_reverse_long_v2 import MomentumExhaustionReverseLongV2Scanner
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


# ── Test helpers ────────────────────────────────────────────────────────

@dataclass
class MockCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0


@dataclass
class MockIndicators:
    rsi: float = 70.0
    atr: float = 100.0
    ma_20: float = 50000.0


@dataclass
class MockMarketContext:
    symbol: str = "BTCUSDT"
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    candles_5m: tuple = ()
    candles_15m: tuple = ()
    candles_1h: tuple = ()
    candles_4h: tuple = ()
    indicators: MockIndicators = field(default_factory=MockIndicators)
    market_regime: str = "TREND_UP"
    levels: object = None


def make_candles_15m(prices: list[float], base_time: datetime = None) -> list[MockCandle]:
    if base_time is None:
        base_time = datetime.now(timezone.utc)
    candles = []
    for i, price in enumerate(prices):
        ts = base_time.replace(minute=i * 15 % 60, hour=(base_time.hour + i * 15 // 60) % 24)
        candles.append(MockCandle(
            timestamp=ts, open=price * 0.999, high=price * 1.001,
            low=price * 0.998, close=price, volume=1000.0,
        ))
    return candles


def make_candles_5m(prices: list[float], base_time: datetime = None) -> list[MockCandle]:
    if base_time is None:
        base_time = datetime.now(timezone.utc)
    candles = []
    for i, price in enumerate(prices):
        ts = base_time.replace(minute=i * 5 % 60, hour=(base_time.hour + i * 5 // 60) % 24)
        candles.append(MockCandle(
            timestamp=ts, open=price * 1.001, high=price * 1.002,
            low=price * 0.998, close=price, volume=1000.0,
        ))
    return candles


# ── FunnelCounters unit tests ──────────────────────────────────────────

class TestFunnelCounters:
    def test_default_values(self):
        c = FunnelCounters()
        assert c.TOTAL_SCANS == 0
        assert c.FINAL_SETUP == 0
        assert c.NO_DATA == 0
        assert c.RSI_DELTA_NOT_POSITIVE == 0

    def test_as_dict_keys(self):
        c = FunnelCounters()
        d = c.as_dict()
        assert "TOTAL_SCANS" in d
        assert "PASS_DATA_LENGTH" in d
        assert "PASS_RSI_DELTA_POSITIVE" in d
        assert "FINAL_SETUP" in d
        assert "NO_DATA" in d
        assert "RSI_DELTA_NOT_POSITIVE" in d
        # Metadata not in as_dict
        assert "period_start" not in d
        assert "period_type" not in d

    def test_total_rejects(self):
        c = FunnelCounters()
        c.NO_DATA = 3
        c.NO_SWINGS = 2
        c.RSI_DELTA_NOT_POSITIVE = 5
        assert c.total_rejects() == 10


# ── FunnelCollector unit tests ──────────────────────────────────────────

class TestFunnelCollector:
    def setup_method(self):
        reset_all_collectors()

    def test_increment_unknown_counter_raises(self):
        col = FunnelCollector("TEST")
        with pytest.raises(ValueError, match="Unknown funnel counter"):
            col.increment("TYPO_COUNTER")

    def test_increment_and_get(self):
        col = FunnelCollector("TEST")
        col.increment("TOTAL_SCANS")
        col.increment("TOTAL_SCANS")
        col.increment("PASS_DATA_LENGTH")
        c = col.get_counters()
        assert c.TOTAL_SCANS == 2
        assert c.PASS_DATA_LENGTH == 1

    def test_get_counters_returns_copy(self):
        col = FunnelCollector("TEST")
        col.increment("TOTAL_SCANS")
        c1 = col.get_counters()
        col.increment("TOTAL_SCANS")
        c2 = col.get_counters()
        assert c1.TOTAL_SCANS == 1
        assert c2.TOTAL_SCANS == 2

    def test_flush_and_get_flushed(self):
        col = FunnelCollector("TEST")
        col.increment("TOTAL_SCANS")
        col.increment("FINAL_SETUP")
        flushed = col.flush("hourly")
        assert flushed.TOTAL_SCANS == 1
        assert flushed.FINAL_SETUP == 1
        assert len(col.get_flushed()) == 1
        # Current counters reset
        c = col.get_counters()
        assert c.TOTAL_SCANS == 0
        assert c.FINAL_SETUP == 0

    def test_clear_flushed(self):
        col = FunnelCollector("TEST")
        col.increment("TOTAL_SCANS")
        col.flush("hourly")
        assert len(col.get_flushed()) == 1
        col.clear_flushed()
        assert len(col.get_flushed()) == 0

    def test_all_time_total(self):
        col = FunnelCollector("TEST")
        col.increment("TOTAL_SCANS")
        col.increment("TOTAL_SCANS")
        assert col.get_all_time_total() == 2
        col.flush("hourly")
        col.increment("TOTAL_SCANS")
        assert col.get_all_time_total() == 3

    def test_concurrent_increments(self):
        """Verify thread-safety: 100 threads each increment 100 times."""
        col = FunnelCollector("CONCURRENT_TEST")
        n_threads = 100
        n_increments = 100

        def worker():
            for _ in range(n_increments):
                col.increment("TOTAL_SCANS")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert col.get_counters().TOTAL_SCANS == n_threads * n_increments

    def test_summary_text(self):
        col = FunnelCollector("SUMMARY_TEST")
        col.increment("TOTAL_SCANS")
        col.increment("PASS_DATA_LENGTH")
        col.increment("NO_DATA")
        text = col.summary_text()
        assert "SUMMARY_TEST" in text
        assert "TOTAL_SCANS" in text
        assert "NO_DATA" in text


# ── Global registry tests ──────────────────────────────────────────────

class TestFunnelRegistry:
    def setup_method(self):
        reset_all_collectors()

    def test_singleton_behavior(self):
        c1 = get_funnel_collector("V2")
        c2 = get_funnel_collector("V2")
        assert c1 is c2

    def test_different_scanners_different_collectors(self):
        c1 = get_funnel_collector("V1")
        c2 = get_funnel_collector("V2")
        assert c1 is not c2

    def test_get_all_collectors(self):
        get_funnel_collector("A")
        get_funnel_collector("B")
        all_cols = get_all_collectors()
        assert "A" in all_cols
        assert "B" in all_cols

    def test_reset_all(self):
        get_funnel_collector("X").increment("TOTAL_SCANS")
        reset_all_collectors()
        # After reset, a new collector is created
        c = get_funnel_collector("X")
        assert c.get_counters().TOTAL_SCANS == 0


# ── V2 Scanner integration tests ───────────────────────────────────────

class TestV2FunnelIntegration:
    """Verify that V2 _scan_long() correctly populates funnel counters."""

    def setup_method(self):
        reset_all_collectors()

    def _make_empty_context(self) -> MockMarketContext:
        return MockMarketContext(candles_15m=(), candles_5m=())

    def _make_short_data_context(self) -> MockMarketContext:
        """Context with data length < 30 on 15m."""
        prices_5m = [100.0] * 20
        return MockMarketContext(
            candles_15m=tuple(make_candles_15m([100.0] * 10)),
            candles_5m=tuple(make_candles_5m(prices_5m)),
        )

    def test_empty_data_counts_no_data(self):
        scanner = MomentumExhaustionReverseLongV2Scanner()
        ctx = self._make_empty_context()
        scanner.scan(ctx)

        funnel = get_funnel_collector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")
        c = funnel.get_counters()
        assert c.TOTAL_SCANS == 1
        assert c.NO_DATA == 1
        assert c.FINAL_SETUP == 0

    def test_short_data_counts_no_data(self):
        scanner = MomentumExhaustionReverseLongV2Scanner()
        ctx = self._make_short_data_context()
        scanner.scan(ctx)

        funnel = get_funnel_collector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")
        c = funnel.get_counters()
        assert c.TOTAL_SCANS == 1
        assert c.NO_DATA == 1
        assert c.PASS_DATA_LENGTH == 0

    def test_multiple_scans_accumulate(self):
        scanner = MomentumExhaustionReverseLongV2Scanner()
        for _ in range(5):
            scanner.scan(self._make_empty_context())

        funnel = get_funnel_collector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")
        c = funnel.get_counters()
        assert c.TOTAL_SCANS == 5
        assert c.NO_DATA == 5

    def test_scan_result_unchanged_with_diagnostics(self):
        """Verify that adding diagnostics doesn't change scan output."""
        scanner = MomentumExhaustionReverseLongV2Scanner()

        # Empty context → no setup (same as before diagnostics)
        ctx = self._make_empty_context()
        result = scanner.scan(ctx)
        assert result == []

    def test_funnel_collector_created_for_scanner(self):
        scanner = MomentumExhaustionReverseLongV2Scanner()
        scanner.scan(self._make_empty_context())

        collectors = get_all_collectors()
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2" in collectors

    def test_reject_reason_only_one_per_scan(self):
        """Each scan should produce exactly one reject reason (first-match)."""
        scanner = MomentumExhaustionReverseLongV2Scanner()
        scanner.scan(self._make_empty_context())

        funnel = get_funnel_collector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")
        c = funnel.get_counters()
        # Only NO_DATA should be non-zero among reject reasons
        reject_counts = [
            c.NO_DATA, c.NO_SWINGS, c.NO_BREAKOUT,
            c.TOO_FAR_ABOVE_PREV_HIGH, c.NOT_BEARISH,
            c.BODY_TOO_LARGE, c.RSI_BELOW_65,
            c.RSI_DELTA_MISSING, c.RSI_DELTA_NOT_POSITIVE,
        ]
        non_zero = sum(1 for x in reject_counts if x > 0)
        assert non_zero == 1, f"Expected exactly 1 reject reason, got {non_zero}"

    def test_no_production_behavior_change(self):
        """V2 scanner source must still contain rsi_delta_3 <= 0 check."""
        import inspect
        source = inspect.getsource(MomentumExhaustionReverseLongV2Scanner)
        # Original production logic preserved
        assert "rsi_delta_3 <= 0" in source
        assert "sl_pct = 0.025" in source
        assert "tp_pct = 0.03" in source
        # Diagnostics added (not replacing anything)
        assert "funnel.increment" in source
        assert "get_funnel_collector" in source

    def test_v2_no_shadow_dependency(self):
        """V2 must not depend on shadow engine (paper/live)."""
        import inspect
        source = inspect.getsource(MomentumExhaustionReverseLongV2Scanner)
        assert "ShadowPaperEngine" not in source
        assert "ShadowTradeEngine" not in source
        assert "shadow_engine" not in source
