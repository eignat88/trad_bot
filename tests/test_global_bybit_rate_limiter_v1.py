"""GLOBAL_BYBIT_RATE_LIMITER_V1 core — deterministic unit tests (Issue #231, Phase 3).

Local / deterministic coverage for the shared limiter core. The core has NO
transport at all, so every "attempt" here is a grant/record round-trip over
a temp state file — no Bybit, no network, no production configuration.

Cross-process and crash/SIGSTOP evidence lives in
``tests/test_global_bybit_rate_limiter_mp_v1.py`` (Linux evidence required
before merge — see the deliverable status).

Contract coverage mapped to Phase 3 §4:
    2  priority fairness (P0–P3 order, FIFO, borrowing, no starvation)
    4  20 unique OK / duplicate-result suppression (scaled target)
    7  corrupt state + fsync failure → FAIL_CLOSED
    8  clock jumps / reboot re-baseline (boot change)
    9  pending TTL + crash (dead pid / foreign boot) cleanup
    10 terminal grant eviction + bounded registry (capacity fail-closed)
    11 deadline-aware wait
    12 default-off: zero shared-state I/O
    3.1 live probe holder blocks new probes past lease (single-process part)
    3.2 grant lifecycle ISSUED→TERMINAL exactly once
    3.3 accounting invariants (5 counters + exact token identity)
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from app.exchange import global_rate_limiter as grl
from app.exchange.global_rate_limiter import (
    GrantResult,
    GlobalBybitRateLimiter,
    LimiterConfig,
)


class FakeClock:
    """Deterministic clock: sleep() advances fake time instantly."""

    def __init__(self, wall: float = 1_700_000_000.0, mono: float = 1000.0):
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        s = max(0.0, float(seconds))
        self._wall += s
        self._mono += s

    def jump(self, delta: float) -> None:
        """Simulate a wall-clock set (monotonic source untouched)."""
        self._wall += float(delta)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


def _cfg(tmp_path, **over) -> LimiterConfig:
    base = dict(enabled=True, state_path=str(tmp_path / "state.json"))
    base.update(over)
    return LimiterConfig(**base)


def _lim(tmp_path, clock, **over) -> GlobalBybitRateLimiter:
    return GlobalBybitRateLimiter(_cfg(tmp_path, **over), clock=clock)


def _raw(tmp_path) -> dict:
    with open(tmp_path / "state.json", encoding="utf-8") as fh:
        return json.load(fh)


# =====================================================================
# 12. default-off: zero shared-state I/O
# =====================================================================
def test_default_off_zero_shared_state_io(tmp_path):
    cfg = LimiterConfig(state_path=str(tmp_path / "off.json"))  # enabled=False
    lim = GlobalBybitRateLimiter(cfg, clock=FakeClock())
    out = lim.acquire("P0", deadline=lim.clock.mono() + 1.0)
    assert out.result is GrantResult.GRANTED
    assert out.grant_id == "-"
    assert lim.record_result("e1:1", "P0", "OK", epoch=1) == "DISABLED"
    assert lim.prepare_send("e1:1") is True
    assert lim.snapshot() == {"enabled": False}
    assert lim.refresh() == {"enabled": False}
    assert not (tmp_path / "off.json").exists()
    assert not (tmp_path / "off.json.lock").exists()


def test_api_validation(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    with pytest.raises(ValueError):
        lim.acquire("P9", deadline=clock.mono() + 1.0)
    with pytest.raises(ValueError):
        lim.record_result("e1:1", "P0", "NOT_A_RESULT")
    with pytest.raises(ValueError):
        LimiterConfig(enabled=True, g_rps=0.0)
    with pytest.raises(ValueError):
        # capacity below 2·G·max_lifetime (R4-2 retention rule)
        LimiterConfig(enabled=True, registry_capacity=1, max_lifetime_s=60.0)


# =====================================================================
# Burst / sustained budget (Theorem 1, deterministic fake time)
# =====================================================================
def test_burst_of_12_then_deadline_skipped(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=8.0, burst=12.0)
    gids = []
    for _ in range(12):
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted, out
        gids.append(out.grant_id)
    assert len(set(gids)) == 12

    # 13th: the next token arrives at ~0.125 fake-s > 0.10 deadline
    out = lim.acquire("P1", deadline=clock.mono() + 0.10)
    assert out.result is GrantResult.SKIPPED
    assert out.reason == "deadline"
    assert out.waited_ms >= 95
    assert out.retry_after_ms >= 0
    assert lim.snapshot()["pending_count"] == 0     # demand cleaned up

    out2 = lim.acquire("P1", deadline=clock.mono() + 5.0)
    assert out2.granted                            # next token granted


def test_sustained_budget_grants_cannot_exceed_G_times_T_plus_B(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=8.0, burst=12.0)
    t0 = clock.mono()
    for i in range(60):
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted, (i, out)
        assert lim.record_result(out.grant_id, "P1", "OK",
                                 epoch=out.epoch) == "APPLIED"
    elapsed = clock.mono() - t0
    # 60 grants ≤ G·T + B  ⇒  T ≥ (60 − 12)/8 = 6.0 s (jitter adds more)
    assert 5.5 <= elapsed <= 9.0, elapsed


# =====================================================================
# 3.3 accounting invariants
# =====================================================================
def test_accounting_invariants_after_grants_and_trip(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(5.0, 30.0, 120.0))
    for _ in range(9):
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted
        assert lim.record_result(out.grant_id, "P1", "OK",
                                 epoch=out.epoch) == "APPLIED"
    clock.advance(1.0)
    out = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out.granted
    assert lim.record_result(out.grant_id, "P0", "RATE_LIMIT",
                             epoch=out.epoch) == "APPLIED"   # trip → epoch++

    st = grl._validate(_raw(tmp_path))          # checksum + schema valid
    tokens = float(st["tokens"])
    ref = float(st["refill_total"])
    tot = int(st["total_grants"])
    # exact identity: tokens == B + refill_total − total_grants
    assert abs(tokens - (12.0 + ref - tot)) < 1e-6, (tokens, ref, tot)
    # per-epoch: one GRANTED == one token == usage +1
    assert sum(int(v) for v in st["usage_by_priority"].values()) == \
        int(st["epoch_grants"])
    assert int(st["epoch_grants"]) == 0                       # trip reset
    cur_ep = int(st["epoch"])
    in_epoch = sum(1 for gid in st["grants"]
                   if GlobalBybitRateLimiter._gid_epoch(gid) == cur_ep)
    assert in_epoch == int(st["epoch_grants"])
    snap = lim.snapshot()
    assert snap["state"] == "OPEN" and snap["level"] == 1
    assert snap["success_streak"] == 0
    assert int(st["total_grants"]) == 10


# =====================================================================
# 2. priority fairness: order, FIFO, borrowing (white-box selection)
# =====================================================================
def test_selection_priority_fifo_and_borrow_reserve(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    now = clock.wall()
    pid = os.getpid()
    boot = grl.current_boot_id()

    def entry(prio: str, t: float) -> dict:
        return {"prio": prio, "pid": pid, "boot_id": boot,
                "registered_wall": t, "expire_wall": now + 1000.0,
                "lrid": ""}

    with lim._transaction(write=True) as st:
        st["pending"]["p3a"] = entry("P3", now + 1.0)
        st["pending"]["p0new"] = entry("P0", now + 5.0)
        st["pending"]["p0old"] = entry("P0", now + 2.0)
        st["tokens"] = 5.0
        # strict priority among eligible waiters + FIFO inside the class
        assert lim._best_eligible(st) == ("P0", "p0old")
        st["pending"].pop("p0old")
        st["pending"].pop("p0new")
        assert lim._best_eligible(st) == ("P3", "p3a")

        # nobody eligible (usage far above curve) → borrow path only
        st["pending"].clear()
        st["pending"]["p0b"] = entry("P0", now)
        st["pending"]["p3b"] = entry("P3", now + 1.0)
        st["usage_by_priority"]["P0"] = 10_000
        st["usage_by_priority"]["P3"] = 10_000
        st["epoch_start_wall"] = now - 1.0
        st["tokens"] = 1.0
        assert lim._best_eligible(st) is None
        # reserve: with a single token no borrow may starve another
        # backlogged class (tokens − 1 < 1 other class)
        assert lim._borrow_candidate(st) is None
        st["tokens"] = 3.0
        # factually free capacity → borrow in priority order
        assert lim._borrow_candidate(st) == ("P0", "p0b")
        lim._set_dirty()


def test_priority_p0_served_before_p3_under_contention(tmp_path):
    # g=1.0 keeps a wide margin between burst drain and the first refilled
    # token, so the registration order (P3 first) is unambiguous.
    lim = GlobalBybitRateLimiter(LimiterConfig(
        enabled=True, state_path=str(tmp_path / "thr.json"),
        g_rps=1.0, burst=4.0))
    results: dict[str, tuple[float, object]] = {}

    def run(prio: str, key: str) -> None:
        out = lim.acquire(prio, deadline=time.monotonic() + 8.0)
        results[key] = (time.monotonic(), out)

    for _ in range(4):                       # drain the burst (real time)
        assert lim.acquire("P1", deadline=time.monotonic() + 5.0).granted
    t3 = threading.Thread(target=run, args=("P3", "p3"))
    t3.start()
    time.sleep(0.15)                         # P3 registers first
    t0 = threading.Thread(target=run, args=("P0", "p0"))
    t0.start()
    t3.join(10)
    t0.join(10)
    assert not t3.is_alive() and not t0.is_alive()
    assert results["p0"][1].granted and results["p3"][1].granted
    # strict priority: the later-registered P0 wins the first refilled token
    assert results["p0"][0] < results["p3"][0]
    assert results["p0"][1].grant_id != results["p3"][1].grant_id


def test_fifo_within_class_under_contention(tmp_path):
    lim = GlobalBybitRateLimiter(LimiterConfig(
        enabled=True, state_path=str(tmp_path / "fifo.json"),
        g_rps=1.0, burst=4.0))
    results: dict[str, float] = {}

    def run(key: str) -> None:
        out = lim.acquire("P0", deadline=time.monotonic() + 8.0)
        assert out.granted, out
        results[key] = time.monotonic()

    for _ in range(4):
        assert lim.acquire("P1", deadline=time.monotonic() + 5.0).granted
    ta = threading.Thread(target=run, args=("a",))
    ta.start()
    time.sleep(0.15)                         # A registers first
    tb = threading.Thread(target=run, args=("b",))
    tb.start()
    ta.join(10)
    tb.join(10)
    assert not ta.is_alive() and not tb.is_alive()
    assert results["a"] < results["b"]       # FIFO by registration order


def test_work_conserving_borrow_for_lone_overcurve_class(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    for _ in range(12):                      # P2 drains the whole bucket
        out = lim.acquire("P2", deadline=clock.mono() + 5.0)
        assert out.granted, out
    snap = lim.snapshot()
    assert snap["usage_by_priority"]["P2"] == 12   # usage == grants (§3.3)
    # nobody else demands capacity → an over-curve lone class still gets
    # grants (work-conserving borrow), it must not starve on its curve alone
    out = lim.acquire("P2", deadline=clock.mono() + 5.0)
    assert out.granted
    assert lim.snapshot()["usage_by_priority"]["P2"] == 13


# =====================================================================
# Shared breaker: trip / OPEN → RECOVERY / probe slot / 20 unique OK / dedup
# =====================================================================
def test_breaker_trip_recovery_unique_ok_and_dedup(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock,
               breaker_cooldowns_s=(0.05, 0.1, 0.2),
               recovery_success_target=3)
    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert g1.granted
    assert lim.prepare_send(g1.grant_id) is True
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT",
                             epoch=g1.epoch) == "APPLIED"

    snap = lim.snapshot()
    assert snap["state"] == "OPEN" and snap["level"] == 1
    assert snap["epoch"] == g1.epoch + 1               # epoch fencing
    assert snap["success_streak"] == 0
    assert abs(snap["cooldown_until_wall"] - (clock.wall() + 0.05)) < 1e-9
    assert snap["epoch_grants"] == 0
    assert sum(snap["usage_by_priority"].values()) == 0

    # P3 defers immediately while OPEN
    out = lim.acquire("P3", deadline=clock.mono() + 0.2)
    assert out.result is GrantResult.BREAKER_OPEN
    assert out.reason == "breaker_open"
    assert lim.snapshot()["pending_count"] == 0

    # P0 waits through the cooldown (deadline covers it) → RECOVERY probe
    out = lim.acquire("P0", deadline=clock.mono() + 1.0)
    assert out.granted and out.probe
    snap = lim.snapshot()
    assert snap["state"] == "RECOVERY"
    gid, ep = out.grant_id, out.epoch
    assert snap["probe"]["holder"] == gid

    # live holder keeps the slot: a second process/class cannot admit
    out2 = lim.acquire("P1", deadline=clock.mono() + 0.3)
    assert out2.result is GrantResult.SKIPPED
    assert lim.snapshot()["probe"]["holder"] == gid

    # holder result → streak; replay → DUPLICATE, streak unchanged
    assert lim.record_result(gid, "P0", "OK", epoch=ep) == "APPLIED"
    assert lim.snapshot()["success_streak"] == 1
    assert lim.record_result(gid, "P0", "OK", epoch=ep) == "DUPLICATE"
    assert lim.snapshot()["success_streak"] == 1
    assert lim.snapshot()["probe"] is None

    # more UNIQUE successes → target → CLOSED / level 0
    g = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert g.granted and g.probe
    assert lim.record_result(g.grant_id, "P0", "OK", epoch=g.epoch) == "APPLIED"
    assert lim.snapshot()["success_streak"] == 2
    g = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert g.granted and g.probe
    assert lim.record_result(g.grant_id, "P0", "OK", epoch=g.epoch) == "APPLIED"
    snap = lim.snapshot()
    assert snap["state"] == "CLOSED"
    assert snap["level"] == 0 and snap["success_streak"] == 0
    # out of recovery: ordinary non-probe grant
    g = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert g.granted and not g.probe


# =====================================================================
# 3.2 grant lifecycle + fencing + idempotency
# =====================================================================
def test_unknown_grant_result_never_touches_breaker(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    before = lim.snapshot()
    res = lim.record_result("e1:999999", "P0", "OK", epoch=1)
    assert res == "UNKNOWN_GRANT"
    after = lim.snapshot()
    for key in ("state", "level", "epoch", "success_streak",
                "cooldown_until_wall", "probe", "total_grants",
                "epoch_grants", "tokens"):
        assert after[key] == before[key]
    assert after["anomalies"]["unknown_grant"] == \
        before["anomalies"]["unknown_grant"] + 1


def test_stale_epoch_ok_does_not_advance_streak(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    a = lim.acquire("P0", deadline=clock.mono() + 5.0)
    b = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert a.granted and b.granted and a.epoch == b.epoch
    assert lim.record_result(b.grant_id, "P0", "RATE_LIMIT",
                             epoch=b.epoch) == "APPLIED"   # trip → epoch+1
    assert lim.snapshot()["epoch"] == a.epoch + 1

    assert lim.record_result(a.grant_id, "P0", "OK",
                             epoch=a.epoch) == "APPLIED"   # terminal once
    snap = lim.snapshot()
    assert snap["success_streak"] == 0          # stale success NOT counted
    assert snap["state"] == "OPEN"              # nothing else changed
    assert lim.grant_outcome(a.grant_id) == "OK"
    assert lim.record_result(a.grant_id, "P0", "OK",
                             epoch=a.epoch) == "DUPLICATE"
    assert lim.snapshot()["success_streak"] == 0


def test_prepare_send_revokes_grant_after_trip(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    a = lim.acquire("P0", deadline=clock.mono() + 5.0)
    b = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert a.granted and b.granted
    assert lim.record_result(b.grant_id, "P0", "RATE_LIMIT",
                             epoch=b.epoch) == "APPLIED"
    # outstanding grant from the fenced epoch must not reach the wire
    assert lim.prepare_send(a.grant_id) is False
    assert lim.grant_outcome(a.grant_id) == "REVOKED"
    assert lim.record_result(a.grant_id, "P0", "OK",
                             epoch=a.epoch) == "DUPLICATE"


def test_prepare_send_marks_probe_send_marker(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(0.05, 0.1, 0.2))
    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT",
                             epoch=g1.epoch) == "APPLIED"
    out = lim.acquire("P0", deadline=clock.mono() + 1.0)
    assert out.granted and out.probe
    assert lim.prepare_send(out.grant_id) is True
    snap = lim.snapshot()
    assert snap["probe"]["marker"] is True
    assert snap["probe"]["holder"] == out.grant_id
    assert lim.record_result(out.grant_id, "P0", "OK",
                             epoch=out.epoch) == "APPLIED"


def test_registry_capacity_fail_closed_then_eviction(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, registry_capacity=3, max_lifetime_s=0.05)
    gids = []
    for _ in range(3):
        out = lim.acquire("P0", deadline=clock.mono() + 5.0)
        assert out.granted
        gids.append(out.grant_id)
    out4 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out4.result is GrantResult.STATE_UNAVAILABLE
    assert out4.reason == "registry_capacity"      # bounded state, fail-closed
    assert lim.snapshot()["pending_count"] == 0

    # terminal + aged → evictable (R4-2 safe removal condition)
    for gid in gids:
        assert lim.record_result(gid, "P0", "NOT_SENT",
                                 epoch=1) == "APPLIED"
    clock.advance(0.10)                            # > max_lifetime_s
    lim.refresh()
    assert lim.snapshot()["grants_count"] == 0
    out5 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out5.granted                            # capacity restored


# =====================================================================
# 7. corrupt state / fsync failure → FAIL_CLOSED, never torn
# =====================================================================
def test_corrupt_state_fails_closed_and_manual_restore_recovers(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    out = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out.granted                              # valid state created
    valid = (tmp_path / "state.json").read_text(encoding="utf-8")
    (tmp_path / "state.json").write_text("{ not json", encoding="utf-8")

    out2 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out2.result is GrantResult.STATE_UNAVAILABLE
    assert out2.reason == "corrupt_state"
    assert lim.record_result("e1:1", "P0", "OK") == "STATE_UNAVAILABLE"
    assert lim.prepare_send("e1:1") is False
    snap = lim.snapshot()
    assert snap["available"] is False and snap["error"] == "corrupt_state"

    # safe restart after restoring a known-good state
    (tmp_path / "state.json").write_text(valid, encoding="utf-8")
    out3 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out3.granted


def test_fsync_failure_fails_closed_without_torn_state(tmp_path, monkeypatch):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    out = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out.granted
    before = grl._validate(_raw(tmp_path))

    def boom(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    out2 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out2.result is GrantResult.STATE_UNAVAILABLE
    assert out2.reason == "write_failed"
    monkeypatch.undo()

    after = grl._validate(_raw(tmp_path))          # previous version intact
    assert after["total_grants"] == before["total_grants"]
    assert not [n for n in os.listdir(tmp_path) if ".tmp." in n]
    out3 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out3.granted                            # recovery after fsync OK


# =====================================================================
# 8. clock jumps / reboot — no extra tokens, cooldown never shortened
# =====================================================================
def test_forward_clock_jump_mints_no_tokens_and_keeps_cooldown(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, burst=12.0,
               breaker_cooldowns_s=(5.0, 30.0, 120.0))
    gids = []
    for _ in range(12):                            # drain bucket while CLOSED
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted
        gids.append((out.grant_id, out.epoch))
    gid, ep = gids[-1]
    assert lim.record_result(gid, "P1", "RATE_LIMIT", epoch=ep) == "APPLIED"
    snap = lim.snapshot()
    assert snap["state"] == "OPEN"
    assert abs(snap["cooldown_until_wall"] - (clock.wall() + 5.0)) < 1e-9

    clock.jump(3600)                               # wall set forward 1 h
    lim.refresh()
    snap = lim.snapshot()
    assert snap["tokens"] < 1.0                    # NO +28800-token mint
    assert snap["state"] == "OPEN"
    assert snap["cooldown_until_wall"] >= clock.wall() + 4.9  # re-armed

    out = lim.acquire("P0", deadline=clock.mono() + 0.5)
    assert out.result is GrantResult.BREAKER_OPEN
    assert out.reason == "breaker_cooldown_exceeds_deadline"


def test_backward_clock_jump_freezes_refill(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, burst=12.0)
    for _ in range(12):
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted
    assert lim.snapshot()["tokens"] < 1.0
    clock.jump(-3600)
    lim.refresh()
    assert lim.snapshot()["tokens"] < 1.0          # frozen, no credit
    clock.jump(+3600)                              # back to origin: Δ = 0
    lim.refresh()
    assert lim.snapshot()["tokens"] < 1.0          # still no credit


def test_boot_change_rebaselines_and_prunes_foreign_pending(tmp_path,
                                                            monkeypatch):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    assert lim.acquire("P0", deadline=clock.mono() + 5.0).granted
    now = clock.wall()
    with lim._transaction(write=True) as st:
        st["pending"]["old"] = {
            "prio": "P3", "pid": os.getpid(),
            "boot_id": grl.current_boot_id(),
            "registered_wall": now, "expire_wall": now + 1000.0,
            "lrid": ""}
        st["last_wall"] = now - 100.0              # stale pre-reboot point
        lim._set_dirty()
    before_tok = lim.snapshot()["tokens"]
    assert lim.snapshot()["pending_count"] == 1

    monkeypatch.setattr(grl, "current_boot_id", lambda: "rebooted-boot")
    snap = lim.refresh()
    assert snap["pending_count"] == 0              # foreign-boot demand gone
    assert snap["tokens"] == before_tok            # reboot mints no credit
    assert snap["available"] is True


# =====================================================================
# 9. pending TTL / dead-owner cleanup
# =====================================================================
def test_pending_ttl_foreign_boot_and_dead_pid_prune(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, demand_ttl_s=120.0)
    pid = os.getpid()
    boot = grl.current_boot_id()

    def entry(expire_delta: float, boot_id: str, owner: int) -> dict:
        now = clock.wall()
        return {"prio": "P0", "pid": owner, "boot_id": boot_id,
                "registered_wall": now, "expire_wall": now + expire_delta,
                "lrid": ""}

    with lim._transaction(write=True) as st:
        st["pending"]["expired"] = entry(-1.0, boot, pid)
        st["pending"]["foreign"] = entry(120.0, "other-boot", pid)
        st["pending"]["alive"] = entry(120.0, boot, pid)
        if os.name == "posix":
            st["pending"]["dead"] = entry(120.0, boot, 99_999_999)
        lim._set_dirty()

    snap = lim.refresh()
    # only the live same-boot owner survives: expired + foreign-boot are
    # always pruned; on POSIX the dead pid goes as well (it is never
    # inserted on Windows, where pid probing is unsupported)
    assert snap["pending_count"] == 1
    assert snap["anomalies"]["pending_pruned"] >= 2


# =====================================================================
# 11. deadline-aware wait
# =====================================================================
def test_deadline_aware_wait_returns_at_deadline(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=8.0, burst=12.0)
    for _ in range(12):
        assert lim.acquire("P1", deadline=clock.mono() + 5.0).granted
    out = lim.acquire("P1", deadline=clock.mono() + 0.05)
    assert out.result is GrantResult.SKIPPED
    assert 45 <= out.waited_ms <= 150, out.waited_ms
    assert out.retry_after_ms >= 0
    assert lim.snapshot()["pending_count"] == 0


# =====================================================================
# Safe restart: counters survive a brand-new instance on the same state
# =====================================================================
def test_restart_preserves_counters_and_identity(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    for _ in range(5):
        assert lim.acquire("P0", deadline=clock.mono() + 5.0).granted
    out = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out.granted
    assert lim.record_result(out.grant_id, "P0", "RATE_LIMIT",
                             epoch=out.epoch) == "APPLIED"
    before = lim.snapshot()

    lim2 = GlobalBybitRateLimiter(_cfg(tmp_path), clock=clock)  # restart
    after = lim2.snapshot()
    for key in ("state", "level", "epoch", "total_grants",
                "epoch_grants", "refill_total", "tokens"):
        assert after[key] == before[key]
    st = grl._validate(_raw(tmp_path))
    assert abs(float(st["tokens"]) -
               (12.0 + float(st["refill_total"]) -
                int(st["total_grants"]))) < 1e-6


# =====================================================================
# 3.1 (single-process part): a LIVE probe holder blocks new probes even
# past its lease — time alone never frees the recovery slot.
# =====================================================================
def test_live_probe_holder_blocks_new_probes_past_lease(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock,
               breaker_cooldowns_s=(0.05, 0.1, 0.2),
               probe_lease_s=0.05,
               recovery_success_target=5)
    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert g1.granted
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT",
                             epoch=g1.epoch) == "APPLIED"
    p = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert p.granted and p.probe
    assert lim.prepare_send(p.grant_id) is True        # marker set

    # holder (this process) stays alive; lease + stall horizon pass
    clock.advance(0.20)
    snap = lim.refresh()
    assert snap["state"] == "RECOVERY"                 # NOT released on time
    assert snap["probe"] is not None
    assert snap["probe"]["holder"] == p.grant_id
    assert snap["anomalies"]["probe_stalled"] >= 1     # anomaly only

    # a second probe is refused while the holder is provably alive
    out = lim.acquire("P1", deadline=clock.mono() + 0.2)
    assert out.result is GrantResult.SKIPPED
    assert lim.snapshot()["probe"]["holder"] == p.grant_id

    # the holder's own result releases the slot normally
    assert lim.record_result(p.grant_id, "P0", "OK",
                             epoch=p.epoch) == "APPLIED"
    assert lim.snapshot()["probe"] is None
    q = lim.acquire("P1", deadline=clock.mono() + 2.0)
    assert q.granted and q.probe


# =====================================================================
# Phase 3D remediation regressions (independent code review F1–F8)
# =====================================================================

# ---------------------------------------------------------------- F1
def test_rate_limit_burst_same_epoch_single_escalation(tmp_path):
    """F1: N RATE_LIMITs from ONE epoch escalate the breaker exactly once."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(5.0, 30.0, 120.0))
    gs = [lim.acquire("P0", deadline=clock.mono() + 5.0) for _ in range(3)]
    assert all(x.granted for x in gs)
    ep0 = gs[0].epoch
    assert all(x.epoch == ep0 for x in gs)          # same (pre-trip) epoch

    for i, x in enumerate(gs):
        assert lim.record_result(x.grant_id, "P0", "RATE_LIMIT") == "APPLIED"
        snap = lim.snapshot()
        # only the FIRST (current-epoch) trip escalates; the stale two are
        # terminal diagnostic evidence with NO re-trip and NO escalation
        assert snap["level"] == 1, (i, snap["level"])
        assert snap["epoch"] == ep0 + 1, (i, snap["epoch"])
        assert lim.grant_outcome(x.grant_id) == "RATE_LIMIT"  # terminal once
    snap = lim.snapshot()
    assert snap["state"] == "OPEN"
    assert snap["success_streak"] == 0
    # the single escalation lands on the FIRST rung of the 5→30→120 ladder
    assert abs(snap["cooldown_until_wall"] - (clock.wall() + 5.0)) < 1e-9
    # a replay of any of them stays a no-op (terminal-once by grant_id)
    assert lim.record_result(gs[0].grant_id, "P0", "RATE_LIMIT") == "DUPLICATE"
    assert lim.snapshot()["level"] == 1


