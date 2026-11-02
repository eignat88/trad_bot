"""Issue #246: cycle-local validated historical page reuse, offline only."""
from __future__ import annotations

from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    BybitHistoricalCandleSource,
    SOURCE_INCOMPLETE_COVERAGE,
    SOURCE_VALIDATED,
)

STEP = 300_000


class FakeBybit:
    def __init__(self, n=450, *, gap=None):
        self.rows = [
            [str(i * STEP), "100", "102", "98", "101", "1", "0"]
            for i in range(n) if i != gap
        ]
        self.calls = []

    def _public_get(self, endpoint, **kwargs):
        assert endpoint == "/v5/market/kline"
        assert kwargs["category"] == "linear"
        assert kwargs["interval"] == "5"
        self.calls.append((kwargs["symbol"], kwargs["end"], kwargs["limit"]))
        eligible = [row for row in self.rows if int(row[0]) <= kwargs["end"]]
        return {"result": {"list": list(reversed(eligible[-kwargs["limit"]:]))}}


def test_two_observations_identical_pages_parity_and_request_saving():
    asof = 451 * STEP
    fake = FakeBybit()
    source = BybitHistoricalCandleSource(fake)
    baseline1 = source.fetch_5m_candles("ZROUSDT", 0, 449 * STEP, asof)
    baseline2 = source.fetch_5m_candles("ZROUSDT", 0, 449 * STEP, asof)
    assert baseline1.status == baseline2.status == SOURCE_VALIDATED
    baseline_calls = len(fake.calls)
    assert baseline_calls == 3 * 2

    fake.calls.clear()
    source.begin_cycle()
    try:
        one = source.fetch_5m_candles("ZROUSDT", 0, 449 * STEP, asof)
        two = source.fetch_5m_candles("ZROUSDT", 0, 449 * STEP, asof)
        assert one == baseline1
        assert two == baseline2
        assert len(fake.calls) == 3
        assert source.cycle_request_metrics == {"cache_hits": 3, "http_calls": 3}
    finally:
        source.end_cycle()


def test_overlapping_intervals_with_same_page_end_reuse_pages():
    fake = FakeBybit()
    source = BybitHistoricalCandleSource(fake)
    source.begin_cycle()
    try:
        first = source.fetch_5m_candles("ZROUSDT", 0, 449 * STEP, 451 * STEP)
        second = source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 451 * STEP)
        assert first.status == second.status == SOURCE_VALIDATED
        assert len(second.candles) == 200
        assert source.cycle_request_metrics["cache_hits"] >= 1
    finally:
        source.end_cycle()


def test_new_cycle_and_asof_do_not_reuse_old_pages():
    fake = FakeBybit()
    source = BybitHistoricalCandleSource(fake)
    source.begin_cycle()
    source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 451 * STEP)
    first = len(fake.calls)
    source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 452 * STEP)
    assert len(fake.calls) == first + 1
    source.end_cycle()
    source.begin_cycle()
    source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 451 * STEP)
    assert len(fake.calls) == first + 2
    source.end_cycle()


def test_incomplete_interval_not_cached():
    fake = FakeBybit(gap=300)
    source = BybitHistoricalCandleSource(fake)
    source.begin_cycle()
    try:
        first = source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 451 * STEP)
        n = len(fake.calls)
        second = source.fetch_5m_candles("ZROUSDT", 250 * STEP, 449 * STEP, 451 * STEP)
        assert first.status == second.status == SOURCE_INCOMPLETE_COVERAGE
        assert len(fake.calls) == n * 2
        assert source.cycle_request_metrics["cache_hits"] == 0
    finally:
        source.end_cycle()
