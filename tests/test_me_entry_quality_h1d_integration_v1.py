"""Issue #314 / H1-D — integration: the research fetch against the REAL
client governance stack (audit defect D1).

Unlike the unit tests (which stub ``_public_get``), this module drives
``fetch_htf_history`` through a REAL ``BybitClient`` so the following run
for real, unmodified, on every request:

* the shared CROSS-PROCESS rate limiter
  (``app.exchange.global_rate_limiter.GlobalBybitRateLimiter`` with a
  durable state file under ``tmp_path`` — the same flock/epoch machinery
  production uses);
* the process-local retry BUDGET (``app.exchange.retry_budget``:
  ``bybit_max_attempts`` / rate-limit retry caps / backoff);
* the process-local circuit BREAKER (``RateLimitBreaker.gate``).

Only the transport double (``ScriptedSession``) stands in for the network,
and ``urlopen`` is poisoned so no real Bybit request can ever escape —
exactly the fake-transport style of
``test_global_bybit_rate_limiter_integration_v1``.

Scenarios:

 1. happy path: one grant per logical page request through the real shared
    limiter (P1), correct rows, real HTTP attempts only via the session;
 2. the real shared limiter SKIPS (no token) → ``HtfFetchLimiterDeniedError``
    fail-closed with ZERO HTTP;
 3. the real process-local breaker defers (cooldown open) →
    ``HtfFetchBreakerDeniedError`` fail-closed with ZERO HTTP;
 4. the real retry budget bounds rate-limit retries →
    ``HtfFetchRateLimitError`` after exactly ``bybit_max_attempts`` HTTP
    attempts (the adapter adds NO retry of its own);
 5. a rate-limit response then success: recovery happens INSIDE the budget
    (one logical request, several HTTP attempts).

Deterministic and offline: zero PostgreSQL, zero real Bybit, zero clock
dependence (backoffs set to 0).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import Settings
from app.exchange import retry_budget
from app.exchange.bybit_client import (
    BybitClient,
    BybitRateLimitError,
    _RateLimiter,
)
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    LimiterConfig,
)
from app.research.me_entry_quality_h1d_v1.htf_fetch import (
    HtfFetchBreakerDeniedError,
    HtfFetchLimiterDeniedError,
    HtfFetchRateLimitError,
    fetch_htf_history,
)

TF = 14_400_000        # 4H
BASE_MS = 365 * 86_400_000    # 1971-01-01T00:00:00Z — on the 4H grid


# ─── transport double (the ONLY stand-in) ───────────────────────────────────


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []}}
        self.status = status
        self.headers = None

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class ScriptedSession:
    """Replays scripted HTTP outcomes in call order (no network)."""

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


def _raw_row(open_ms):
    return [str(open_ms), "100", "101", "99", "100", "1", "100"]


def _raw_rows(start_ms, n):
    """Newest-first, exactly like Bybit."""
    return [_raw_row(start_ms + i * TF) for i in range(n)][::-1]


def _ok_klines(rows):
    return FakeResponse({"retCode": 0, "retMsg": "OK",
                         "result": {"list": list(rows)}})


def _rate_limited():
    return FakeResponse({"retCode": 10006, "retMsg": "too many visits",
                         "result": {"list": []}})


# ─── real limiter / client builders ─────────────────────────────────────────


class _SpyLimiter:
    """Wraps a REAL limiter and records every gate call."""

    def __init__(self, real):
        self._real = real
        self.acquires = []      # (priority, deadline, logical_request_id)
        self.grants = []
        self.prepared = []
        self.records = []

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


def _isolated_limiter(tmp_path, **overrides):
    """A real shared limiter on a workspace-isolated durable state file."""
    cfg = dict(
        enabled=True,
        state_path=str(Path(tmp_path) / "limiter_state.json"),
        g_rps=1000.0,
        burst=100000.0,
        registry_capacity=500000,
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


def _enabled_client(session, tmp_path, *, limiter=None, settings_over=None):
    """Real BybitClient with the shared cross-process limiter switched ON."""
    settings = _base_settings(
        global_bybit_rate_limiter_enabled=True, **(settings_over or {}))
    client = BybitClient(settings, session)
    assert client._limiter_enabled() is True
    if limiter is None:
        limiter = _SpyLimiter(_isolated_limiter(tmp_path))
    client._shared_limiter_obj = limiter
    return client, limiter


def _fetch(client, *, start=BASE_MS, end=BASE_MS + 60 * TF - 1, page_size=200):
    return fetch_htf_history(
        client, symbol="SOXSUSDT", timeframe="4H",
        start_ms=start, end_ms=end, page_size=page_size)


# ─── fixtures: clean process state, no real network, no local throttling ────


@pytest.fixture(autouse=True)
def clean_process_state():
    retry_budget.reset_process_state()
    yield
    retry_budget.reset_process_state()


@pytest.fixture(autouse=True)
def fast_local_limiter(monkeypatch):
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def _poisoned(*a, **k):
        raise AssertionError("real network request attempted in test")
    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


# ─── scenarios ──────────────────────────────────────────────────────────────


class TestRealGovernanceStack:
    def test_fetch_consumes_real_shared_limiter_end_to_end(self, tmp_path):
        """One real grant per logical page request, P1, recorded exactly."""
        page1 = _raw_rows(BASE_MS, 30)
        page2 = _raw_rows(BASE_MS + 30 * TF, 30)
        session = ScriptedSession([_ok_klines(page1), _ok_klines(page2)])
        client, limiter = _enabled_client(session, tmp_path)

        result = _fetch(client, end=BASE_MS + 60 * TF - 1, page_size=30)

        assert result.requests_used == 2
        assert len(result.rows) == 60
        assert result.first_open_ms == BASE_MS
        assert result.last_open_ms == BASE_MS + 59 * TF
        # the REAL shared limiter was consulted per page request…
        assert len(limiter.acquires) == 2
        assert [p for p, _, _ in limiter.acquires] == ["P1", "P1"]
        assert len(limiter.grants) == 2
        assert len(limiter.prepared) == 2
        assert len(limiter.records) == 2
        # …and the requests really left through the (double) transport with
        # the research interval and explicit bounds
        assert len(session.calls) == 2
        for _, _, params in session.calls:
            assert params["interval"] == "240"
            assert params["category"] == "linear"
            assert params["symbol"] == "SOXSUSDT"
            assert "start" in params and "end" in params

    def test_real_shared_limiter_skip_fails_closed_zero_http(self, tmp_path):
        """A real SKIPPED grant (bucket empty) is a limiter denial — the
        request is never sent and no rows are returned."""
        # one token of burst, refill ~0 — consume it with a real acquire
        limiter = _isolated_limiter(tmp_path, g_rps=0.0001, burst=1.0)
        warmup = limiter.acquire("P1", deadline=time.monotonic() + 0.5,
                                 logical_request_id="warmup")
        assert warmup.granted
        session = ScriptedSession([])
        client, _ = _enabled_client(
            session, tmp_path, limiter=limiter,
            settings_over={"global_limiter_max_wait_s": 0.05})

        with pytest.raises(HtfFetchLimiterDeniedError) as excinfo:
            _fetch(client)

        assert excinfo.value.limiter_result == "SKIPPED"
        assert isinstance(excinfo.value.__cause__, BybitRateLimitError)
        assert session.calls == []          # ZERO HTTP — fail closed

    def test_real_breaker_deferral_fails_closed_zero_http(self, tmp_path):
        """An open process-local breaker cooldown defers the request."""
        retry_budget.get_breaker().trip(60.0)
        session = ScriptedSession([])
        client = BybitClient(
            _base_settings(bybit_breaker_max_wait_s=0.0), session)

        with pytest.raises(HtfFetchBreakerDeniedError) as excinfo:
            _fetch(client)

        assert "cooldown open" in str(excinfo.value.__cause__)
        assert session.calls == []          # ZERO HTTP — fail closed

    def test_real_retry_budget_bounds_rate_limit_retries(self, tmp_path):
        """Persistent 10006s stop at the REAL attempt budget — the adapter
        never retries on its own."""
        session = ScriptedSession([_rate_limited() for _ in range(5)])
        client = BybitClient(
            _base_settings(bybit_max_attempts=2,
                           bybit_rate_limit_max_retries=1), session)

        with pytest.raises(HtfFetchRateLimitError) as excinfo:
            _fetch(client)

        assert excinfo.value.ret_code == 10006
        assert isinstance(excinfo.value.__cause__, BybitRateLimitError)
        assert len(session.calls) == 2       # exactly bybit_max_attempts

    def test_rate_limit_recovery_happens_inside_the_budget(self, tmp_path):
        """10006 → 10006 → OK: ONE logical request, THREE HTTP attempts."""
        rows = _raw_rows(BASE_MS, 10)
        session = ScriptedSession(
            [_rate_limited(), _rate_limited(), _ok_klines(rows)])
        client = BybitClient(
            _base_settings(bybit_max_attempts=3,
                           bybit_rate_limit_max_retries=2), session)

        result = _fetch(client, end=BASE_MS + 10 * TF - 1, page_size=10)

        assert result.requests_used == 1     # the adapter issued ONE page
        assert len(session.calls) == 3       # retries lived INSIDE _public_get
        assert len(result.rows) == 10
