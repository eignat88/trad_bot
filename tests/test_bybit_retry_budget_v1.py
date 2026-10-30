"""BYBIT_RETRY_BUDGET_HARDENING_V1 tests — Issue #229.

Acceptance-criteria coverage:

1. property test: for ANY error sequence the total HTTP attempts per logical
   request are <= 3 — and fail-closed outcomes are never retried (no L3×L2
   nesting anywhere, incl. static source contracts in the style of
   ``tests/test_shadow_cli.py``);
2. classification matrix end-to-end: 10006 → retryable within cap; other
   retCode → no retry; 403 / "access too frequent" → fail-closed raise;
   HTTP 429 → treated as rate limit;
3. backoff: exponential, bounded, non-decreasing per sequence (the frozen
   scanner timeout schedule [1, 2] is preserved bit-for-bit; jitter on the
   rate-limit class);
4. storm guard: retry share per window counted and capped; breaker-open
   policy — P3 defers the cycle, P1/P2 wait, P0 retries only after the
   cooldown;
5. ``attempt_index`` / ``retry_reason`` / ``total_attempts`` visible in
   BYBIT_API_TELEMETRY_V1 records (parity: one record per HTTP attempt);
6. regression semantics preserved inside the new budget (timeout retries,
   concise exhaustion errors);
7. POST create_order: single attempt, never retried, and identical
   re-submission after an indeterminate outcome is blocked fail-closed.
"""
from __future__ import annotations

import itertools
import logging
from pathlib import Path
from urllib.error import URLError

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import Settings
from app.exchange import retry_budget
from app.exchange.bybit_client import (
    BybitClient,
    BybitError,
    BybitOrderUncertainError,
    BybitRateLimitError,
    BybitTimeoutError,
    _RateLimiter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status=200, http_error=False):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []},
        }
        self.status = status
        self.headers = None
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error or not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class BadJsonResponse:
    status = 200
    headers = None

    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class ScriptedSession:
    """Replays scripted outcomes; falls back to an OK response when empty."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {}), timeout))
        return self._next()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, timeout))
        return self._next()

    def _next(self):
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClock:
    """Controlled clock: sleep() advances time deterministically."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def make_client(session, **overrides):
    defaults = dict(
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=2,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
    )
    defaults.update(overrides)
    return BybitClient(Settings(**defaults), session)


def live_settings(**overrides):
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        live_safety_ready=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def fast_limiter(monkeypatch):
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)


@pytest.fixture
def telemetry_env(monkeypatch, caplog):
    monkeypatch.setattr(bybit_client_module, "_telemetry", None)
    caplog.set_level(logging.INFO)
    yield


def attempt_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("BYBIT_API_CALL")]


def field(line, name):
    for token in line.split():
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return ""


# ----------------------------------------------------------------------
# 1. Unified attempt budget <= 3 per logical request (property test)
# ----------------------------------------------------------------------

RETRYABLE_KINDS = ("timeout", "rate_limit", "http429")
FAIL_CLOSED_KINDS = (
    "api_error", "access_denied", "http403", "http500", "transport", "payload",
)
ALL_KINDS = RETRYABLE_KINDS + FAIL_CLOSED_KINDS


def outcome(kind):
    if kind == "timeout":
        return TimeoutError("slow")
    if kind == "rate_limit":
        return FakeResponse({"retCode": 10006, "retMsg": "Too many visits"})
    if kind == "http429":
        return FakeResponse(status=429)
    if kind == "api_error":
        return FakeResponse({"retCode": 10001, "retMsg": "params error"})
    if kind == "access_denied":
        return FakeResponse(
            {"retCode": 0, "retMsg": "access too frequent", "result": {}})
    if kind == "http403":
        return FakeResponse(status=403)
    if kind == "http500":
        return FakeResponse(status=500)
    if kind == "transport":
        return URLError(ConnectionRefusedError(111, "refused"))
    if kind == "payload":
        return BadJsonResponse()
    raise AssertionError(kind)


