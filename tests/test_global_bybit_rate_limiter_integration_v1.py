"""ISSUE231 Phase 4B — BybitClient ⇄ GlobalBybitRateLimiter integration.

Fake-transport coverage of the default-off feature flag
``global_bybit_rate_limiter_enabled`` (GLOBAL_BYBIT_RATE_LIMITER_V1):

 1. disabled — full parity with the prior Issue #229 behaviour, no shared
    limiter built (zero I/O);
 2. enabled GET — acquire → prepare_send → HTTP → record_result, one grant;
 3. GET retry — ≤ 3 HTTP attempts, exactly one grant per attempt;
 4. enabled POST — P0 acquire → OrderSubmitGuard → prepare_send → exactly one
    HTTP send;
 5. limiter SKIPPED / BREAKER_OPEN / STATE_UNAVAILABLE — zero HTTP sends, no
    fallback to the process-local limiter (FAIL_CLOSED);
 6. reconciliation GET — FAIL_CLOSED, the durable blocker is NOT lifted when
    the exchange is never reached;
 7. BYBIT_API_SKIP + queue-wait telemetry preserved;
 8. a record_result failure triggers NO extra HTTP attempts;
 9. endpoint priorities P0–P3 and the deadline are preserved;
10. no real Bybit API request is ever issued (urlopen is poisoned).

Core / trading logic / OOS / systemd / production env are untouched. Every
network call is served by an injected fake session; a poisoned ``urlopen``
proves no real request escapes.
"""
from __future__ import annotations

import logging
import time
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
    _ORDER_EXECUTION_TIMEOUT_S,
    _RateLimiter,
)
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    GrantOutcome,
    GrantResult,
    LimiterConfig,
)


# ------------------------------------------------------------------ fakes
class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []}}
        self.status = status
        self.headers = None

    def raise_for_status(self):
        if not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class ScriptedSession:
    """Replays scripted outcomes; falls back to an OK response when empty."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, dict(params or {})))
        return self._next()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self._next()

    def _next(self):
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def ok_klines():
    return FakeResponse({"retCode": 0, "retMsg": "OK",
                         "result": {"list": []}})


def ok_orders():
    return FakeResponse({"retCode": 0, "retMsg": "OK",
                         "result": {"category": "linear", "list": []}})


def ok_post():
    return FakeResponse({"retCode": 0, "retMsg": "OK",
                         "result": {"orderId": "OID1"}})


# ------------------------------------------------------- limiter test doubles
class _SpyLimiter:
    """Wraps a REAL limiter and records every gate call (order + arguments)."""

    def __init__(self, real):
        self._real = real
        self.acquires = []      # (priority, deadline, logical_request_id)
        self.grants = []        # grant_id of every GRANTED acquire
        self.prepared = []      # grant_id passed to prepare_send
        self.records = []       # (grant_id, priority, outcome)

    def acquire(self, priority, deadline=None, logical_request_id=""):
        self.acquires.append((priority, deadline, logical_request_id))
        out = self._real.acquire(priority, deadline=deadline,
                                 logical_request_id=logical_request_id)
        if out.granted:
            self.grants.append(out.grant_id)
        return out

    def prepare_send(self, grant_id):
        self.prepared.append(grant_id)
        return self._real.prepare_send(grant_id)

    def record_result(self, grant_id, priority, outcome, **kw):
        self.records.append((grant_id, priority, outcome))
        return self._real.record_result(grant_id, priority, outcome, **kw)

    @property
    def real(self):
        return self._real


class _FakeLimiter:
    """Returns a scripted GrantOutcome from acquire (deterministic denies)."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.acquires = []
        self.prepared = []
        self.records = []

    def acquire(self, priority, deadline=None, logical_request_id=""):
        self.acquires.append((priority, deadline, logical_request_id))
        return self._outcome

    def prepare_send(self, grant_id):
        self.prepared.append(grant_id)
        return True

    def record_result(self, grant_id, priority, outcome, **kw):
        self.records.append((grant_id, priority, outcome))
        return "APPLIED"