def test_independent_rate_limit_episodes_escalate_cooldown(tmp_path):
    """F1: fresh (current-epoch) trips across episodes escalate 1→2→3."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(0.05, 0.1, 0.2))

    def episode(cooldown_before: float, expect_level: int) -> None:
        clock.advance(cooldown_before)               # walk into RECOVERY
        p = lim.acquire("P0", deadline=clock.mono() + 2.0)
        assert p.granted and p.probe                 # one probe per episode
        assert p.epoch == lim.snapshot()["epoch"]    # current epoch → fresh
        assert lim.record_result(p.grant_id, "P0", "RATE_LIMIT") == "APPLIED"
        assert lim.snapshot()["level"] == expect_level

    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT") == "APPLIED"
    assert lim.snapshot()["level"] == 1              # episode 1 → level 1
    episode(0.06, 2)                                 # episode 2 → level 2
    episode(0.11, 3)                                 # episode 3 → level 3
    episode(0.21, 3)                                 # capped at last rung


def test_cooldown_ladder_default_mapping():
    """F1: the approved 5→30→120 ladder maps to levels 1→2→3 (then caps)."""
    cfg = LimiterConfig()                            # default cooldowns
    assert cfg.cooldown_for(1) == 5.0
    assert cfg.cooldown_for(2) == 30.0
    assert cfg.cooldown_for(3) == 120.0
    assert cfg.cooldown_for(4) == 120.0              # capped at last rung


# ---------------------------------------------------------------- F2
def test_dead_owner_grant_reconciled_bounded(tmp_path):
    """F2: a provably dead owner's non-terminal grant closes neutrally."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, registry_capacity=8, max_lifetime_s=0.05)
    g = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert g.granted
    now = clock.wall()
    with lim._transaction(write=True) as st:
        # foreign-boot owner, NO send-marker → provably never sent
        st["grants"]["e1:900"] = {
            "prio": "P0", "issued_wall": now, "outcome": None, "probe": False,
            "pid": 99_999_999, "boot_id": "other-boot", "sent": False}
        # foreign-boot owner, send-marker set → indeterminate, NOT NOT_SENT
        st["grants"]["e1:901"] = {
            "prio": "P0", "issued_wall": now, "outcome": None, "probe": False,
            "pid": 99_999_999, "boot_id": "other-boot", "sent": True}
        lim._set_dirty()
    snap = lim.refresh()
    assert lim.grant_outcome("e1:900") == "NOT_SENT"   # no marker
    assert lim.grant_outcome("e1:901") == "UNKNOWN"    # marker → indeterminate
    assert lim.grant_outcome(g.grant_id) is None       # our live grant intact
    # neutral: no success streak, no breaker change, no token refund
    assert snap["success_streak"] == 0
    assert snap["state"] == "CLOSED"
    # reconciliation refunded no token: clock has not advanced, so tokens is
    # still exactly burst(12) − 1 real grant, untouched by the 2 closures
    assert snap["tokens"] == 11.0
    assert snap["total_grants"] == 1
    # retention (age > max_lifetime) evicts the closed records → bounded
    clock.advance(0.10)
    lim.refresh()
    assert lim.grant_outcome("e1:900") == "ABSENT"
    assert lim.grant_outcome("e1:901") == "ABSENT"
    assert lim.grant_outcome(g.grant_id) is None       # non-terminal survives
    assert lim.snapshot()["grants_count"] == 1         # only our live grant


