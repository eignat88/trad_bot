"""ISSUE253 / BYBIT_SCANNER_STARTUP_BREAKER_OPEN_RECOVERY_V1 — regressions.

Production incident (2026-11-03, release ``54201c0`` / PR #255): with
``scanner_universe.mode=dynamic`` the FIRST Bybit call of the process is the
universe refresh, executed before the database repository and the runner
lock exist. The shared limiter denied it with
``BREAKER_OPEN: breaker_cooldown_exceeds_deadline``, the resulting
``BybitRateLimitError`` escaped ``main()`` and systemd restart-looped the
Scanner until the release was rolled back to ``ce51f2a4``.

Every test here is deterministic: no network (an in-memory session fails
loudly if HTTP escapes the limiter), no PostgreSQL, no real long waits
(the recovery wait is injected), no frozen policy/scoring/trading gate is
touched. The BREAKER_OPEN → RECOVERY tests drive a REAL ``BybitClient``
with a REAL ``GlobalBybitRateLimiter`` whose breaker is genuinely tripped
through its public acquire/record_result API — never a stub.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import scanner_runner
from app.config import ScannerUniverseSettings, Settings
from app.exchange.bybit_client import (
    BybitAuthError,
    BybitClient,
    BybitError,
    BybitRateLimitError,
)

ROOT = Path(__file__).resolve().parent.parent

#: The exact deny wording of the incident (BybitClient fail-closed branch):
INCIDENT_DENY_MESSAGE = (
    "/v5/market/instruments-info: /v5/market/instruments-info deferred by "
    "shared rate limiter (BREAKER_OPEN: breaker_cooldown_exceeds_deadline) "
    "— not sent (fail-closed)"
)


# --------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def clean_process_state():
    from app.exchange import retry_budget
    retry_budget.reset_process_state()
    yield
    retry_budget.reset_process_state()


@pytest.fixture(autouse=True)
def fast_local_limiter(monkeypatch):
    """The pre-existing process-local limiter must never throttle a test."""
    from app.exchange.bybit_client import _RateLimiter
    monkeypatch.setattr(BybitClient, "_global_limiter",
                        _RateLimiter(rps=1_000_000.0))


@pytest.fixture(autouse=True)
def shutdown_flag_reset():
    """``main()``'s SIGTERM/SIGINT flag starts and ends False per test."""
    scanner_runner.SHUTDOWN = False
    yield
    scanner_runner.SHUTDOWN = False


# ------------------------------------------------------------- collaborators
class _JsonResponse:
    """Minimal Bybit-shaped response for a real HTTP send."""

    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingSession:
    """Records every HTTP attempt; raises unless explicitly unblocked.

    A limiter deny must leave ``calls`` EMPTY — any URL here proves the
    request escaped the FAIL_CLOSED gate.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.blocked = True

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        self.events.append(("http", url))
        if self.blocked:
            raise AssertionError(f"HTTP attempted despite limiter deny: {url}")
        if url.endswith("/v5/market/instruments-info"):
            return _JsonResponse({
                "retCode": 0, "retMsg": "OK",
                "result": {
                    "list": [{
                        "symbol": "BTCUSDT", "status": "Trading",
                        "quoteCoin": "USDT",
                        "contractType": "LinearPerpetual",
                    }],
                    "nextPageCursor": "",
                },
            })
        if url.endswith("/v5/market/tickers"):
            return _JsonResponse({
                "retCode": 0, "retMsg": "OK",
                "result": {"list": [{
                    "symbol": "BTCUSDT",
                    "turnover24h": "123456789",
                    "volume24h": "1000",
                }]},
            })
        raise AssertionError(f"unexpected URL {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        self.events.append(("http", url))
        raise AssertionError(f"POST attempted: {url}")


class _FailingClient:
    """Client whose universe fetch always fails with the given exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def get_liquid_instruments(self, **kwargs):
        self.calls += 1
        raise self._exc