class _GrantButFailRecord:
    """Grants + prepares, but record_result always explodes (test 8)."""

    def __init__(self):
        self.acquires = 0
        self.records = 0

    def acquire(self, priority, deadline=None, logical_request_id=""):
        self.acquires += 1
        return GrantOutcome(GrantResult.GRANTED, grant_id="e1:1", epoch=1)

    def prepare_send(self, grant_id):
        return True

    def record_result(self, grant_id, priority, outcome, **kw):
        self.records += 1
        raise RuntimeError("simulated record_result failure")


# ------------------------------------------------------------------ helpers
def _isolated_limiter(tmp_path, **overrides):
    """A real, fast, workspace-isolated shared limiter (never the prod path)."""
    cfg = dict(
        enabled=True,
        state_path=str(Path(tmp_path) / "limiter_state.json"),
        g_rps=1000.0,
        burst=100000.0,
        registry_capacity=500000,     # >= ceil(2 * g_rps * max_lifetime)
        breaker_cooldowns_s=(0.05, 0.1, 0.2),
        lock_timeout_s=5.0,
        max_wait_s=5.0,
    )
    cfg.update(overrides)
    return GlobalBybitRateLimiter(LimiterConfig(**cfg))


def _base_settings(**overrides):
    values = dict(
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=2,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
        bybit_retry_deadline_s=10.0,
    )
    values.update(overrides)
    return Settings(**values)


def _live_settings(tmp_path, **overrides):
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        live_safety_ready=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
        bybit_order_submit_journal_path=str(Path(tmp_path) / "journal.json"),
    )
    values.update(overrides)
    return Settings(**values)


def _enabled_client(session, tmp_path, *, live=False, limiter=None):
    """Build a client with the Phase 4 flag ON and a limiter injected.

    Returns ``(client, spy_or_limiter)`` where the second element is the
    object actually installed as ``client._shared_limiter_obj`` (so tests can
    assert on gate calls). ``limiter=None`` ⇒ a real isolated limiter wrapped
    in the recording spy.
    """
    if live:
        settings = _live_settings(tmp_path,
                                  global_bybit_rate_limiter_enabled=True)
    else:
        settings = _base_settings(global_bybit_rate_limiter_enabled=True)
    client = BybitClient(settings, session)
    assert client._limiter_enabled() is True
    if limiter is None:
        limiter = _SpyLimiter(_isolated_limiter(tmp_path))
    client._shared_limiter_obj = limiter
    return client, limiter


def _disabled_client(session, tmp_path, *, live=False):
    settings = (_live_settings(tmp_path) if live else _base_settings())
    client = BybitClient(settings, session)
    assert client._limiter_enabled() is False
    return client


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def clean_process_state():
    retry_budget.reset_process_state()
    yield
    retry_budget.reset_process_state()