# ---------------------------------------------------------------- F3
def test_record_result_transient_write_failure_retries(tmp_path, monkeypatch):
    """F3: a transient fsync failure is retried and applied (no HTTP)."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    g = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert g.granted
    calls = {"n": 0}
    real_fsync = os.fsync

    def flaky(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    assert lim.record_result(g.grant_id, "P0", "OK", epoch=g.epoch) == "APPLIED"
    monkeypatch.undo()
    assert calls["n"] >= 2                           # it actually retried
    assert lim.grant_outcome(g.grant_id) == "OK"


def test_record_result_exhausted_retry_fails_closed_then_restart(tmp_path,
                                                                 monkeypatch):
    """F3: exhausting the bounded retry fails closed; a restart applies once."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(5.0, 30.0, 120.0),
               record_retry_attempts=2, record_retry_backoff_s=0.0)
    g = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert g.granted
    before = lim.snapshot()

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    res = lim.record_result(g.grant_id, "P0", "RATE_LIMIT", epoch=g.epoch)
    assert res == "STATE_UNAVAILABLE"                 # fail-closed, exhausted
    monkeypatch.undo()
    # nothing applied: grant still ISSUED, breaker unchanged (no escalation)
    assert lim.grant_outcome(g.grant_id) is None
    after = lim.snapshot()
    assert after["level"] == before["level"] == 0
    assert after["state"] == "CLOSED"
    # restart on the same durable state applies exactly ONE transition
    lim2 = GlobalBybitRateLimiter(_cfg(tmp_path), clock=clock)
    assert lim2.record_result(g.grant_id, "P0", "RATE_LIMIT",
                              epoch=g.epoch) == "APPLIED"
    assert lim2.snapshot()["level"] == 1              # single, not doubled