def test_property_total_http_attempts_per_logical_request_le_3(no_sleep):
    """For ANY error sequence: <= 3 HTTP attempts; fail-closed = 1 attempt."""
    checked = 0
    for length in (1, 2, 3):
        for seq in itertools.product(ALL_KINDS, repeat=length):
            retry_budget.reset_process_state()
            session = ScriptedSession([outcome(kind) for kind in seq])
            client = make_client(session)
            try:
                client.get_klines("BTCUSDT")
            except Exception:
                pass  # outcome type is checked in the classification matrix
            assert len(session.calls) <= 3, (seq, len(session.calls))
            if seq[0] in FAIL_CLOSED_KINDS:
                assert len(session.calls) == 1, seq
            checked += 1
    assert checked == 9 + 81 + 729


def test_budget_hard_cap_overrides_aggressive_settings(no_sleep):
    """bybit_max_attempts / rate-limit retries can never exceed the hard cap."""
    session = ScriptedSession([outcome("rate_limit")] * 10)
    client = make_client(
        session, bybit_max_attempts=10, bybit_rate_limit_max_retries=10)
    with pytest.raises(BybitRateLimitError):
        client.get_klines("BTCUSDT")
    assert len(session.calls) == 3


def test_all_timeouts_raise_concise_budget_error(no_sleep):
    session = ScriptedSession([outcome("timeout")] * 10)
    client = make_client(session)
    with pytest.raises(BybitTimeoutError, match="BTCUSDT.*3 attempts"):
        client.get_klines("BTCUSDT")
    assert len(session.calls) == 3


# ----------------------------------------------------------------------
# 2. Classification matrix (end-to-end)
# ----------------------------------------------------------------------

def test_10006_is_retryable_within_cap(no_sleep):
    session = ScriptedSession([outcome("rate_limit"), FakeResponse()])
    candles = make_client(session).get_klines("BTCUSDT")
    assert candles == []                  # logical request SUCCEEDED on retry
    assert len(session.calls) == 2


def test_other_retcode_never_retries(no_sleep):
    session = ScriptedSession([outcome("api_error")])
    with pytest.raises(BybitError, match="params error"):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1


def test_http_403_fails_closed_no_retry(no_sleep):
    session = ScriptedSession([outcome("http403")])
    with pytest.raises(BybitError, match="HTTP 403"):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1


def test_access_too_frequent_fails_closed_no_retry(no_sleep):
    """ACCESS_DENIED wins even over the 10006 rate-limit code."""
    session = ScriptedSession([
        FakeResponse({"retCode": 10006, "retMsg": "access too frequent",
                      "result": {}}),
    ])
    with pytest.raises(BybitError, match="access too frequent"):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1


def test_http_429_is_treated_as_rate_limit(no_sleep):
    session = ScriptedSession([outcome("http429"), FakeResponse()])
    candles = make_client(session).get_klines("BTCUSDT")
    assert candles == []                  # succeeded via a rate-limit retry
    assert len(session.calls) == 2


def test_rate_limit_retry_cap_per_priority_class(no_sleep):
    """P3 carries the smallest rate-limit retry cap (1) and is not retried
    more than its cap, while the budget would allow more."""
    session = ScriptedSession([outcome("rate_limit")] * 5)
    client = make_client(session, bybit_rate_limit_max_retries=5)
    with pytest.raises(BybitRateLimitError, match="rate-limit retry cap"):
        client._public_get("/v5/market/kline", retry_priority="P3",
                           symbol="BTCUSDT")
    # 1 initial + 1 rate-limit retry (P3 cap) = 2 attempts
    assert len(session.calls) == 2


# ----------------------------------------------------------------------
# 3. Backoff: exponential + jitter, bounded, non-decreasing
# ----------------------------------------------------------------------

