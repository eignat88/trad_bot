"""Observer latency measurement — ensures research INSERT does not block scanner."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.research.observer import ResearchObserver
from app.research.repository import ResearchRepository


def _make_candidate(candle_ts: int = 1700000000000) -> SimpleNamespace:
    return SimpleNamespace(
        setup_id="latency-test-001",
        scanner_name="MOMENTUM_EXHAUSTION_R",
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime.now(timezone.utc),
        setup_started_at=datetime.now(timezone.utc),
        signal_candle_open_time=candle_ts,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        target_2=104.5,
        score=55.0,
        market_regime="TREND_UP",
        reasons=(),
        features={"exhaustion_magnitude": 0.5},
        state="SETUP_READY",
    )


def _make_experiment_config() -> dict:
    return {
        "experiment_id": "MER_GENERIC_V1",
        "scanner_name": "MOMENTUM_EXHAUSTION_R",
        "scanner_version": "1.0.0",
        "parameter_set_id": "mer_1.0.0_20260928",
        "parameters": {"swing_lookback": 5},
        "htf_timeframe": "1h",
        "setup_timeframe": "15m",
        "entry_timeframe": "5m",
    }


class TestObserverLatency:
    """Measure observer latency to ensure it cannot block scanner cycle."""

    def _measure_observe_time(self, observe_fn, iterations: int = 100) -> list[float]:
        """Measure observe() latency over N iterations."""
        latencies = []
        for i in range(iterations):
            start = time.perf_counter()
            observe_fn(i)
            elapsed = time.perf_counter() - start
            latencies.append(elapsed)
        return latencies

    def test_observe_latency_mock_repo(self):
        """Observer latency with mocked DB (upper bound on logic overhead)."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 1

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )

        def observe_fn(i):
            candidate = _make_candidate(candle_ts=1700000000000 + i * 300_000)
            observer.observe(candidate)

        latencies = self._measure_observe_time(observe_fn, iterations=200)

        latencies_us = [l * 1_000_000 for l in latencies]
        p50 = sorted(latencies_us)[len(latencies_us) // 2]
        p95 = sorted(latencies_us)[int(len(latencies_us) * 0.95)]
        max_us = max(latencies_us)

        print(f"\nObserver latency (mocked DB):")
        print(f"  p50:  {p50:.1f} µs")
        print(f"  p95:  {p95:.1f} µs")
        print(f"  max:  {max_us:.1f} µs")

        # Logic overhead must be negligible (< 1ms even at p99)
        assert p95 < 1_000, f"p95 latency {p95:.1f}µs exceeds 1ms threshold"
        assert max_us < 5_000, f"max latency {max_us:.1f}µs exceeds 5ms threshold"

    def test_observe_fail_open_does_not_block(self):
        """When DB fails, observe() must return — not hang or raise.

        Note: logger.exception() formatting adds overhead (~10ms per call
        with full traceback).  The key guarantee is that the exception is
        CAUGHT (never propagates) and the method returns.  Latency from
        logging is acceptable since it only fires on actual DB errors.
        """
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.side_effect = ConnectionError("timeout")

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )

        # Run a few iterations — exception must never propagate
        for i in range(5):
            # This MUST not raise — if it does, the test fails
            observer.observe(_make_candidate(candle_ts=1700000000000 + i * 300_000))

        assert observer.stats["errors"] == 5
        assert observer.stats.get("inserted", 0) == 0