def test_record_result_failure_keeps_probe_slot_held(tmp_path, monkeypatch):
    """F3: a failed probe result does NOT release the slot; a retry frees it."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(0.05, 0.1, 0.2),
               record_retry_attempts=2, record_retry_backoff_s=0.0)
    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT",
                             epoch=g1.epoch) == "APPLIED"
    clock.advance(0.06)
    p = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert p.granted and p.probe
    assert lim.prepare_send(p.grant_id) is True

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    assert lim.record_result(p.grant_id, "P0", "OK",
                             epoch=p.epoch) == "STATE_UNAVAILABLE"
    monkeypatch.undo()
    # probe slot still held (fail-closed) — not released on the failure
    snap = lim.snapshot()
    assert snap["probe"] is not None
    assert snap["probe"]["holder"] == p.grant_id
    # the holder's clean retry frees the slot and counts the success ONCE
    assert lim.record_result(p.grant_id, "P0", "OK", epoch=p.epoch) == "APPLIED"
    assert lim.snapshot()["probe"] is None
    assert lim.snapshot()["success_streak"] == 1


# ---------------------------------------------------------------- F4
def test_snapshot_is_read_only_and_stable(tmp_path):
    """F4: snapshot never mutates/refreshes state; repeated reads are stable."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    assert lim.acquire("P0", deadline=clock.mono() + 5.0).granted
    raw_before = _raw(tmp_path)
    s1 = lim.snapshot()
    s2 = lim.snapshot()
    assert s1["available"] and s2["available"]
    assert s1["total_grants"] == s2["total_grants"] == 1
    # a read-only op must not write anything back
    assert _raw(tmp_path) == raw_before