def test_timeout_schedule_preserved_bit_for_bit(monkeypatch):
    """The frozen scanner regression sleep contract [1, 2] survives."""
    sleeps = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", sleeps.append)
    session = ScriptedSession(
        [outcome("timeout"), outcome("timeout"), FakeResponse()])
    client = make_client(session, bybit_retry_backoff=1.0)
    client.get_klines("VIRTUALUSDT")
    assert sleeps == [1, 2]


def test_rate_limit_backoff_exponential_jittered_bounded(monkeypatch):
    sleeps = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", sleeps.append)
    session = ScriptedSession(
        [outcome("rate_limit"), outcome("rate_limit"), FakeResponse()])
    client = make_client(session,
                         bybit_rate_limit_retry_backoff=1.0,
                         bybit_rate_limit_max_retries=3)
    client.get_klines("BTCUSDT")
    assert len(sleeps) == 2
    first, second = sleeps
    assert 1.0 <= first <= 1.5            # base * 2^0 + jitter(0..50%)
    assert 2.0 <= second <= 3.0           # base * 2^1 + jitter(0..50%)
    assert second >= first                # non-decreasing per sequence
    assert second <= 30.0                 # bounded


def test_mixed_sequence_backoff_is_non_decreasing(monkeypatch):
    sleeps = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", sleeps.append)
    session = ScriptedSession(
        [outcome("timeout"), outcome("rate_limit"), FakeResponse()])
    client = make_client(session,
                         bybit_retry_backoff=1.0,
                         bybit_rate_limit_retry_backoff=0.1)
    client.get_klines("BTCUSDT")
    assert len(sleeps) == 2
    assert sleeps[0] == 1.0
    assert sleeps[1] >= sleeps[0]         # floored at the previous delay


# ----------------------------------------------------------------------
# 4. Storm guard + breaker (plan §3.2 / AC4)
# ----------------------------------------------------------------------

def test_storm_guard_caps_absolute_retries_per_window():
    guard = retry_budget.RetryStormGuard(
        window_s=60.0, max_retries=2, max_share=1.0, min_sample=1)
    guard.note_attempt(is_retry=True)
    assert guard.allow_retry()
    guard.note_attempt(is_retry=True)
    assert not guard.allow_retry()        # 3rd retry exceeds the cap


def test_storm_guard_caps_retry_share_per_window():
    guard = retry_budget.RetryStormGuard(
        window_s=60.0, max_retries=1000, max_share=0.5, min_sample=1)
    for _ in range(6):
        guard.note_attempt(is_retry=False)
    for _ in range(4):
        guard.note_attempt(is_retry=True)
    assert guard.allow_retry()            # projected 5/11 <= 50%
    guard.note_attempt(is_retry=True)
    assert guard.allow_retry()            # projected 6/12 <= 50%
    guard.note_attempt(is_retry=True)
    assert not guard.allow_retry()        # projected 7/13 > 50%


def test_storm_guard_window_expiry_allows_retries_again(monkeypatch):
    import time as real_time
    guard = retry_budget.RetryStormGuard(
        window_s=0.05, max_retries=1, max_share=1.0, min_sample=1)
    guard.note_attempt(is_retry=True)
    assert not guard.allow_retry()
    real_time.sleep(0.1)
    assert guard.allow_retry()


def test_storm_guard_settings_suppress_retries(no_sleep):
    """Guard caps are wired from Settings: zero tolerance → fail-closed."""
    session = ScriptedSession([outcome("rate_limit"), FakeResponse()])
    client = make_client(session, bybit_retry_storm_max_retries=0)
    with pytest.raises(BybitRateLimitError, match="storm guard"):
        client.get_klines("BTCUSDT")
    assert len(session.calls) == 1        # retry suppressed before sending


def test_breaker_open_p3_defers_cycle_without_http(no_sleep):
    retry_budget.get_breaker().trip(30.0)
    session = ScriptedSession([FakeResponse()])
    with pytest.raises(BybitRateLimitError, match="deferred"):
        make_client(session)._public_get(
            "/v5/market/kline", retry_priority="P3", symbol="BTCUSDT")
    assert session.calls == []            # deferred: nothing was sent


