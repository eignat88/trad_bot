"""Read-only breaker cooldown hint + real shared breaker at levels 1/2/3.

Offline only: workspace-isolated limiter (tmp_path), fake HTTP session, no
production DB, no network.  The autouse fixture forbids real network.

Contract under test (review D1):
* ``breaker_cooldown_hint_seconds`` reads ONLY ``limiter.snapshot()`` and
  returns exactly ``cooldown_until_wall - now`` while the breaker is OPEN,
  ``0.0`` in RECOVERY/CLOSED;
* anything unprovable raises ``RecoveryHintUnavailable`` (fail-closed to
  the normal schedule — the 130 s constant is never trusted blindly);
* at breaker level 1/2/3 the expedited delay is clamped by the LIVE
  cooldown: ``min(normal, max(130, hint))``.
"""
from types import SimpleNamespace

import pytest

import app.exchange.bybit_client as bybit_module
from app.exchange.bybit_client import BybitRateLimitError
from app.exchange import retry_budget

from app.scanners.cycle_completion_v1 import (
    CycleCompletionReport,
    classify_symbol_failure,
    REQUEST_STATE_PRE_SEND_SKIP,
)
from app.scanners.recovery_policy_v1 import (
    RecoveryHintUnavailable,
    breaker_cooldown_hint_seconds,
    decide_recovery,
)

from test_global_bybit_rate_limiter_integration_v1 import (
    ScriptedSession,
    _isolated_limiter,
    _enabled_client,
)
from test_scanner_ac7_recovery_real_report_v1 import execute

#: Long enough that a client deadline can never cover it (pre-send skips).
LONG_COOLDOWN_S = 120.0
#: Short enough that tripping through intermediate levels stays fast.
HOP_COOLDOWN_S = 0.05


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    retry_budget.reset_process_state()

    def blocked_network(*args, **kwargs):
        pytest.fail("REAL_NETWORK_FORBIDDEN")

    monkeypatch.setattr(bybit_module, "urlopen", blocked_network)

    yield

    retry_budget.reset_process_state()


def _client_of(limiter):
    return SimpleNamespace(_shared_limiter=lambda: limiter)


def _open_breaker_at_level(tmp_path, level, *, long_cooldown_s=LONG_COOLDOWN_S):
    """Trip the REAL shared breaker to ``level`` and leave it OPEN.

    ``cooldown_for(level)`` indexes the cooldown table by level, so the
    target level's cooldown is the long one and every hop below it is short.
    """
    assert level in (1, 2, 3)
    cooldowns = [HOP_COOLDOWN_S, HOP_COOLDOWN_S, HOP_COOLDOWN_S]
    cooldowns[level - 1] = long_cooldown_s
    limiter = _isolated_limiter(
        tmp_path,
        breaker_cooldowns_s=tuple(cooldowns),
        max_wait_s=5.0,
    )

    for expected_level in range(1, level + 1):
        out = limiter.acquire("P0")
        assert out.granted
        assert limiter.prepare_send(out.grant_id)
        result = limiter.record_result(
            out.grant_id, "P0", "RATE_LIMIT", epoch=out.epoch,
        )
        assert result == "APPLIED"
        snap = limiter.snapshot()
        assert snap["state"] == "OPEN"
        assert snap["level"] == expected_level

    return limiter


# ---------------------------------------------------------------------------
# breaker_cooldown_hint_seconds — pure read-only semantics
# ---------------------------------------------------------------------------

def test_open_breaker_hint_is_exact_remaining_cooldown(tmp_path):
    limiter = _open_breaker_at_level(tmp_path, 1)
    snap = limiter.snapshot()
    until = snap["cooldown_until_wall"]

    client = _client_of(limiter)
    assert breaker_cooldown_hint_seconds(client, now_wall=until - 60.0) == 60.0
    assert breaker_cooldown_hint_seconds(client, now_wall=until - 0.5) == 0.5
    assert breaker_cooldown_hint_seconds(client, now_wall=until) == 0.0
    # Past the cooldown: nothing left to sleep off.
    assert breaker_cooldown_hint_seconds(
        client, now_wall=until + 1000.0) == 0.0


def test_closed_breaker_hint_is_zero(tmp_path):
    limiter = _isolated_limiter(tmp_path)
    assert limiter.snapshot()["state"] == "CLOSED"
    assert breaker_cooldown_hint_seconds(
        _client_of(limiter), now_wall=0.0) == 0.0


def test_recovery_state_hint_is_zero(tmp_path):
    """RECOVERY already admits probe traffic — no wall-clock cooldown left;
    a rescan is slow (serialized probes) but not futile."""
    import time as _time

    cooldowns = (HOP_COOLDOWN_S,) * 3
    short = _isolated_limiter(
        tmp_path, breaker_cooldowns_s=cooldowns, max_wait_s=5.0)
    out = short.acquire("P0")
    assert out.granted and short.prepare_send(out.grant_id)
    assert short.record_result(
        out.grant_id, "P0", "RATE_LIMIT", epoch=out.epoch) == "APPLIED"
    assert short.snapshot()["state"] == "OPEN"

    _time.sleep(HOP_COOLDOWN_S * 3)

    # The hint is TIME-based, not state-based: even before the lazy
    # OPEN -> RECOVERY transition runs (snapshot() does not refresh), an
    # expired cooldown already yields 0.0 — the rescan has nothing to wait
    # for.
    snap = short.snapshot()
    assert breaker_cooldown_hint_seconds(
        _client_of(short), now_wall=snap["cooldown_until_wall"] + 0.1) == 0.0

    # Force the lazy transition and observe RECOVERY explicitly.
    short.refresh()
    snap = short.snapshot()
    assert snap["state"] == "RECOVERY"
    assert breaker_cooldown_hint_seconds(
        _client_of(short), now_wall=snap["cooldown_until_wall"] + 0.1) == 0.0
    assert breaker_cooldown_hint_seconds(
        _client_of(short), now_wall=0.0) == 0.0