class _Bomb:
    """Collaborator that must never be reached in the tested path."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.hits: list[str] = []

    def __call__(self, *args, **kwargs):
        self.hits.append(self.name)
        raise AssertionError(f"{self.name} must not run in this path")


def _startup_settings(tmp_path: Path, **universe_overrides) -> Settings:
    """Dynamic-universe Settings with the shared limiter genuinely ON.

    The 0.2 s acquire budget vs. a 2.0 s breaker cooldown reproduces the
    incident deny (``wait >= budget`` → ``breaker_cooldown_exceeds_deadline``)
    with a wide margin around the (real, fsync-bound) trip→attempt gap, while
    the cooldown still expires within a couple of injected waits so the
    RECOVERY probe is admitted quickly.
    """
    universe = dict(
        mode="dynamic",
        startup_retry_interval_s=0.5,
        startup_retry_max_interval_s=4.0,
        startup_retry_max_attempts=0,
    )
    universe.update(universe_overrides)
    return Settings(
        scanner_universe=ScannerUniverseSettings(**universe),
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "shared_state.json"),
        global_limiter_breaker_cooldowns_s=(2.0, 4.0, 8.0),
        global_limiter_recovery_success_target=1,
        global_limiter_max_wait_s=0.2,
        bybit_timeout=2,
        bybit_max_attempts=1,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=0,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
        bybit_retry_deadline_s=5.0,
    )


def _breaking_client(settings: Settings,
                     session: _RecordingSession) -> BybitClient:
    """REAL client whose shared limiter REALLY denies (breaker OPEN).

    The breaker is tripped through the public acquire/record_result API —
    no state-file surgery — so the deny every test observes is the genuine
    BREAKER_OPEN path of the production incident, not a stub.
    """
    client = BybitClient(settings, session)
    limiter = client._shared_limiter()
    grant = limiter.acquire("P1")
    assert grant.granted, grant
    limiter.record_result(grant.grant_id, "P1", "RATE_LIMIT")  # trips breaker
    denied = limiter.acquire("P1")
    assert not denied.granted, "breaker did not open"
    assert session.calls == []            # tripping it must not send HTTP
    return client


def _load(tmp_path: Path, payload: dict):
    from app.config import load_settings
    config = tmp_path / "config.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(config, env_file=tmp_path / ".env.absent")


# ===========================================================================
# 1. BREAKER_OPEN → wait → RECOVERY → successful universe initialization
# ===========================================================================
def test_breaker_open_waits_then_recovers_and_initializes_universe(tmp_path):
    """The production sequence, end to end, with the REAL limiter.

    While the breaker is OPEN the deny must send NOTHING and be waited out;
    once the cooldown expires the recovery probe is admitted, the universe
    refresh completes and returns the DISCOVERED symbols (never a
    fabricated/stale list).
    """
    settings = _startup_settings(tmp_path)
    session = _RecordingSession()
    client = _breaking_client(settings, session)

    sleeps: list[float] = []

    def fake_sleep(step: float) -> None:
        sleeps.append(step)
        assert session.calls == [], "a denied attempt must send no HTTP"
        session.blocked = False      # the network is healthy again from now on
        time.sleep(step)             # let the REAL breaker cooldown elapse

    universe = scanner_runner.initialize_scanner_universe(
        client, settings, sleep=fake_sleep)

    assert universe is not None, "recovery must yield a universe, not None"
    assert [row["symbol"] for row in universe] == ["BTCUSDT"]
    assert universe[0]["rank"] == 1
    assert sleeps, "the deny must have been waited out, not retried in place"
    assert all(step > 0 for step in sleeps)
    # at least one full configured-interval wait happened:
    assert sum(sleeps) >= settings.scanner_universe.startup_retry_interval_s
    # exactly two requests, both AFTER the breaker recovered:
    assert len(session.calls) == 2


# ===========================================================================
# 2. repeated temporary denials — bounded, configurable, never a busy loop
# ===========================================================================
def test_repeated_temporary_denials_wait_with_bounded_backoff(tmp_path):
    """Six denials → six attempts → five waits: 1 s, 2 s, 4 s, 4 s, 4 s.

    Every wait chunk is positive and ≤ the poll granularity, the per-wait
    total never exceeds ``startup_retry_max_interval_s`` and the whole test
    runs on injected sleeps — i.e. the recovery loop always SLEEPS (no
    busy-spin) and its schedule is exactly the configured one.
    """
    settings = _startup_settings(
        tmp_path,
        startup_retry_interval_s=1.0,
        startup_retry_max_interval_s=4.0,
        startup_retry_max_attempts=6,
    )
    client = _FailingClient(BybitRateLimitError(INCIDENT_DENY_MESSAGE))
    sleeps: list[tuple[int, float]] = []

    def fake_sleep(step: float) -> None:
        sleeps.append((client.calls, step))   # attempts so far == wait index

    started = time.monotonic()
    with pytest.raises(BybitRateLimitError):
        scanner_runner.initialize_scanner_universe(
            client, settings, sleep=fake_sleep)
    elapsed = time.monotonic() - started

    assert client.calls == 6
    assert elapsed < 1.0, "the schedule must run on injected sleeps only"

    per_wait: dict[int, float] = {}
    for attempt_no, step in sleeps:
        per_wait[attempt_no] = per_wait.get(attempt_no, 0.0) + step
    assert [round(per_wait[key], 6) for key in sorted(per_wait)] == \
        [1.0, 2.0, 4.0, 4.0, 4.0]          # doubling, capped at max_interval
    assert all(0 < step <= scanner_runner.STARTUP_RETRY_POLL_S
               for _, step in sleeps)      # bounded chunks, never busy


def test_backoff_never_exceeds_configured_max_interval(tmp_path):
    """The cap holds across a long denial streak (no unbounded growth)."""
    settings = _startup_settings(
        tmp_path,
        startup_retry_interval_s=1.0,
        startup_retry_max_interval_s=2.0,
        startup_retry_max_attempts=9,
    )
    client = _FailingClient(BybitRateLimitError(INCIDENT_DENY_MESSAGE))
    sleeps: list[tuple[int, float]] = []
    with pytest.raises(BybitRateLimitError):
        scanner_runner.initialize_scanner_universe(
            client, settings, sleep=lambda s: sleeps.append((client.calls, s)))

    per_wait: dict[int, float] = {}
    for attempt_no, step in sleeps:
        per_wait[attempt_no] = per_wait.get(attempt_no, 0.0) + step
    waits = [per_wait[key] for key in sorted(per_wait)]
    assert waits == [1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
    assert max(waits) <= settings.scanner_universe.startup_retry_max_interval_s
    assert all(step > 0 for _, step in sleeps)


# ===========================================================================
# 3. graceful shutdown (SIGTERM/SIGINT) while waiting
# ===========================================================================
def test_shutdown_signal_during_wait_returns_none_after_one_poll(tmp_path):
    settings = _startup_settings(tmp_path, startup_retry_interval_s=1.0)
    client = _FailingClient(BybitRateLimitError(INCIDENT_DENY_MESSAGE))
    sleeps: list[float] = []

    def fake_sleep(step: float) -> None:
        sleeps.append(step)
        scanner_runner.SHUTDOWN = True       # SIGTERM/SIGINT arrives here

    universe = scanner_runner.initialize_scanner_universe(
        client, settings, sleep=fake_sleep)

    assert universe is None, "shutdown while waiting must stop cleanly"
    assert client.calls == 1
    assert sleeps == [1.0]                  # stopped after the first poll


def test_shutdown_before_first_attempt_does_no_work(tmp_path):
    settings = _startup_settings(tmp_path)
    client = _FailingClient(BybitRateLimitError(INCIDENT_DENY_MESSAGE))
    scanner_runner.SHUTDOWN = True

    universe = scanner_runner.initialize_scanner_universe(
        client, settings, sleep=lambda s: pytest.fail("no wait expected"))

    assert universe is None
    assert client.calls == 0                # no Bybit request after shutdown


def test_main_shutdown_during_recovery_leaves_no_side_effects(tmp_path,
                                                              monkeypatch):
    """Requirement: NO database / OOS / observer work before the universe.

    SIGTERM arrives while startup is waiting out the breaker: ``main()``
    returns normally (graceful) with zero HTTP, zero repository
    constructions and zero observer/OOS initializations.
    """
    settings = _startup_settings(tmp_path, startup_retry_interval_s=1.0)
    session = _RecordingSession()
    client = _breaking_client(settings, session)

    repo_bomb = _Bomb("ScannerRepository")
    research_bomb = _Bomb("_get_research_observer")
    prospective_bomb = _Bomb("_get_prospective_observer")
    mae_bomb = _Bomb("_get_me_early_mae_v2_pipeline")
    sleeps: list[float] = []

    def fake_sleep(step: float) -> None:
        sleeps.append(step)
        scanner_runner.SHUTDOWN = True       # signal during the wait

    monkeypatch.setattr(scanner_runner, "setup_logging", lambda: None)
    monkeypatch.setattr(scanner_runner, "_load_runner_settings",
                        lambda: settings)
    monkeypatch.setattr(scanner_runner, "BybitClient", lambda s: client)
    monkeypatch.setattr(scanner_runner, "_startup_sleep", fake_sleep)
    monkeypatch.setattr(scanner_runner, "ScannerRepository", repo_bomb)
    monkeypatch.setattr(scanner_runner, "_get_research_observer",
                        research_bomb)
    monkeypatch.setattr(scanner_runner, "_get_prospective_observer",
                        prospective_bomb)
    monkeypatch.setattr(scanner_runner, "_get_me_early_mae_v2_pipeline",
                        mae_bomb)

    assert scanner_runner.main() is None    # graceful, no SystemExit

    assert sleeps, "the denial must have been waited out"
    assert repo_bomb.hits == []
    assert research_bomb.hits == []
    assert prospective_bomb.hits == []
    assert mae_bomb.hits == []
    assert session.calls == []              # no HTTP escaped the limiter


# ===========================================================================
# 4. permanent startup failures — never retried, never swallowed
# ===========================================================================
@pytest.mark.parametrize("build_exc", [
    lambda: BybitAuthError("HTTP 401"),
    lambda: BybitError("invalid api key"),
    lambda: BybitRateLimitError(
        "/v5/market/instruments-info: shared rate limiter unavailable "
        "(LimiterUnavailableError) — not sent (fail-closed)"),
    lambda: BybitRateLimitError(
        "/v5/market/instruments-info: deferred by shared rate limiter "
        "(STATE_UNAVAILABLE: config_mismatch) — not sent (fail-closed)"),
    lambda: RuntimeError(
        "Dynamic scanner universe is empty; check liquidity thresholds"),
], ids=["auth", "api_error", "limiter_unavailable", "state_unavailable",
        "empty_universe"])
def test_permanent_startup_failure_is_never_retried(tmp_path, build_exc):
    """Exactly ONE attempt, zero waits, the original exception propagates."""
    settings = _startup_settings(tmp_path, startup_retry_max_attempts=0)
    exc = build_exc()
    client = _FailingClient(exc)
    sleeps: list[float] = []

    with pytest.raises(type(exc)) as excinfo:
        scanner_runner.initialize_scanner_universe(
            client, settings, sleep=sleeps.append)

    assert excinfo.value is exc            # re-raised unchanged (fail-closed)
    assert client.calls == 1
    assert sleeps == []


def test_temporary_denial_classification_contract():
    """Only denials a bounded wait can outlive are classified temporary."""
    temporary = [
        BybitRateLimitError(
            "X: deferred by shared rate limiter (BREAKER_OPEN: "
            "breaker_cooldown_exceeds_deadline) — not sent (fail-closed)"),
        BybitRateLimitError(
            "X: deferred by shared rate limiter (SKIPPED: demand_expired) "
            "— not sent (fail-closed)"),
        BybitRateLimitError(
            "X: limiter grant revoked before send (epoch moved) — not sent "
            "(fail-closed)"),
        BybitRateLimitError(
            "X: Rate limited on /v5/market/tickers after 3 attempt(s) "
            "(storm guard: retry share capped)"),
    ]
    permanent = [
        BybitAuthError("HTTP 401"),
        BybitError("Bybit request failed"),
        BybitRateLimitError(
            "X: shared rate limiter unavailable (LimiterUnavailableError) "
            "— not sent (fail-closed)"),
        BybitRateLimitError(
            "X: deferred by shared rate limiter (STATE_UNAVAILABLE: "
            "corrupt_state) — not sent (fail-closed)"),
        BybitRateLimitError(               # unknown future wording
            "X: some brand new deny wording"),
        RuntimeError("Dynamic scanner universe is empty"),
        ValueError("scanner_universe.mode must be static or dynamic"),
    ]

    assert all(scanner_runner.is_temporary_startup_limiter_denial(exc)
               for exc in temporary)
    assert not any(scanner_runner.is_temporary_startup_limiter_denial(exc)
                   for exc in permanent)


# ===========================================================================
# 5. main(): database/OOS only AFTER a successful universe initialization
# ===========================================================================
def test_main_recovers_from_breaker_open_then_initializes_database(tmp_path,
                                                                  monkeypatch):
    """BREAKER_OPEN → wait → RECOVERY → universe → THEN the database.

    ``main()`` may only construct the repository (here: a fake whose ping()
    fails, ending startup with SystemExit) strictly after the universe HTTP
    requests have succeeded.
    """
    settings = _startup_settings(tmp_path)
    session = _RecordingSession()
    client = _breaking_client(settings, session)

    repo_state = {"constructed": 0}

    class _FakeRepository:
        def __init__(self, *args, **kwargs):
            repo_state["constructed"] += 1
            session.events.append(("repo", "ScannerRepository"))

        def ping(self):
            return False                    # stop startup right after connect

        def close(self):
            session.events.append(("repo_closed", "ScannerRepository"))

    def fake_sleep(step: float) -> None:
        # While still waiting, neither HTTP nor the database may exist yet.
        assert repo_state["constructed"] == 0
        if not any(kind == "http" for kind, _ in session.events):
            session.blocked = False          # healthy again after the wait
        time.sleep(step)                     # real cooldown elapse

    monkeypatch.setattr(scanner_runner, "setup_logging", lambda: None)
    monkeypatch.setattr(scanner_runner, "_load_runner_settings",
                        lambda: settings)
    monkeypatch.setattr(scanner_runner, "BybitClient", lambda s: client)
    monkeypatch.setattr(scanner_runner, "_startup_sleep", fake_sleep)
    monkeypatch.setattr(scanner_runner, "ScannerRepository", _FakeRepository)

    with pytest.raises(SystemExit, match="database health check failed"):
        scanner_runner.main()

    assert repo_state["constructed"] == 1
    kinds = [kind for kind, _ in session.events]
    http_at = kinds.index("http")
    repo_at = kinds.index("repo")
    assert kinds.count("http") == 2         # instruments-info + tickers
    assert repo_at > kinds.index("http", http_at + 1), (
        "the database may only be initialized AFTER the universe succeeded")


def test_main_orders_universe_recovery_before_database_and_lock():
    """Structural invariant (Issue #253 requirement): the startup order is
    universe recovery → database repository → runner lock, and the per-cycle
    refresh stays AFTER the lock with its keep-last-good-universe guard.
    """
    import inspect
    source = inspect.getsource(scanner_runner.main)

    universe_at = source.index("initialize_scanner_universe(client, settings)")
    db_at = source.index("ScannerRepository(")
    lock_at = source.index("acquire_runner_lock")
    refresh_at = source.index("get_scanner_universe(client, settings)")

    assert universe_at < db_at < lock_at < refresh_at
    assert "if universe is None:" in source          # graceful shutdown path
    assert "failed to refresh scanner universe" in source  # last-good kept


def test_static_universe_passes_through_without_any_client_call():
    """Static mode never touches the network: identical to the old path."""
    class _BombClient:
        def __getattr__(self, name):
            raise AssertionError("static mode must not call the client")

    settings = Settings(scanner_universe=ScannerUniverseSettings(
        mode="static"))

    universe = scanner_runner.initialize_scanner_universe(
        _BombClient(), settings,
        sleep=lambda s: pytest.fail("no wait expected"))

    assert universe == [{"symbol": "BTCUSDT", "rank": 1},
                        {"symbol": "ETHUSDT", "rank": 2}]


# ===========================================================================
# 6. configuration contract (bounded, configurable intervals)
# ===========================================================================
def test_startup_retry_knobs_default_and_load(tmp_path):
    defaults = _load(tmp_path, {})
    assert defaults.scanner_universe.startup_retry_interval_s == 10.0
    assert defaults.scanner_universe.startup_retry_max_interval_s == 60.0
    assert defaults.scanner_universe.startup_retry_max_attempts == 0

    configured = _load(tmp_path, {
        "scanner_universe": {
            "mode": "dynamic",
            "top_n": 50,
            "startup_retry_interval_s": 5,
            "startup_retry_max_interval_s": 30,
            "startup_retry_max_attempts": 12,
        },
    })
    universe = configured.scanner_universe
    assert universe.startup_retry_interval_s == 5
    assert universe.startup_retry_max_interval_s == 30
    assert universe.startup_retry_max_attempts == 12


@pytest.mark.parametrize("bad,match", [
    ({"startup_retry_interval_s": 0}, "startup_retry_interval_s"),
    ({"startup_retry_interval_s": -1}, "startup_retry_interval_s"),
    ({"startup_retry_interval_s": 10, "startup_retry_max_interval_s": 5},
     "startup_retry_max_interval_s"),
    ({"startup_retry_max_attempts": -1}, "startup_retry_max_attempts"),
], ids=["zero_interval", "negative_interval", "max_below_interval",
        "negative_attempts"])
def test_invalid_startup_retry_knobs_fail_closed(tmp_path, bad, match):
    """Invalid recovery config aborts at settings-load time — before any
    BybitClient exists and therefore before any HTTP request could go out.
    """
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, {"scanner_universe": bad})


def test_recovery_wait_is_poll_chunked_and_shutdown_aware():
    """The wait itself: positive bounded chunks, shutdown ends it early."""
    slept: list[float] = []
    assert scanner_runner.wait_before_startup_retry(
        3.0, sleep=slept.append, poll_s=1.0) is True
    assert slept == [1.0, 1.0, 1.0]

    scanner_runner.SHUTDOWN = True
    slept.clear()
    assert scanner_runner.wait_before_startup_retry(
        3.0, sleep=slept.append, poll_s=1.0) is False
    assert slept == []                      # shutdown checked before sleeping