def test_breaker_open_p1_p2_wait_for_cooldown(monkeypatch):
    sleeps = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", sleeps.append)
    retry_budget.get_breaker().trip(5.0)
    session = ScriptedSession([FakeResponse()])
    candles = make_client(session).get_klines("BTCUSDT")
    assert candles == []
    assert len(session.calls) == 1
    assert sleeps and 4.0 <= sleeps[0] <= 5.0   # bounded wait for cooldown


def test_breaker_bounded_wait_fails_closed_when_cooldown_too_long(no_sleep):
    retry_budget.get_breaker().trip(120.0)
    session = ScriptedSession([FakeResponse()])
    with pytest.raises(BybitRateLimitError, match="deferred"):
        make_client(session, bybit_breaker_max_wait_s=0.5).get_klines("BTCUSDT")
    assert session.calls == []


def test_p0_retries_only_after_cooldown(monkeypatch):
    sleeps = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", sleeps.append)
    session = ScriptedSession([outcome("rate_limit"), FakeResponse()])
    client = make_client(session, bybit_rate_limit_cooldown_s=2.0)
    payload = client._public_get("/v5/order/realtime", symbol="BTCUSDT")
    assert payload["retCode"] == 0
    assert len(session.calls) == 2
    # the retry left only after the cooldown wait, not inside the burst
    assert sleeps and sleeps[-1] >= 1.5


def test_rate_limit_event_trips_breaker_cooldown():
    breaker = retry_budget.get_breaker()
    assert not breaker.is_open
    breaker.trip(2.0)
    assert breaker.is_open
    assert breaker.remaining() > 0


# ----------------------------------------------------------------------
# 5. Telemetry parity: one BYBIT_API_CALL record per HTTP attempt
# ----------------------------------------------------------------------

def test_attempt_index_retry_reason_and_budget_visible(
        telemetry_env, no_sleep, caplog):
    session = ScriptedSession(
        [outcome("timeout"), outcome("timeout"), FakeResponse()])
    make_client(session, bybit_retry_backoff=0.0).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 3                 # one record per HTTP attempt
    assert [field(line, "result") for line in lines] == \
        ["TIMEOUT", "TIMEOUT", "OK"]
    assert [field(line, "attempt_index") for line in lines] == ["1", "2", "3"]
    assert [field(line, "retry_reason") for line in lines] == \
        ["none", "timeout", "timeout"]
    assert {field(line, "total_attempts") for line in lines} == {"3"}
    ids = {field(line, "logical_request_id") for line in lines}
    assert len(ids) == 1                   # one logical request id


def test_rate_limit_retry_reason_visible(telemetry_env, no_sleep, caplog):
    session = ScriptedSession([outcome("rate_limit"), FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 2
    assert [field(line, "retry_reason") for line in lines] == \
        ["none", "rate_limit"]
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2]


# ----------------------------------------------------------------------
# 6. Regression semantics preserved within the new budget
# ----------------------------------------------------------------------

def test_timeout_retry_log_wording_preserved(monkeypatch, caplog):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    caplog.set_level(logging.INFO)
    session = ScriptedSession(
        [outcome("timeout"), outcome("timeout"), FakeResponse()])
    make_client(session, bybit_retry_backoff=1.0).get_klines("VIRTUALUSDT")
    assert "VIRTUALUSDT: Bybit timeout, retry 1/3" in caplog.text
    assert "VIRTUALUSDT: Bybit timeout, retry 2/3" in caplog.text


# ----------------------------------------------------------------------
# 7. POST create_order: single attempt + double-submit protection
# ----------------------------------------------------------------------

def test_create_order_sends_exactly_one_http_attempt(no_sleep):
    session = ScriptedSession([
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}}),
    ])
    payload = BybitClient(live_settings(), session).create_order(
        "BTCUSDT", "Buy", 1)
    assert payload["result"]["orderId"] == "1"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"


