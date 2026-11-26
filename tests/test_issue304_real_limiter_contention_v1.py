"""Issue #304: offline evidence using the REAL shared limiter implementation.

No Bybit, no runtime imports, no production state file, no service changes.
The tests intentionally demonstrate both lifecycle safety and the limit of
what priority alone can guarantee under shared P1 contention.
"""
from __future__ import annotations

from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    GrantResult,
    LimiterConfig,
)


class FakeClock:
    """Deterministic time; all sleeps advance both clocks without real waiting."""

    def __init__(self):
        self.wall_time = 1_700_000_000.0
        self.mono_time = 1000.0

    def wall(self):
        return self.wall_time

    def mono(self):
        return self.mono_time

    def sleep(self, seconds):
        increment = max(0.0, float(seconds))
        self.wall_time += increment
        self.mono_time += increment


def make_limiter(tmp_path, *, clock=None, **overrides):
    clock = clock or FakeClock()
    config = dict(
        enabled=True,
        state_path=str(tmp_path / "issue304_isolated_state.json"),
        g_rps=8.0,
        burst=12.0,
    )
    config.update(overrides)
    return GlobalBybitRateLimiter(LimiterConfig(**config), clock=clock), clock


def complete_success(limiter, *, priority="P1"):
    grant = limiter.acquire(priority, deadline=limiter.clock.mono() + 5.0)
    assert grant.granted, grant
    assert limiter.prepare_send(grant.grant_id) is True
    assert limiter.record_result(
        grant.grant_id, priority, "OK", epoch=grant.epoch
    ) == "APPLIED"
    return grant


def test_same_p1_priority_cannot_reserve_scanner_capacity(tmp_path):
    """Research protective kline and Scanner kline are both presently P1.

    Issue 304 may NOT claim 50/50 just because 8 RPS/12 burst is configured.
    The first 12 grants exhaust the available burst; a short-deadline
    Scanner request remains unsent. This is a sequential contention
    counterexample, not a concurrent timing or a Bybit simulation.
    """
    limiter, clock = make_limiter(tmp_path)
    grants = []
    # This loop models 12 already-granted P1 protective data requests.
    for _ in range(12):
        grants.append(complete_success(limiter))
    assert len({grant.grant_id for grant in grants}) == 12

    # A Scanner P1 request now has the same class, no separate reservation.
    skipped = limiter.acquire("P1", deadline=clock.mono() + 0.10)
    assert skipped.result is GrantResult.SKIPPED
    assert skipped.reason == "deadline"
    state = limiter.snapshot()
    assert state["total_grants"] == 12
    assert state["pending_count"] == 0


def test_scanner_burst_does_not_guarantee_protective_latency(tmp_path):
    """Symmetry check: if Scanner consumes the P1 burst first, a protective
    kline with a short deadline can also be skipped. No implicit preference
    between two P1 consumer roles exists in the existing API.
    """
    limiter, clock = make_limiter(tmp_path)
    for _ in range(12):
        complete_success(limiter)
    protective = limiter.acquire("P1", deadline=clock.mono() + 0.10)
    assert protective.result is GrantResult.SKIPPED
    assert protective.reason == "deadline"
    assert limiter.snapshot()["total_grants"] == 12


def test_breaker_revokes_previously_granted_requests_before_send(tmp_path):
    """A RATE_LIMIT recorded on a different grant fences a prior grant;
    an application must not send HTTP when prepare_send returns false.
    """
    limiter, clock = make_limiter(tmp_path)
    held = limiter.acquire("P1", deadline=clock.mono() + 5.0)
    trigger = limiter.acquire("P1", deadline=clock.mono() + 5.0)
    assert held.granted and trigger.granted
    assert limiter.prepare_send(trigger.grant_id) is True
    assert limiter.record_result(
        trigger.grant_id, "P1", "RATE_LIMIT", epoch=trigger.epoch
    ) == "APPLIED"

    # The shared state is now in the new breaker epoch.
    assert limiter.snapshot()["state"] == "OPEN"
    assert limiter.prepare_send(held.grant_id) is False
    assert limiter.snapshot()["total_grants"] == 2


def test_default_off_has_no_shared_state_file(tmp_path):
    """The limiter's existing disabled mode must remain zero-I/O."""
    clock = FakeClock()
    state_path = tmp_path / "disabled_state.json"
    limiter = GlobalBybitRateLimiter(
        LimiterConfig(enabled=False, state_path=str(state_path)), clock=clock
    )
    granted = limiter.acquire("P1", deadline=clock.mono() + 1.0)
    assert granted.granted
    assert limiter.snapshot() == {"enabled": False}
    assert not state_path.exists()
    assert not (tmp_path / "disabled_state.json.lock").exists()