# ---------------------------------------------------------------- F5
def test_abandoned_pending_pruned_within_liveness_not_ttl(tmp_path):
    """F5: an abandoned demand lapses at the liveness window, not demand_ttl."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, demand_ttl_s=120.0, pending_liveness_ttl_s=5.0)
    now = clock.wall()
    pid = os.getpid()
    boot = grl.current_boot_id()
    # a demand registered then abandoned by a still-live owner: registration
    # caps expire_wall at now + liveness (NOT now + demand_ttl)
    with lim._transaction(write=True) as st:
        st["pending"]["abandoned"] = {
            "prio": "P0", "pid": pid, "boot_id": boot,
            "registered_wall": now, "expire_wall": now + 5.0, "lrid": ""}
        lim._set_dirty()
    assert lim.snapshot()["pending_count"] == 1
    clock.jump(5.5)                                  # far short of 120 s TTL
    snap = lim.refresh()
    assert snap["pending_count"] == 0                # pruned by LIVENESS


def test_live_waiter_renews_pending_across_liveness_window(tmp_path):
    """F5: a live waiter renews its demand so it is NOT pruned while waiting."""
    clock = FakeClock()
    # refill 0.5/s → the 2nd grant is not available until ~2 s, past the
    # 1 s liveness window; only renewal keeps the demand alive that long.
    lim = _lim(tmp_path, clock, g_rps=0.5, burst=1.0,
               pending_liveness_ttl_s=1.0, demand_ttl_s=1000.0)
    assert lim.acquire("P1", deadline=clock.mono() + 5.0).granted  # drain
    out = lim.acquire("P1", deadline=clock.mono() + 10.0)
    assert out.granted                               # survived via renewal
    assert clock.mono() > 1.0                        # indeed waited past it


# ---------------------------------------------------------------- F6
def test_prune_witness_files_removes_only_resolved_unlocked(tmp_path):
    """F6: prune removes only resolved, unlocked witnesses; keeps active."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, breaker_cooldowns_s=(0.05, 0.1, 0.2))
    g1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert lim.record_result(g1.grant_id, "P0", "RATE_LIMIT",
                             epoch=g1.epoch) == "APPLIED"
    clock.advance(0.06)                              # → RECOVERY
    p = lim.acquire("P0", deadline=clock.mono() + 2.0)
    assert p.granted and p.probe
    wfiles = list(tmp_path.glob("*.witness"))
    assert len(wfiles) == 1
    # unresolved probe → its witness must be KEPT (never unlink active)
    assert lim.prune_witness_files() == 0
    assert wfiles[0].exists()
    # resolve the probe (terminal) → the witness is now provably inert
    assert lim.record_result(p.grant_id, "P0", "OK", epoch=p.epoch) == "APPLIED"
    if os.name == "posix":
        # POSIX: unlink-while-lock-held removes the inert witness safely
        assert lim.prune_witness_files() >= 1
        assert not wfiles[0].exists()
    else:
        # Phase 3E: non-POSIX cannot unlink-while-lock safely → cleanup is
        # DISABLED fail-safe and the file is retained.
        assert lim.prune_witness_files() == 0
        assert wfiles[0].exists()