@pytest.fixture(autouse=True)
def fast_local_limiter(monkeypatch):
    """The pre-existing process-local limiter must never throttle a test."""
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Test 10: any real Bybit request (urlopen) fails the test immediately."""
    def _poisoned(*a, **k):
        raise AssertionError("real network request attempted in test")
    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)


@pytest.fixture
def telemetry_env(monkeypatch, caplog):
    from app.exchange.api_telemetry import ApiTelemetry
    monkeypatch.setattr(bybit_client_module, "_telemetry", None)
    # The ApiTelemetry singleton is process-global; clear its per-cycle record
    # list so a test only ever sees the records it produced itself.
    ApiTelemetry.get_instance().begin_cycle([])
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


# ============================================================ 1. disabled
def test_disabled_parity_no_limiter_built(tmp_path):
    """Default-off: exact prior behaviour AND the shared limiter is never built."""
    session = ScriptedSession([ok_klines()])
    client = _disabled_client(session, tmp_path)

    # zero I/O: the flag gates construction, so no limiter (hence no state
    # file, no flock, no epoch) is ever created while disabled.
    assert client._shared_limiter() is None
    assert client._shared_limiter_obj is None

    # GET behaves exactly as before (single attempt, normal payload).
    assert client.get_klines("BTCUSDT") == []
    assert len(session.calls) == 1
    assert session.calls[0][0] == "GET"


def test_disabled_post_parity_single_send(tmp_path):
    """Default-off POST: one send, guard resolved, no shared limiter."""
    session = ScriptedSession([ok_post()])
    client = _disabled_client(session, tmp_path, live=True)

    assert client._shared_limiter() is None
    payload = client.create_order("BTCUSDT", "Buy", 0.1)
    assert payload["retCode"] == 0
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1
    # guard recorded a definitive resolution (no UNKNOWN block left behind)
    guard = retry_budget.get_order_guard()
    assert guard.pending_unknown() == []


# ============================================================ 2. enabled GET
def test_enabled_get_acquire_prepare_send_http_record(tmp_path, telemetry_env):
    """acquire → prepare_send → HTTP → record_result(OK), exactly one grant."""
    session = ScriptedSession([ok_klines()])
    client, spy = _enabled_client(session, tmp_path)

    assert client.get_klines("BTCUSDT") == []

    # one HTTP attempt, one grant, prepare_send then record_result on it
    assert len(session.calls) == 1
    assert len(spy.acquires) == 1
    priority, deadline, lrid = spy.acquires[0]
    assert priority == "P1"                      # /v5/market/kline → P1
    assert deadline is not None                  # GET carries a deadline
    assert lrid
    gid = spy.grants[0]
    assert spy.prepared == [gid]                 # prepare_send before send
    assert spy.records == [(gid, "P1", "OK")]    # record_result(OK) after
    # the grant really consumed a token in the shared state
    assert spy.real.snapshot()["total_grants"] == 1
    assert spy.real.snapshot()["state"] == "CLOSED"


# ============================================================ 3. GET retry
def test_get_retry_le_3_attempts_one_grant_each(tmp_path, telemetry_env,
                                                no_sleep):
    """Timeout retries: ≤3 HTTP attempts, exactly one grant per attempt."""
    session = ScriptedSession([TimeoutError("slow"),
                               TimeoutError("slow"),
                               ok_klines()])
    client, spy = _enabled_client(session, tmp_path)

    assert client.get_klines("BTCUSDT") == []    # eventually succeeds

    # HARD_MAX_ATTEMPTS = 3 honoured
    assert len(session.calls) == 3
    # one acquire + one record_result per attempt (no attempt skips the gate)
    assert len(spy.acquires) == 3
    assert [r[2] for r in spy.records] == ["TIMEOUT", "TIMEOUT", "OK"]
    # one token consumed per attempt in the shared state
    assert spy.real.snapshot()["total_grants"] == 3


# ============================================================ 4. enabled POST
def test_enabled_post_p0_acquire_guard_prepare_single_send(tmp_path,
                                                           telemetry_env):
    """P0 acquire → OrderSubmitGuard → prepare_send → exactly ONE HTTP send."""
    session = ScriptedSession([ok_post()])
    client, spy = _enabled_client(session, tmp_path, live=True)

    payload = client.create_order("BTCUSDT", "Buy", 0.1)
    assert payload["retCode"] == 0

    # exactly one POST (non-idempotent, never retried)
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1

    # P0 priority, acquire wait bounded by the execution timeout (Q1)
    assert len(spy.acquires) == 1
    priority, deadline, lrid = spy.acquires[0]
    assert priority == "P0"
    assert deadline is not None                  # never deadline=None (30 s)
    assert time.monotonic() <= deadline <= (
        time.monotonic() + _ORDER_EXECUTION_TIMEOUT_S + 1.0)

    # prepare_send before the send, record_result(OK) after — same grant
    gid = spy.grants[0]
    assert spy.prepared == [gid]
    assert spy.records == [(gid, "P0", "OK")]
    assert spy.real.snapshot()["total_grants"] == 1

    # guard resolved definitively (no UNKNOWN block)
    assert retry_budget.get_order_guard().pending_unknown() == []


# --------------------------------------------- 4b. Q1 execution-timeout bound
def test_post_acquire_wait_bounded_by_execution_timeout(tmp_path,
                                                        telemetry_env):
    """Q1: the P0 acquire wait never exceeds the agreed execution timeout.

    ``deadline=None`` used to bound the wait at the limiter's generic
    ``max_wait_s`` (30 s), so a saturated bucket or an open breaker could
    stall an order well past the 10 s send budget. The fix passes an explicit
    deadline equal to the execution timeout, guaranteeing
    ``limiter wait <= execution timeout`` (fail-closed beyond it).
    """
    session = ScriptedSession([ok_post()])
    client, spy = _enabled_client(session, tmp_path, live=True)

    client.create_order("BTCUSDT", "Buy", 0.1)

    assert len(spy.acquires) == 1
    _, deadline, _ = spy.acquires[0]
    now = time.monotonic()
    # (1) never deadline=None (which would mean the 30 s max_wait_s cap)
    assert deadline is not None
    # (2) bounded by the execution timeout, NOT max_wait_s=30
    assert deadline <= now + _ORDER_EXECUTION_TIMEOUT_S + 0.5
    # (3) the send timeout shares the same envelope
    assert _ORDER_EXECUTION_TIMEOUT_S == 10.0
    # and the order still went out exactly once
    assert len([c for c in session.calls if c[0] == "POST"]) == 1


# ============================================================ 5. deny / FAIL_CLOSED
@pytest.mark.parametrize("gres,reason", [
    (GrantResult.SKIPPED, "deadline"),
    (GrantResult.BREAKER_OPEN, "breaker_open"),
    (GrantResult.STATE_UNAVAILABLE, "state_unavailable"),
])
def test_limiter_deny_fail_closed_no_fallback(tmp_path, telemetry_env,
                                              gres, reason):
    """A shared-limiter deny ⇒ zero HTTP sends, FAIL_CLOSED, no local fallback."""
    session = ScriptedSession([ok_klines()])
    waited = 7 if gres is GrantResult.SKIPPED else 0
    fake = _FakeLimiter(GrantOutcome(gres, reason=reason, waited_ms=waited))
    client, _ = _enabled_client(session, tmp_path, limiter=fake)

    with pytest.raises(BybitRateLimitError):
        client.get_klines("BTCUSDT")

    # zero HTTP: the deny happens before any attempt leaves
    assert session.calls == []
    # the shared limiter was consulted (no silent bypass)
    assert len(fake.acquires) == 1
    # FAIL_CLOSED, not a fallback: nothing was sent via the local limiter
    assert fake.prepared == []          # prepare_send never reached
    assert fake.records == []           # no result recorded for a non-grant


# ============================================================ 6. reconciliation
def test_reconciliation_get_fail_closed_blocker_kept(tmp_path, telemetry_env):
    """Limiter-denied reconciliation query ⇒ UNRESOLVED_BLOCKER, block kept."""
    journal = str(Path(tmp_path) / "journal.json")
    guard = retry_budget.get_order_guard()
    guard.configure(journal)
    fp = ("BTCUSDT", "Buy", "0.1", "Market")
    olink = "rbreconcile0001"
    guard.mark_unknown(fp, order_link_id=olink, reason="planted for test")
    assert [e["order_link_id"] for e in guard.pending_unknown()] == [olink]

    # reconciliation runs against a limiter that always denies
    session = ScriptedSession()                 # no outcome should be consumed
    fake = _FakeLimiter(GrantOutcome(GrantResult.SKIPPED, reason="deadline"))
    client, _ = _enabled_client(session, tmp_path, limiter=fake)

    report = client.reconcile_order_submissions()

    # the exchange was never reached → explicit blocker, not a resolution
    assert session.calls == []
    assert any(r.get("resolution") == "UNRESOLVED_BLOCKER" for r in report)
    # the durable blocker is NOT lifted without exchange confirmation
    assert [e["order_link_id"] for e in guard.pending_unknown()] == [olink]


# ============================================================ 7. telemetry
def test_bybit_api_skip_and_queue_wait_telemetry(tmp_path, telemetry_env,
                                                 caplog):
    """A limiter deny emits BYBIT_API_SKIP carrying the queue-wait figure."""
    session = ScriptedSession([ok_klines()])
    fake = _FakeLimiter(GrantOutcome(GrantResult.SKIPPED,
                                     reason="deadline", waited_ms=123))
    client, _ = _enabled_client(session, tmp_path, limiter=fake)

    with pytest.raises(BybitRateLimitError):
        client.get_klines("BTCUSDT")

    # exactly one telemetry record, tagged BYBIT_API_SKIP
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    assert field(lines[0], "result") == "BYBIT_API_SKIP"

    # the queue-wait figure survives in the record's raw (unescaped) ret_msg
    recs = bybit_client_module._get_telemetry()._calls
    assert len(recs) == 1
    assert recs[0].result == "BYBIT_API_SKIP"
    assert "limiter=SKIPPED" in recs[0].ret_msg
    assert "queue_wait_ms=123" in recs[0].ret_msg
    assert session.calls == []                   # still zero HTTP


# ============================================================ 8. record_result error
def test_record_result_failure_never_triggers_extra_http(tmp_path,
                                                         telemetry_env,
                                                         no_sleep, caplog):
    """A record_result explosion causes NO extra HTTP attempt (Q2 safety)
    and stays visible as a structured warning (Q2 observability)."""
    session = ScriptedSession([ok_klines(), ok_klines(), ok_klines()])
    limiter = _GrantButFailRecord()
    client, _ = _enabled_client(session, tmp_path, limiter=limiter)

    # the GET still succeeds: accounting is observational and never masks it
    assert client.get_klines("BTCUSDT") == []

    # exactly ONE HTTP attempt — the record_result failure caused no retry
    assert len(session.calls) == 1
    assert limiter.acquires == 1
    assert limiter.records == 1                 # it did fail, once

    # Q2: the durable-accounting failure is NOT silently swallowed — it is
    # surfaced as a structured warning (visible in the telemetry/log stream)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("record_result failed" in w for w in warnings), warnings


# ============================================================ 9. priorities + deadline
def test_priorities_and_deadline_preserved(tmp_path, telemetry_env):
    """Endpoint priority (P1 GET / P0 POST) and deadline shape are preserved."""
    session = ScriptedSession([ok_klines(), ok_post()])
    client, spy = _enabled_client(session, tmp_path, live=True)

    client.get_klines("BTCUSDT")                # /v5/market/kline → P1
    client.create_order("BTCUSDT", "Buy", 0.1)  # /v5/order/create → P0

    (p_get, dl_get, _), (p_post, dl_post, _) = spy.acquires
    assert p_get == "P1"
    assert dl_get is not None                   # GET: bounded deadline
    assert p_post == "P0"
    # POST acquire is bounded by the execution timeout (never None → 30 s)
    assert dl_post is not None
    assert dl_post <= time.monotonic() + _ORDER_EXECUTION_TIMEOUT_S + 1.0


# ============================================================ 10. no real requests
def test_no_real_bybit_requests_issued(tmp_path, telemetry_env):
    """Every request is served by the fake session; urlopen stays poisoned."""
    session = ScriptedSession([ok_klines(), ok_post()])
    client, _ = _enabled_client(session, tmp_path, live=True)

    client.get_klines("BTCUSDT")
    client.create_order("BTCUSDT", "Buy", 0.1)

    # all traffic went through the injected session (no real Bybit call)
    assert len(session.calls) == 2
    assert {c[0] for c in session.calls} == {"GET", "POST"}
    # the poisoned urlopen was never invoked (autouse fixture would have
    # raised AssertionError otherwise)