def test_hint_requires_an_accessible_shared_limiter():
    with pytest.raises(RecoveryHintUnavailable):
        breaker_cooldown_hint_seconds(object())          # no accessor
    with pytest.raises(RecoveryHintUnavailable):
        breaker_cooldown_hint_seconds(
            SimpleNamespace(_shared_limiter=lambda: None))   # disabled
    with pytest.raises(RecoveryHintUnavailable):
        breaker_cooldown_hint_seconds(
            SimpleNamespace(_shared_limiter=lambda: object()))  # no snapshot()


@pytest.mark.parametrize("snap", [
    {"enabled": False},
    {"enabled": True, "available": False, "error": "lock_timeout"},
    {"enabled": True, "available": True, "state": "WEIRD"},
    {"enabled": True, "available": True, "state": "OPEN",
     "cooldown_until_wall": None},
    {"enabled": True, "available": True, "state": "OPEN",
     "cooldown_until_wall": float("nan")},
])
def test_unprovable_snapshots_raise_instead_of_guessing(snap):
    limiter = SimpleNamespace(snapshot=lambda: snap)
    client = SimpleNamespace(_shared_limiter=lambda: limiter)
    with pytest.raises(RecoveryHintUnavailable):
        breaker_cooldown_hint_seconds(client, now_wall=0.0)


# ---------------------------------------------------------------------------
# Real breaker levels 1/2/3 -> 50 symbols -> report -> scheduling decision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", [1, 2, 3])
def test_every_breaker_level_blocks_50_symbols(tmp_path, level):
    limiter = _open_breaker_at_level(tmp_path, level)

    session = ScriptedSession()
    client, _ = _enabled_client(session, tmp_path, limiter=limiter)

    report = CycleCompletionReport(expected_symbols=50)
    for i in range(50):
        symbol = f"S{i}USDT"
        with pytest.raises(BybitRateLimitError) as caught:
            client.get_klines(symbol)
        exc = caught.value
        assert exc.limiter_result in ("BREAKER_OPEN", "SKIPPED")
        assert exc.telemetry_result == "BYBIT_API_SKIP"
        outcome = classify_symbol_failure(symbol, exc)
        assert outcome.request_state == REQUEST_STATE_PRE_SEND_SKIP
        report.record_outcome(outcome)

    assert session.calls == []                       # nothing reached HTTP
    assert report.processed_count == 0
    assert len(report.limiter_pre_send_skip_symbols) == 50
    assert report.completion_status == "FAILED"

    snap = limiter.snapshot()
    assert snap["state"] == "OPEN"
    assert snap["level"] == level

    # The real hint for THIS client matches the snapshot formula exactly.
    until = snap["cooldown_until_wall"]
    hint = breaker_cooldown_hint_seconds(client, now_wall=until - 60.0)
    assert hint == 60.0

    # Scheduling decision at this level:
    # - hint below the experimental floor -> floor rules (130 s);
    # - hint above the floor               -> the LIVE cooldown rules;
    # - hint beyond the normal schedule    -> no recovery (not beneficial).
    delay, attempted, events = execute(
        report, enabled=True, attempted=False, hint=hint)
    assert (delay, attempted) == (130.0, True)
    assert not any(level_ == "exception" for level_, _ in events)

    delay, attempted, _ = execute(
        report, enabled=True, attempted=False, hint=200.0)
    assert (delay, attempted) == (200.0, True)

    delay, attempted, _ = execute(
        report, enabled=True, attempted=False, hint=400.0)
    assert (delay, attempted) == (260.0, False)


def test_level3_long_cooldown_clamps_rescan_to_cooldown_expiry(tmp_path):
    """Incident-#297 shape at level 3 with a cooldown LONGER than the
    experimental 130 s floor: the rescan is clamped to the cooldown so it
    can never burn its one shot on a provably-closed breaker (the bare 130 s
    constant would)."""
    limiter = _open_breaker_at_level(tmp_path, 3, long_cooldown_s=200.0)
    snap = limiter.snapshot()
    until = snap["cooldown_until_wall"]

    client = _client_of(limiter)
    hint = breaker_cooldown_hint_seconds(client, now_wall=until - 200.0)
    assert hint == 200.0

    decision = decide_recovery(
        enabled=True,
        expected_symbols=50,
        processed_symbols=0,
        limiter_pre_send_skips=50,
        previous_recovery_attempted=False,
        normal_delay_seconds=260.0,
        limiter_cooldown_hint_seconds=hint,
    )
    assert decision.expedite
    assert decision.delay_seconds == 200.0      # clamped to the cooldown
    assert decision.delay_seconds > 130.0       # never earlier than it


def test_hint_reads_only_snapshot_no_mutation(tmp_path):
    """The hint helper must be provably read-only: no acquire, no
    record_result, no state change."""
    limiter = _open_breaker_at_level(tmp_path, 2)
    before = limiter.snapshot()

    client = _client_of(limiter)
    for now_wall in (before["cooldown_until_wall"] - 1.0,
                     before["cooldown_until_wall"],
                     before["cooldown_until_wall"] + 10.0):
        breaker_cooldown_hint_seconds(client, now_wall=now_wall)

    after = limiter.snapshot()
    for key in ("state", "level", "epoch", "cooldown_until_wall",
                "total_grants", "pending_count", "grants_count"):
        assert after[key] == before[key], key
