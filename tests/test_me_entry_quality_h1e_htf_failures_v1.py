"""Issue #324 / H1-E.3 — HTF failure matrix: limiter / 429 / 10006 /
breaker / timeout / API error / retry-budget exhaustion.  Fail closed,
bounded work, ONLY mock transports.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

from app.research.me_entry_quality_h1e_v1.classification import (
    NONELIGIBLE_HTF_API_ERROR,
    NONELIGIBLE_HTF_BREAKER_DENIED,
    NONELIGIBLE_HTF_LIMITER_DENIED,
    NONELIGIBLE_HTF_RATE_LIMITED,
    NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED,
    NONELIGIBLE_HTF_TIMEOUT,
    STATUS_CLASSIFIED,
    STATUS_DEFERRED,
    HtfBreakerDeniedError,
    HtfLimiterDeniedError,
    HtfRateLimitError,
    HtfTimeoutError,
    HtfTransportApiError,
    RetryPolicy,
    classify_h1e_event,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    H1EFreeze,
    admit_source_row,
)
from tests.h1e_v1_fixtures import (
    DAY_MS,
    FIXTURE_ASOF_US,
    FIXTURE_FREEZE_US,
    H4_MS,
    StubTransport,
    make_source_row,
    timeframe_payload,
    zigzag_rows,
)

FREEZE = H1EFreeze.synthetic_fixture(FIXTURE_FREEZE_US)
POLICY = RetryPolicy(max_attempts_per_cycle=2, backoff_base_s=0.5, max_deferrals=2)


def _event():
    decision = admit_source_row(make_source_row(), freeze=FREEZE)
    assert decision.accepted
    return decision.event


def _payload(interval):
    rows = zigzag_rows(
        "BULLISH",
        timeframe_ms=H4_MS if interval == "240" else DAY_MS,
        first_open_ms=FIXTURE_ASOF_US // 1000
        - 60 * (H4_MS if interval == "240" else DAY_MS),
    )
    return timeframe_payload(rows, interval=interval)


def _sleep_recorder():
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
    return sleeps, sleep


def _transport_with_error(exc_factory, *, interval="240", attempts=2):
    """Transport whose ``interval`` fails on EVERY call (last step repeats)."""
    steps = [exc_factory() for _ in range(attempts)]
    return StubTransport({
        "240": steps if interval == "240" else [_payload("240")],
        "D": steps if interval == "D" else [_payload("D")],
    })


def _classify(transport, prior_deferrals=0):
    sleeps, sleep = _sleep_recorder()
    outcome = classify_h1e_event(
        _event(), transport,
        retry_policy=POLICY, sleep=sleep, prior_deferrals=prior_deferrals)
    return outcome, sleeps


# ─── typed failures: bounded deferral then terminal fail-closed ─────────────


def test_limiter_denial_defers_then_exhausts_budget():
    transport = _transport_with_error(
        HtfLimiterDeniedError, attempts=2)  # 2 attempts/cycle all denied
    outcome, sleeps = _classify(transport)
    record = outcome.record
    assert record.status == STATUS_DEFERRED
    assert record.eligible is False
    assert record.non_eligible_codes == (NONELIGIBLE_HTF_LIMITER_DENIED,)
    assert record.combined_regime is None  # never a market mode
    assert record.snapshot_sha256 == ""    # no fabricated provenance
    # bounded work + backoff through the injected sleep only
    # (one backoff BETWEEN the two attempts; never a sleep after the last)
    assert len(transport.calls) == POLICY.max_attempts_per_cycle
    assert sleeps == [0.5]

    # second cycle: still denied → one more bounded deferral
    outcome2, _ = _classify(transport, prior_deferrals=outcome.attempts)
    assert outcome2.record.status == STATUS_DEFERRED
    assert outcome2.attempts == 2

    # third cycle: deferral budget exhausted → terminal fail-closed
    outcome3, _ = _classify(transport, prior_deferrals=outcome2.attempts)
    assert outcome3.record.status == STATUS_CLASSIFIED
    assert not outcome3.record.eligible
    assert outcome3.record.non_eligible_codes == (
        NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED,)
    assert outcome3.deferred_reason == ""


def test_rate_limit_429_and_10006_map_to_explicit_code():
    for detail in ("HTTP 429", "retCode=10006"):
        transport = _transport_with_error(
            lambda d=detail: HtfRateLimitError(d), attempts=2)
        outcome, _ = _classify(transport)
        assert outcome.record.non_eligible_codes == (
            NONELIGIBLE_HTF_RATE_LIMITED,)
        assert detail in outcome.record.eligibility_reason


def test_breaker_denial_defers_with_reason():
    transport = _transport_with_error(HtfBreakerDeniedError, attempts=2)
    outcome, _ = _classify(transport)
    assert outcome.record.status == STATUS_DEFERRED
    assert outcome.record.non_eligible_codes == (
        NONELIGIBLE_HTF_BREAKER_DENIED,)


def test_timeout_defers_then_fails_closed():
    transport = _transport_with_error(HtfTimeoutError, attempts=2)
    outcome, sleeps = _classify(transport)
    assert outcome.record.status == STATUS_DEFERRED
    assert outcome.record.non_eligible_codes == (NONELIGIBLE_HTF_TIMEOUT,)
    assert sleeps  # bounded backoff happened (mock sleep only)


def test_api_error_is_terminal_immediately():
    calls_before = []

    class _Transport(StubTransport):
        def fetch_htf_window(self, **kwargs):
            calls_before.append(kwargs["interval"])
            raise HtfTransportApiError("malformed response")

    outcome, sleeps = _classify(_Transport())
    record = outcome.record
    assert record.status == STATUS_CLASSIFIED
    assert not record.eligible
    assert record.non_eligible_codes == (NONELIGIBLE_HTF_API_ERROR,)
    assert record.combined_regime is None
    assert sleeps == []  # non-retryable: no backoff, no extra attempts
    assert calls_before == ["240"]  # bounded work — stops at first failure


def test_unexpected_exception_is_not_leaked_as_market_mode():
    class _Transport(StubTransport):
        def fetch_htf_window(self, **kwargs):
            raise HtfTransportApiError("boom")

    outcome = classify_h1e_event(_event(), _Transport())
    assert not outcome.record.eligible
    assert outcome.record.regime_4h is None
    assert outcome.record.regime_1d is None


def test_1d_failure_after_4h_success_fails_closed():
    transport = StubTransport({
        "240": [_payload("240")],
        "D": [HtfLimiterDeniedError("limiter"), HtfLimiterDeniedError("limiter")],
    })
    outcome, _ = _classify(transport)
    assert outcome.record.status == STATUS_DEFERRED
    assert outcome.record.non_eligible_codes == (NONELIGIBLE_HTF_LIMITER_DENIED,)
    assert outcome.record.coverage_1d_present == 0  # no fabricated 1D rows


def test_retry_budget_is_bounded_per_cycle():
    transport = _transport_with_error(HtfTimeoutError, attempts=2)
    _classify(transport)
    # exactly max_attempts_per_cycle calls — never unbounded
    assert len(transport.calls) == POLICY.max_attempts_per_cycle