def test_prune_never_mints_witness_inode(tmp_path):
    """Phase 3E req2: prune must never create a witness file/inode."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    # a TERMINAL grant exists, but its witness file is ABSENT
    with lim._transaction(write=True) as st:
        st["grants"]["e1:888"] = {
            "prio": "P0", "issued_wall": clock.wall(), "outcome": "OK",
            "probe": False, "pid": 1, "boot_id": grl.current_boot_id(),
            "sent": False}
        lim._set_dirty()
    assert list(tmp_path.glob("*.witness")) == []
    lim.prune_witness_files()
    # prune opened WITHOUT create → it must not have minted an inode
    assert list(tmp_path.glob("*.witness")) == []


# ---------------------------------------------------------------- F7
def test_acquire_deadline_none_bounded_by_max_wait(tmp_path):
    """F7: acquire(deadline=None) is bounded by max_wait_s, never forever."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=0.001, burst=12.0, max_wait_s=2.0)
    for _ in range(12):                              # drain the bucket
        assert lim.acquire("P1", deadline=clock.mono() + 5.0).granted
    out = lim.acquire("P1", deadline=None)           # refill ~0 → must wait
    assert out.result is GrantResult.SKIPPED
    assert out.reason == "deadline"
    assert 1500 <= out.waited_ms <= 2500, out.waited_ms   # ~ max_wait_s


def test_deadline_sleep_never_overshoots(tmp_path):
    """F7: the poll sleep never overshoots the deadline (bounded overshoot)."""
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=0.001, burst=12.0)
    for _ in range(12):
        assert lim.acquire("P1", deadline=clock.mono() + 5.0).granted
    t0 = clock.mono()
    out = lim.acquire("P1", deadline=t0 + 0.5)
    assert out.result is GrantResult.SKIPPED
    # sleeping lands ON the deadline; only txn latency remains (≈0 in fake
    # time), so overshoot is bounded well under one poll quantum of slop
    assert 450 <= out.waited_ms <= 550, out.waited_ms
