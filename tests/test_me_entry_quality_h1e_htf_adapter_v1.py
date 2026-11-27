"""Issue #324 / H1-E — C.3 offline tests: HTF transport adapter.

Frozen ASOF semantics, 59/60 coverage verdicts, bounded deferral on
limiter denial / timeout / rate limit / budget, fail-closed on corrupted
data.  ZERO live Bybit requests — every fetch is an injected fake.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h1d_v1.htf_fetch import (
    HtfFetchApiError,
    HtfFetchBreakerDeniedError,
    HtfFetchLimiterDeniedError,
    HtfFetchRateLimitError,
    HtfFetchRequestCapExceeded,
    HtfFetchResult,
    HtfFetchRowConflictError,
    HtfFetchTimeoutError,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    NONELIGIBLE_HTF_API_ERROR,
    NONELIGIBLE_HTF_BREAKER_DENIED,
    NONELIGIBLE_HTF_INCOMPLETE_COVERAGE,
    NONELIGIBLE_HTF_LIMITER_DENIED,
    NONELIGIBLE_HTF_RATE_LIMITED,
    NONELIGIBLE_HTF_TIMEOUT,
    NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED,
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
    RetryPolicy,
    classify_h1e_event,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    FIXTURE_LABEL_SYNTHETIC,
    H1EFreeze,
    admit_source_row,
)
from app.research.me_entry_quality_h1e_v1.htf_transport import (
    H1EHtfApiBudgetExceeded,
    MappedH1EHtfTransport,
    map_h1d_fetch_error,
)
from tests.h1e_v1_fixtures import (
    DAY_MS,
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    H4_MS,
    make_source_row,
    zigzag_rows,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)

STEP_BY_TIMEFRAME = {"4H": H4_MS, "1D": DAY_MS}


def _result(timeframe: str, rows, *, requests_used: int = 1) -> HtfFetchResult:
    return HtfFetchResult(
        timeframe=timeframe,
        interval="240" if timeframe == "4H" else "D",
        symbol="BTCUSDT",
        start_ms=rows[0]["open_ms"] if rows else 0,
        end_ms=rows[-1]["open_ms"] if rows else 0,
        rows=tuple(rows),
        requests_used=requests_used,
        rejected_rows=0,
        deduped_rows=0,
    )


class FakeFetch:
    """Injected fetch double — records calls, serves canned rows."""

    def __init__(self, rows_by_timeframe, *, error: BaseException | None = None):
        self.rows_by_timeframe = rows_by_timeframe
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, client, *, symbol, timeframe, start_ms, end_ms,
                 max_requests, page_size, call_timeout_s):
        self.calls.append({
            "symbol": symbol, "timeframe": timeframe,
            "start_ms": start_ms, "end_ms": end_ms,
            "max_requests": max_requests,
        })
        if self.error is not None:
            raise self.error
        rows = self.rows_by_timeframe[timeframe]
        return _result(timeframe, rows, requests_used=2)


def _zigzag(timeframe: str, *, n: int = 60, extra_future: int = 0):
    step = STEP_BY_TIMEFRAME[timeframe]
    return zigzag_rows(
        "BEARISH" if timeframe == "4H" else "NEUTRAL",
        n=n, timeframe_ms=step,
        # n closed candles ENDING at the ASOF close, then extra FUTURE rows
        first_open_ms=FIXTURE_ASOF_US // 1000 - n * step,
        extra_future_rows=extra_future)


def _event():
    row = make_source_row(
        signal_time_us=FIXTURE_ASOF_US,
        captured_us=FIXTURE_FREEZE_US + 100_000_000,
        candle_open_ms=1_700_000_000_000,
    )
    decision = admit_source_row(row, freeze=FREEZE)
    assert decision.accepted
    return decision.event


def _transport(fetch, **kwargs) -> MappedH1EHtfTransport:
    return MappedH1EHtfTransport(
        client=object(), fixture_label=FIXTURE_LABEL_SYNTHETIC,
        fetch=fetch, **kwargs)


# ─── frozen ASOF semantics ─────────────────────────────────────────────────


def test_window_returns_only_candles_closed_at_the_original_asof():
    rows4 = _zigzag("4H", extra_future=3)
    rows1 = _zigzag("1D", extra_future=3)
    fetch = FakeFetch({"4H": rows4, "1D": rows1})
    transport = _transport(fetch)
    payload = transport.fetch_htf_window(
        symbol="BTCUSDT", interval="240", asof_us=FIXTURE_ASOF_US)
    step = H4_MS
    asof_ms = FIXTURE_ASOF_US // 1000
    assert payload.rows
    # NOTHING closed after the frozen ASOF is ever returned
    assert all(int(r["open_ms"]) + step <= asof_ms for r in payload.rows)
    assert len(payload.rows) == 60  # the 3 future rows were dropped


def test_interval_mapping_and_validation():
    fetch = FakeFetch({"4H": _zigzag("4H"), "1D": _zigzag("1D")})
    transport = _transport(fetch)
    transport.fetch_htf_window(
        symbol="BTCUSDT", interval="240", asof_us=FIXTURE_ASOF_US)
    transport.fetch_htf_window(
        symbol="BTCUSDT", interval="D", asof_us=FIXTURE_ASOF_US)
    assert [c["timeframe"] for c in fetch.calls] == ["4H", "1D"]
    with pytest.raises(ValueError, match="interval"):
        transport.fetch_htf_window(
            symbol="BTCUSDT", interval="60", asof_us=FIXTURE_ASOF_US)
    with pytest.raises(ValueError, match="asof_us"):
        transport.fetch_htf_window(
            symbol="BTCUSDT", interval="240", asof_us=FIXTURE_ASOF_US + 0.5)
    with pytest.raises(ValueError, match="symbol"):
        transport.fetch_htf_window(
            symbol="  ", interval="240", asof_us=FIXTURE_ASOF_US)


def test_htf_59_of_60_is_non_eligible():
    rows4 = _zigzag("4H", n=59)
    rows1 = _zigzag("1D")
    fetch = FakeFetch({"4H": rows4, "1D": rows1})
    outcome = classify_h1e_event(_event(), _transport(fetch))
    record = outcome.record
    assert record.status == STATUS_CLASSIFIED
    assert record.eligible is False
    assert NONELIGIBLE_HTF_INCOMPLETE_COVERAGE in record.non_eligible_codes


def test_future_candles_never_reach_the_classifier():
    rows4 = _zigzag("4H", extra_future=3)
    rows1 = _zigzag("1D", extra_future=3)
    fetch = FakeFetch({"4H": rows4, "1D": rows1})
    outcome = classify_h1e_event(_event(), _transport(fetch))
    record = outcome.record
    # full 60/60 closed coverage at the ORIGINAL ASOF
    assert record.eligible is True
    assert record.asof_us == FIXTURE_ASOF_US
    assert record.coverage_4h_present == 60
    assert record.coverage_1d_present == 60


# ─── bounded deferral / fail-closed mapping ────────────────────────────────


def _classify_with_error(error, *, prior_deferrals=0, policy=None):
    fetch = FakeFetch({"4H": _zigzag("4H"), "1D": _zigzag("1D")},
                      error=error)
    return classify_h1e_event(
        _event(), _transport(fetch),
        retry_policy=policy or RetryPolicy(max_attempts_per_cycle=1),
        prior_deferrals=prior_deferrals)


def test_limiter_denied_defers_bounded_then_terminal():
    outcome = _classify_with_error(
        HtfFetchLimiterDeniedError("shared limiter denied"))
    assert outcome.record.status == STATUS_DEFERRED
    assert list(outcome.record.non_eligible_codes) == [
        NONELIGIBLE_HTF_LIMITER_DENIED]
    # budget spent -> TERMINAL, never endless attempts
    final = _classify_with_error(
        HtfFetchLimiterDeniedError("shared limiter denied"),
        prior_deferrals=3,
        policy=RetryPolicy(max_attempts_per_cycle=1, max_deferrals=3))
    assert final.record.status == STATUS_CLASSIFIED
    assert final.record.eligible is False
    assert list(final.record.non_eligible_codes) == [
        NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED]


def test_timeout_defers_with_explicit_code():
    outcome = _classify_with_error(HtfFetchTimeoutError("timed out"))
    assert outcome.record.status == STATUS_DEFERRED
    assert list(outcome.record.non_eligible_codes) == [NONELIGIBLE_HTF_TIMEOUT]


def test_breaker_and_rate_limit_map_to_explicit_codes():
    breaker = _classify_with_error(HtfFetchBreakerDeniedError("breaker open"))
    assert list(breaker.record.non_eligible_codes) == [
        NONELIGIBLE_HTF_BREAKER_DENIED]
    rate = _classify_with_error(HtfFetchRateLimitError("429"))
    assert list(rate.record.non_eligible_codes) == [NONELIGIBLE_HTF_RATE_LIMITED]


def test_api_budget_bounds_total_requests():
    fetch = FakeFetch({"4H": _zigzag("4H"), "1D": _zigzag("1D")})
    transport = _transport(fetch, request_budget=4)
    transport.fetch_htf_window(
        symbol="BTCUSDT", interval="240", asof_us=FIXTURE_ASOF_US)
    assert transport.requests_used_total == 2
    # second window spends the rest of the budget
    transport.fetch_htf_window(
        symbol="BTCUSDT", interval="D", asof_us=FIXTURE_ASOF_US)
    assert transport.requests_used_total == 4
    # budget exhausted -> bounded deferral (never a limiter bypass)
    with pytest.raises(H1EHtfApiBudgetExceeded):
        transport.fetch_htf_window(
            symbol="BTCUSDT", interval="240", asof_us=FIXTURE_ASOF_US)
    assert len(fetch.calls) == 2  # no API call was made


def test_corrupted_source_fails_closed():
    for error in (
        HtfFetchApiError("api error"),
        HtfFetchRequestCapExceeded("cap"),
        HtfFetchRowConflictError("row conflict"),
    ):
        outcome = _classify_with_error(error)
        assert outcome.record.status == STATUS_CLASSIFIED
        assert outcome.record.eligible is False
        assert list(outcome.record.non_eligible_codes) == [
            NONELIGIBLE_HTF_API_ERROR]


def test_error_map_is_total():
    mapped = map_h1d_fetch_error(RuntimeError("unknown"))
    assert isinstance(mapped, type(map_h1d_fetch_error(HtfFetchApiError("x"))))
    budget = H1EHtfApiBudgetExceeded("spent")
    assert map_h1d_fetch_error(budget) is budget