def test_create_order_is_never_retried_on_timeout(no_sleep):
    session = ScriptedSession([TimeoutError("slow")])
    with pytest.raises(TimeoutError):
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 1        # no blind resend after a timeout


def test_create_order_blocks_identical_resubmission_after_unknown_outcome(no_sleep):
    session = ScriptedSession([
        TimeoutError("slow"),
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}}),
    ])
    client = BybitClient(live_settings(), session)
    with pytest.raises(TimeoutError):
        client.create_order("BTCUSDT", "Buy", 1)
    with pytest.raises(BybitOrderUncertainError, match="blocked"):
        client.create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 1        # the resend never reached the wire


def test_create_order_allows_resubmission_after_definitive_rejection(no_sleep):
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": "order rejected", "result": {}}),
        FakeResponse({"retCode": 10001, "retMsg": "order rejected", "result": {}}),
    ])
    client = BybitClient(live_settings(), session)
    for _ in range(2):
        with pytest.raises(BybitError, match="order rejected"):
            client.create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 2        # a fresh decision, not a blind retry


def test_create_order_guard_is_per_fingerprint(no_sleep):
    session = ScriptedSession([
        TimeoutError("slow"),
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}}),
    ])
    client = BybitClient(live_settings(), session)
    with pytest.raises(TimeoutError):
        client.create_order("BTCUSDT", "Buy", 1)
    # a DIFFERENT submission is not blocked
    payload = client.create_order("BTCUSDT", "Sell", 1)
    assert payload["retCode"] == 0
    assert len(session.calls) == 2


def test_create_order_unknown_state_never_timer_released(no_sleep):
    """Review #5709376849 (P1): the block is NEVER released by a timer.

    Even with the legacy block window at zero, an UNKNOWN outcome keeps
    blocking identical re-submission until reconciliation evidence arrives.
    """
    session = ScriptedSession([
        TimeoutError("slow"),
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "1"}}),
    ])
    client = BybitClient(
        live_settings(bybit_order_resubmit_block_s=0.0), session)
    with pytest.raises(TimeoutError):
        client.create_order("BTCUSDT", "Buy", 1)
    with pytest.raises(BybitOrderUncertainError, match="UNKNOWN"):
        client.create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 1        # no resend, no timer release


# ----------------------------------------------------------------------
# 8. Static source contracts: no independent nested retry loops
# ----------------------------------------------------------------------

def _source_of(obj) -> str:
    import inspect
    return inspect.getsource(obj)


def test_bybit_client_has_single_budgeted_loop():
    source = _source_of(BybitClient._public_get)
    assert source.count("while True") == 1     # one attempt state machine
    assert "for attempt" not in source         # no nested retry loops
    assert "for rl" not in source
    assert "retry_budget.HARD_MAX_ATTEMPTS" in source


def test_create_order_source_has_no_retry_loop():
    source = _source_of(BybitClient.create_order)
    assert "for attempt" not in source
    assert "while True" not in source
    assert "guard.begin" in source             # double-submit protection


def test_shadow_runners_have_no_independent_retry_loop():
    import app.shadow.runner as runner_mod
    import app.shadow.v2d_runner as v2d_mod
    for module in (runner_mod, v2d_mod):
        source = _source_of(module)
        assert "_scan_symbol_with_retry" not in source
        assert "for attempt" not in source
        assert "MAX_RETRIES" not in source


def test_htf_candle_source_has_no_hidden_retry_layer():
    from app.research import (
        htf_baseline_short_phase2_bybit_candle_source_v1 as mod,
    )
    for method in (mod.BybitPhase2CandleSource._fetch_server_time_ms,
                   mod.BybitPhase2CandleSource._fetch_page):
        source = _source_of(method)
        assert source.count("self.client._public_get") == 1  # one logical request
        assert "for attempt" not in source         # no hidden retry loop
    # gap recovery stays bounded (data completeness, not error retries)
    recovery = _source_of(mod.BybitPhase2CandleSource._recover_gaps)
    assert "gap_recovery_attempts" in recovery


