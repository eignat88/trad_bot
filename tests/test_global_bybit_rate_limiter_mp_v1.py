"""GLOBAL_BYBIT_RATE_LIMITER_V1 — cross-process evidence (Issue #231, Phase 3).

Multiprocessing / fault-injection suite for the shared limiter core,
following the repository convention (spawn context, module-level workers,
results via files, crash workers via ``os._exit`` — see
``tests/test_bybit_order_submit_guard_multiproc_v1.py``).

Platform note: the file evidence collected on this host covers real OS
locking under the ``spawn`` start method (Windows byte-range locks here;
POSIX flock on the Linux agent), cross-process breaker/probe-slot
exclusion, crash cleanup, SIGKILL, and the first-use race. The
SIGSTOP/SIGCONT test is POSIX-only and reports SKIP on Windows — the
Linux run of this module is required before any merge-readiness claim
(Phase 3 deliverable: LINUX evidence status).

No Bybit HTTP anywhere: every worker only touches a temp state file.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time

import pytest

from app.exchange import global_rate_limiter as grl
from app.exchange.global_rate_limiter import (
    GrantResult,
    GlobalBybitRateLimiter,
    LimiterConfig,
)

CTX = mp.get_context("spawn")


# ------------------------------------------------------------- file helpers
def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("")


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def _wait_files(paths: list[str], timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(os.path.exists(p) for p in paths):
            return
        time.sleep(0.01)
    raise TimeoutError(f"workers did not produce {paths}")


def _proc(target, args) -> mp.Process:
    p = CTX.Process(target=target, args=args)
    p.start()
    return p


def _join(p: mp.Process, timeout: float = 90.0) -> None:
    p.join(timeout)
    assert not p.is_alive(), f"worker pid={p.pid} did not finish"
    assert p.exitcode == 0, f"worker pid={p.pid} exitcode={p.exitcode}"


# --------------------------------------------------------------- test config
def _cfg(tmp_path, name: str = "mp_state.json", **over) -> LimiterConfig:
    base = dict(enabled=True, state_path=str(tmp_path / name))
    base.update(over)
    return LimiterConfig(**base)


def _raw(state_path: str) -> dict:
    with open(state_path, encoding="utf-8") as fh:
        return json.load(fh)


# =====================================================================
# Module-level workers (spawn-safe: no closures, no pickled lambdas)
# =====================================================================
def _worker_hammer(cfg, priority, duration_s, start_evt, ready_file,
                   result_file):
    """Acquire/record in a tight loop for ``duration_s`` after start."""
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    if not start_evt.wait(60):
        os._exit(3)
    granted = skipped = 0
    t_end = time.monotonic() + duration_s
    while True:
        now = time.monotonic()
        if now >= t_end:
            break
        out = lim.acquire(priority, deadline=min(t_end, now + 0.5))
        if out.granted:
            granted += 1
            lim.record_result(out.grant_id, priority, "OK", epoch=out.epoch)
        else:
            skipped += 1
    _write(result_file, f"{granted},{skipped}")
    os._exit(0)


def _worker_counted(cfg, priority, n_grants, start_evt, ready_file,
                    result_file):
    """Acquire exactly ``n_grants`` (recording OK), bounded by a 60 s cap."""
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    if not start_evt.wait(60):
        os._exit(3)
    granted = 0
    hard_end = time.monotonic() + 60.0
    while granted < n_grants and time.monotonic() < hard_end:
        out = lim.acquire(priority, deadline=min(time.monotonic() + 2.0,
                                                 hard_end))
        if out.granted:
            granted += 1
            lim.record_result(out.grant_id, priority, "OK", epoch=out.epoch)
    _write(result_file, str(granted))
    os._exit(0)


def _worker_single_acquire(cfg, priority, start_evt, ready_file,
                           result_file):
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    if not start_evt.wait(60):
        os._exit(3)
    out = lim.acquire(priority, deadline=time.monotonic() + 10.0)
    _write(result_file, f"{out.result.value}|{out.reason}|{out.grant_id}")
    os._exit(0)


def _worker_probe_then_exit(cfg, do_marker, gid_file):
    """Recovery probe holder that CRASHES without recording a result.

    With ``do_marker`` the send-marker is durably written before the crash
    (attempt unknown); without it the attempt never left the process
    (attempt not-sent).
    """
    lim = GlobalBybitRateLimiter(cfg)
    out = lim.acquire("P0", deadline=time.monotonic() + 20.0)
    if not out.granted or not out.probe:
        _write(gid_file, f"FAIL:{out.result.value}")
        os._exit(2)
    if do_marker and not lim.prepare_send(out.grant_id):
        _write(gid_file, "FAIL:noprep")
        os._exit(3)
    _write(gid_file, out.grant_id)
    os._exit(0)          # crash: no record_result, witness lock freed by OS


def _worker_probe_sleep_forever(cfg, ready_file):
    """Probe holder that writes its gid+pid and sleeps until SIGKILL."""
    lim = GlobalBybitRateLimiter(cfg)
    out = lim.acquire("P0", deadline=time.monotonic() + 20.0)
    if not out.granted or not out.probe or not lim.prepare_send(out.grant_id):
        _write(ready_file, "FAIL")
        os._exit(2)
    _write(ready_file, f"{out.grant_id}|{os.getpid()}")
    while True:                       # killed by the parent (SIGKILL)
        time.sleep(3600)


def _worker_probe_then_wait_release(cfg, ready_file, release_file,
                                    done_file):
    """Probe holder that reports gid+pid, then waits (SIGSTOP target)."""
    lim = GlobalBybitRateLimiter(cfg)
    out = lim.acquire("P0", deadline=time.monotonic() + 20.0)
    if not out.granted or not out.probe or not lim.prepare_send(out.grant_id):
        _write(ready_file, "FAIL")
        os._exit(2)
    _write(ready_file, f"{out.grant_id}|{os.getpid()}")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and not os.path.exists(release_file):
        time.sleep(0.02)
    if lim.record_result(out.grant_id, "P0", "OK",
                         epoch=out.epoch) != "APPLIED":
        _write(done_file, "bad")
        os._exit(3)
    _write(done_file, "done")
    os._exit(0)


def _worker_plant_pending_then_exit(cfg, ready_file):
    """Simulate a crashed waiter: durable pending entry, then a crash."""
    lim = GlobalBybitRateLimiter(cfg)
    with lim._transaction(write=True) as st:
        st["pending"]["crashed-" + os.urandom(3).hex()] = {
            "prio": "P1", "pid": os.getpid(),
            "boot_id": grl.current_boot_id(),
            "registered_wall": time.time(),
            "expire_wall": time.time() + 0.2,
            "lrid": "crash-sim"}
        lim._set_dirty()
    _write(ready_file, "ok")
    os._exit(0)


def _worker_acquire_then_exit(cfg, ready_file):
    """F2: acquire a NORMAL (non-probe) grant then die WITHOUT recording."""
    lim = GlobalBybitRateLimiter(cfg)
    out = lim.acquire("P0", deadline=time.monotonic() + 10.0)
    if not out.granted or out.probe:
        _write(ready_file, "FAIL")
        os._exit(2)
    _write(ready_file, out.grant_id)
    os._exit(0)          # crash: no record_result → non-terminal orphan


def _worker_hold_witness(path, hold_s, ready_file, result_file):
    """Phase3E: lock a witness file exclusively and HOLD it cross-process.

    Simulates a live probe holder whose flock inode must never be unlinked
    or replaced while it is held.
    """
    _touch(ready_file)
    try:
        fh = open(path, "a+")          # opens the EXISTING inode
        grl._try_lock(fh)              # exclusive; raises if already held
    except OSError:
        _write(result_file, "LOCKFAIL")
        os._exit(2)
    _write(result_file, str(os.stat(path).st_ino))
    time.sleep(hold_s)                 # held while the parent prunes
    try:
        grl._unlock(fh)
    except OSError:
        pass
    fh.close()
    os._exit(0)


def _worker_prune_flood(cfg, n, ready_file, result_file):
    """Phase3E: run n prune passes concurrently with a live holder."""
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    total = 0
    for _ in range(n):
        total += lim.prune_witness_files()
    _write(result_file, str(total))
    os._exit(0)


# =====================================================================
# 1. Cross-process budget: no process can exceed the global curve
# =====================================================================
def test_mp_sustained_budget_no_burst_overrun(tmp_path):
    cfg = _cfg(tmp_path, "mp_budget.json")
    evt = CTX.Event()
    ready = [str(tmp_path / f"ready{i}") for i in range(3)]
    results = [str(tmp_path / f"res{i}") for i in range(3)]
    workers = [_proc(_worker_hammer,
                     (cfg, "P1", 2.5, evt, ready[i], results[i]))
               for i in range(3)]
    _wait_files(ready)
    evt.set()
    for w in workers:
        _join(w)
    granted = [int(_read(r).split(",")[0]) for r in results]
    total = sum(granted)
    # upper: B + G·T + start-skew slack — no process may exceed the
    # global curve. lower: generous (host-level fsync stalls of ~1 s are
    # observed occasionally on Windows dev hosts; a broken limiter yields
    # 0–3 grants, not 8+).
    assert 6 <= total <= 12 + 8 * 3.0 + 2, granted
    assert all(g >= 1 for g in granted), granted   # no worker starved


# =====================================================================
# 2. Cross-process priority fairness, no starvation of any class
# =====================================================================
def test_mp_priority_fairness_no_starvation(tmp_path):
    cfg = _cfg(tmp_path, "mp_prio.json")
    evt = CTX.Event()
    prios = ("P0", "P1", "P2", "P3")
    ready = [str(tmp_path / f"ready{i}") for i in range(4)]
    results = [str(tmp_path / f"res{i}") for i in range(4)]
    workers = [_proc(_worker_hammer,
                     (cfg, p, 3.0, evt, ready[i], results[i]))
               for i, p in enumerate(prios)]
    _wait_files(ready)
    evt.set()
    for w in workers:
        _join(w)
    by_prio = {p: int(_read(results[i]).split(",")[0])
               for i, p in enumerate(prios)}
    total = sum(by_prio.values())
    # global budget holds: B + G·T + slack; lower bound generous against
    # host fsync stalls (a broken limiter yields 0–3 grants, not 8+)
    assert total <= 12 + 8 * 3.5 + 2, by_prio
    assert total >= 8, by_prio
    # no starvation: every class is served in a bounded window
    # (long-horizon weighted shares follow from the per-class eligibility
    # curves; a 3 s window is dominated by the poll race)
    assert all(v >= 1 for v in by_prio.values()), by_prio


# =====================================================================
# 3. Accounting invariants across processes (§3.3)
# =====================================================================
def test_mp_accounting_consistency_across_processes(tmp_path):
    cfg = _cfg(tmp_path, "mp_acct.json")
    evt = CTX.Event()
    prios = ("P0", "P1", "P2", "P3")
    ready = [str(tmp_path / f"ready{i}") for i in range(4)]
    results = [str(tmp_path / f"res{i}") for i in range(4)]
    workers = [_proc(_worker_counted,
                     (cfg, p, 8, evt, ready[i], results[i]))
               for i, p in enumerate(prios)]
    _wait_files(ready)
    evt.set()
    for w in workers:
        _join(w)
    counts = [_read(r) for r in results]
    assert counts == ["8", "8", "8", "8"], counts

    st = grl._validate(_raw(str(tmp_path / "mp_acct.json")))  # checksum ok
    assert int(st["total_grants"]) == 32
    assert int(st["epoch_grants"]) == 32                       # no trips
    assert sum(int(v) for v in st["usage_by_priority"].values()) == 32
    assert len(st["grants"]) == 32
    # exact identity: tokens == B + refill_total − total_grants
    assert abs(float(st["tokens"]) -
               (12.0 + float(st["refill_total"]) - 32.0)) < 1e-6


# =====================================================================
# 4. Cross-process breaker: a trip by one process defers all classes
# =====================================================================
def test_mp_cross_process_breaker_defers_all_classes(tmp_path):
    cfg = _cfg(tmp_path, "mp_brk.json",
               breaker_cooldowns_s=(8.0, 20.0, 30.0))
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"      # trip → OPEN
    assert lim.snapshot()["state"] == "OPEN"

    evt = CTX.Event()
    ready, res = str(tmp_path / "ready"), str(tmp_path / "res")
    p = _proc(_worker_single_acquire, (cfg, "P3", evt, ready, res))
    _wait_files([ready])
    evt.set()
    _join(p)
    # another process, lowest priority → immediate fail-closed defer
    assert _read(res).startswith("BREAKER_OPEN|breaker_open|")
    # the tripping process itself is deferred too (no local bypass)
    out = lim.acquire("P0", deadline=time.monotonic() + 0.5)
    assert out.result is GrantResult.BREAKER_OPEN
    assert out.reason == "breaker_cooldown_exceeds_deadline"


# =====================================================================
# 5. Recovery probe slot excludes a second process (no double admit)
# =====================================================================
def test_mp_recovery_slot_excludes_second_process(tmp_path):
    cfg = _cfg(tmp_path, "mp_slot.json",
               breaker_cooldowns_s=(0.3, 0.5, 0.7))
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"
    time.sleep(0.4)                                  # cooldown elapses
    out = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert out.granted and out.probe                 # RECOVERY probe admitted
    assert lim.snapshot()["state"] == "RECOVERY"

    evt = CTX.Event()
    ready, res = str(tmp_path / "ready"), str(tmp_path / "res")
    p = _proc(_worker_single_acquire, (cfg, "P1", evt, ready, res))
    _wait_files([ready])
    evt.set()
    _join(p)
    # ≤1 recovery probe: the second process is refused by deadline
    assert _read(res).startswith("SKIPPED"), _read(res)
    assert lim.snapshot()["probe"]["holder"] == out.grant_id

    # holder result frees the slot; the next request gets the probe
    assert lim.record_result(out.grant_id, "P0", "OK",
                             epoch=out.epoch) == "APPLIED"
    assert lim.snapshot()["probe"] is None
    q = lim.acquire("P1", deadline=time.monotonic() + 5.0)
    assert q.granted and q.probe
    assert q.grant_id != out.grant_id
    assert lim.record_result(q.grant_id, "P1", "OK", epoch=q.epoch) == \
        "APPLIED"


# =====================================================================
# 6. Crash BEFORE the send-marker → attempt NOT_SENT, slot reusable
# =====================================================================
def test_mp_crash_before_marker_releases_probe_slot(tmp_path):
    cfg = _cfg(tmp_path, "mp_crash1.json",
               breaker_cooldowns_s=(0.3, 0.5, 0.7))
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"
    gid_file = str(tmp_path / "gid")
    p = _proc(_worker_probe_then_exit, (cfg, False, gid_file))
    _join(p)
    gid = _read(gid_file)
    assert not gid.startswith("FAIL"), gid

    snap = lim.refresh()
    assert snap["state"] == "RECOVERY"          # no reopen: nothing was sent
    assert snap["probe"] is None                # holder death freed the slot
    assert snap["epoch"] == g.epoch + 1         # no fencing bump
    assert lim.grant_outcome(gid) == "NOT_SENT"
    # the released slot admits a fresh probe
    q = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert q.granted and q.probe
    assert q.grant_id != gid


# =====================================================================
# 7. Crash AFTER the send-marker → attempt UNKNOWN, breaker reopens
# =====================================================================
def test_mp_crash_after_marker_fails_closed_unknown(tmp_path):
    cfg = _cfg(tmp_path, "mp_crash2.json",
               breaker_cooldowns_s=(0.3, 0.5, 0.7))
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"
    gid_file = str(tmp_path / "gid")
    p = _proc(_worker_probe_then_exit, (cfg, True, gid_file))
    _join(p)
    gid = _read(gid_file)
    assert not gid.startswith("FAIL"), gid

    # The cooldown is armed DURING refresh (before its fsync). Compare it to
    # a wall captured BEFORE the refresh so a slow host fsync (this suite
    # documents ~1 s stalls on Windows dev hosts) cannot make a freshly-armed
    # cooldown look stale.
    t_before = time.time()
    snap = lim.refresh()
    assert snap["state"] == "OPEN"              # reopened at the same level
    assert snap["level"] == 1                   # escalation stays 1
    assert snap["epoch"] == g.epoch + 2         # trip + unknown resolution
    assert snap["probe"] is None
    assert snap["success_streak"] == 0
    assert lim.grant_outcome(gid) == "UNKNOWN"  # terminal, dedup by gid
    # re-armed: cooldown_for(level 1) == 0.3 s ahead of the pre-refresh wall
    assert snap["cooldown_until_wall"] >= t_before + 0.2


# =====================================================================
# 8. SIGKILL of a live probe holder (marker written) → UNKNOWN, closed
# =====================================================================
def test_mp_sigkill_of_probe_holder_fails_closed(tmp_path):
    cfg = _cfg(tmp_path, "mp_kill.json",
               breaker_cooldowns_s=(0.3, 0.5, 0.7))
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"
    ready = str(tmp_path / "ready")
    p = _proc(_worker_probe_sleep_forever, (cfg, ready))
    _wait_files([ready])
    payload = _read(ready)
    assert payload != "FAIL"
    gid, pid = payload.split("|")

    p.kill()                                      # SIGKILL / TerminateProcess
    p.join(30)
    assert not p.is_alive()

    snap = lim.refresh()
    assert snap["state"] == "OPEN"              # attempt unknown → reopen
    assert snap["level"] == 1
    assert snap["epoch"] == g.epoch + 2
    assert snap["probe"] is None
    assert lim.grant_outcome(gid) == "UNKNOWN"
    assert int(pid) > 0                          # sanity for the record


# =====================================================================
# 9. SIGSTOP: a LIVE holder keeps the slot past its lease (§3.1)
# =====================================================================
@pytest.mark.skipif(os.name != "posix",
                    reason="SIGSTOP/SIGCONT exist on POSIX only")
def test_mp_sigstop_live_holder_blocks_probe_past_lease(tmp_path):
    import signal

    cfg = _cfg(tmp_path, "mp_stop.json",
               breaker_cooldowns_s=(0.2, 0.4, 0.6),
               probe_lease_s=0.05,
               recovery_success_target=9)
    lim = GlobalBybitRateLimiter(cfg)
    g = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert g.granted
    assert lim.record_result(g.grant_id, "P0", "RATE_LIMIT",
                             epoch=g.epoch) == "APPLIED"
    time.sleep(0.25)                             # → RECOVERY is reachable

    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")
    done = str(tmp_path / "done")
    p = _proc(_worker_probe_then_wait_release, (cfg, ready, release, done))
    _wait_files([ready])
    gid, pid_s = _read(ready).split("|")
    pid = int(pid_s)

    os.kill(pid, signal.SIGSTOP)                 # holder stays alive, frozen
    time.sleep(0.15)                             # lease (0.05) long expired

    snap = lim.refresh()
    assert snap["state"] == "RECOVERY"           # NOT released on time
    assert snap["probe"] is not None
    assert snap["probe"]["holder"] == gid
    assert snap["anomalies"]["probe_stalled"] >= 1   # anomaly, no unlock

    # a second probe is refused although the lease expired long ago
    out = lim.acquire("P1", deadline=time.monotonic() + 0.3)
    assert out.result is GrantResult.SKIPPED

    _touch(release)                              # let the holder deliver
    os.kill(pid, signal.SIGCONT)
    _wait_files([done], timeout=60.0)
    _join(p)

    assert lim.refresh()["probe"] is None        # holder result freed it
    q = lim.acquire("P1", deadline=time.monotonic() + 5.0)
    assert q.granted and q.probe


# =====================================================================
# 10. First-use race: two processes initialize the same state file
# =====================================================================
def test_mp_first_use_race_creates_valid_state(tmp_path):
    cfg = _cfg(tmp_path, "mp_race.json")        # fresh state on purpose
    evt = CTX.Event()
    ready = [str(tmp_path / f"ready{i}") for i in range(2)]
    results = [str(tmp_path / f"res{i}") for i in range(2)]
    workers = [_proc(_worker_single_acquire,
                     (cfg, "P1", evt, ready[i], results[i]))
               for i in range(2)]
    _wait_files(ready)
    evt.set()
    for w in workers:
        _join(w)
    payloads = [_read(r) for r in results]
    assert all(p.startswith("GRANTED|") for p in payloads), payloads
    st = grl._validate(_raw(str(tmp_path / "mp_race.json")))
    assert int(st["total_grants"]) == 2
    assert len(st["grants"]) == 2


# =====================================================================
# 11. Crashed waiter: stale pending demand is pruned, pool stays healthy
# =====================================================================
def test_mp_crashed_waiter_pending_pruned(tmp_path):
    cfg = _cfg(tmp_path, "mp_pending.json")
    lim = GlobalBybitRateLimiter(cfg)
    assert lim.acquire("P0", deadline=time.monotonic() + 5.0).granted

    ready = str(tmp_path / "ready")
    p = _proc(_worker_plant_pending_then_exit, (cfg, ready))
    _join(p)
    _wait_files([ready])
    time.sleep(0.25)                             # planted TTL elapses

    snap = lim.refresh()
    assert snap["pending_count"] == 0           # crashed waiter cleaned up
    assert snap["available"] is True
    out = lim.acquire("P0", deadline=time.monotonic() + 5.0)
    assert out.granted                           # pool still operational


# =====================================================================
# 12. F2: a SIGKILL'ed owner of a NORMAL grant is reconciled (bounded)
# =====================================================================
@pytest.mark.skipif(os.name != "posix",
                    reason="pid-death probe is POSIX only")
def test_mp_sigkill_normal_grant_owner_reconciled(tmp_path):
    cfg = _cfg(tmp_path, "mp_deadowner.json", registry_capacity=8,
               max_lifetime_s=0.05)
    lim = GlobalBybitRateLimiter(cfg)
    ready = str(tmp_path / "ready")
    p = _proc(_worker_acquire_then_exit, (cfg, ready))
    _wait_files([ready])
    _join(p)                            # worker died WITHOUT record_result
    gid = _read(ready)
    assert not gid.startswith("FAIL"), gid

    # owner provably dead (POSIX pid gone) → neutral NOT_SENT, no leak.
    # NOTE: max_lifetime_s = 0.05 makes the closed record legally
    # evictable by retention in the VERY SAME refresh pass — F2 closes
    # it first, then the retention loop removes the now-terminal aged
    # record, and process spawn/join routinely exceeds 50 ms on slower
    # hosts. Assert the durable invariant that holds in BOTH timings
    # instead of the record's mere presence: the grant is either still
    # there as terminal NOT_SENT, or already gone — absence is only
    # reachable THROUGH a terminal transition (eviction never touches
    # non-terminal records).
    snap = lim.refresh()
    assert snap["success_streak"] == 0
    assert snap["state"] == "CLOSED"
    st = grl._validate(_raw(str(tmp_path / "mp_deadowner.json")))
    rec = st["grants"].get(gid)
    if rec is not None:
        # still inside the retention window: terminal state is durable
        assert rec["outcome"] == "NOT_SENT"   # no marker → provably unsent
    else:
        # already legally evicted right after reconciliation: nothing
        # lingers (the registry shows no orphan)
        assert snap["grants_count"] == 0
    assert int(st["total_grants"]) == 1       # no token was returned

    # retention evicts the closed record → registry stays bounded
    time.sleep(0.06)
    lim.refresh()
    assert lim.grant_outcome(gid) == "ABSENT"
    assert lim.snapshot()["grants_count"] == 0


# =====================================================================
# 13. Phase3E: witness cleanup TOCTOU — concurrent prune vs live holder
# =====================================================================
@pytest.mark.skipif(os.name != "posix",
                    reason="unlink-while-lock witness invariant is POSIX-only")
def test_mp_prune_cannot_unlink_or_recreate_active_witness(tmp_path):
    """Concurrent prune must not unlink a HELD witness nor mint a 2nd inode."""
    cfg = _cfg(tmp_path, "mp_witness.json")
    lim = GlobalBybitRateLimiter(cfg)
    # plant a TERMINAL grant so prune will consider its witness
    with lim._transaction(write=True) as st:
        st["grants"]["e1:777"] = {
            "prio": "P0", "issued_wall": time.time(), "outcome": "OK",
            "probe": False, "pid": 1, "boot_id": grl.current_boot_id(),
            "sent": False}
        lim._set_dirty()
    wname = grl._witness_name(str(tmp_path / "mp_witness.json"), "e1:777")
    wpath = str(tmp_path / wname)
    with open(wpath, "w", encoding="utf-8"):
        pass
    ino0 = os.stat(wpath).st_ino

    # a cross-process holder locks the witness and holds it
    hready = str(tmp_path / "hready")
    hres = str(tmp_path / "hres")
    holder = _proc(_worker_hold_witness, (wpath, 5.0, hready, hres))
    _wait_files([hres])                       # holder now holds the lock
    assert _read(hres) == str(ino0)           # same inode (no recreation)

    # several prune workers flood concurrently while the witness is HELD
    evt_ready = [str(tmp_path / f"pr{i}") for i in range(3)]
    pr_results = [str(tmp_path / f"pres{i}") for i in range(3)]
    pruners = [_proc(_worker_prune_flood, (cfg, 25, evt_ready[i], pr_results[i]))
               for i in range(3)]
    _wait_files(evt_ready)
    for w in pruners:
        _join(w)
    # every pruner saw a HELD witness → removed nothing
    assert all(int(_read(r)) == 0 for r in pr_results), \
        [_read(r) for r in pr_results]

    # the active witness survived the concurrent prune flood with its inode
    assert os.path.exists(wpath), "active witness was unlinked (TOCTOU)"
    assert os.stat(wpath).st_ino == ino0, "second inode minted for one grant"

    _join(holder)                             # holder released and exited
    # now inert (terminal + no holder) → safe unlink-while-lock removal
    assert lim.prune_witness_files() >= 1
    assert not os.path.exists(wpath)