def test_candle_sync_has_no_hidden_retry_layer():
    from app.analytics.candle_sync import CandleSync
    source = _source_of(CandleSync._fetch_candles_batch)
    assert source.count("self._bybit._public_get") == 1
    assert "for attempt" not in source
    assert "retry_count + 1" not in source


def test_isolated_oos_inherits_client_budget_without_own_retry_loop():
    # read the file directly: the module imports Unix-only ``pwd``
    source = (REPO_ROOT / "app" / "research" / "me_r_long_early_mae_v3"
              / "isolated_oos.py").read_text(encoding="utf-8")
    assert "for attempt" not in source
    assert "MAX_RETRIES" not in source
    assert "get_klines" in source                # still fetches per symbol


# ----------------------------------------------------------------------
# 9. Retry deadline (code review #5709376849, P2) — controlled clocks
# ----------------------------------------------------------------------

def install_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(bybit_client_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bybit_client_module.time, "sleep", clock.sleep)
    # the token limiter waits on real Event time — neutralize it so only the
    # controlled clock governs these tests
    class _NoopLimiter:
        def acquire(self):
            return None

    monkeypatch.setattr(BybitClient, "_global_limiter", _NoopLimiter())
    return clock


def test_deadline_rechecked_after_breaker_wait(monkeypatch):
    """After the bounded breaker wait the deadline is re-checked BEFORE the
    HTTP send: an expired operation must fail closed with zero requests."""
    clock = install_fake_clock(monkeypatch)
    retry_budget.get_breaker().trip(30.0)     # cooldown open for 30s
    session = ScriptedSession([FakeResponse()])
    client = make_client(session,
                         bybit_rate_limit_cooldown_s=30.0,
                         bybit_breaker_max_wait_s=30.0,
                         bybit_retry_deadline_s=10.0)
    with pytest.raises(BybitTimeoutError, match="deadline"):
        client.get_klines("BTCUSDT")
    assert session.calls == []                # nothing was sent after expiry
    assert clock.now >= 1000.0 + 10.0         # the wait really consumed time


def test_deadline_rechecked_after_backoff_sleep(monkeypatch):
    """The backoff sleep is a blocking wait too: the next attempt must not
    leave after the deadline expired."""
    clock = install_fake_clock(monkeypatch)
    session = ScriptedSession(
        [outcome("timeout"), outcome("timeout"), FakeResponse()])
    client = make_client(session,
                         bybit_retry_backoff=10.0,
                         bybit_retry_deadline_s=5.0)
    with pytest.raises(BybitTimeoutError, match="deadline"):
        client.get_klines("BTCUSDT")
    assert len(session.calls) == 1            # second attempt never sent
    assert clock.now >= 1000.0 + 10.0


def test_network_timeout_clamped_to_remaining_deadline(monkeypatch):
    """The per-attempt network timeout is bounded by the remaining operation
    time, never larger than the request timeout."""
    install_fake_clock(monkeypatch)
    session = ScriptedSession([FakeResponse()])
    make_client(session, bybit_timeout=15,
                bybit_retry_deadline_s=5.0).get_klines("BTCUSDT")
    assert session.calls[0][3] == 5.0         # clamped to the remaining time


def test_network_timeout_unchanged_when_deadline_comfortable(monkeypatch):
    """With plenty of deadline left the configured timeout passes through
    unchanged (frozen scanner semantics preserved)."""
    install_fake_clock(monkeypatch)
    session = ScriptedSession([FakeResponse()])
    make_client(session, bybit_timeout=15,
                bybit_retry_deadline_s=60.0).get_klines("BTCUSDT")
    assert session.calls[0][3] == 15.0
