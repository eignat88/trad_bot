"""ISSUE253 — cross-process shared-limiter CONSUMER alignment (Stage D).

Multiprocessing suite for the Issue #253 properties that a single-process
test cannot prove: several INDEPENDENT consumers, each resolving its own
config file, must end up on ONE shared limiter state and ONE token budget,
and a consumer whose configuration drifts must fail closed instead of
running an off-contract budget against the same bucket.

Repository convention (spawn context, module-level workers, results via
files, start event for a simultaneous release) — same as
``tests/test_global_bybit_rate_limiter_mp_v1.py``.

No Bybit HTTP anywhere: every worker only touches a temp state file.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from app.config import load_settings
from app.exchange.global_rate_limiter import (
    GrantResult,
    GlobalBybitRateLimiter,
    LimiterConfig,
)

CTX = mp.get_context("spawn")

BURST = 3.0
G_RPS = 6.0
MAX_WAIT_S = 0.2
DURATION_S = 2.0
ATTEMPTS_PER_WORKER = 10
#: one worker per priority class so the per-class eligibility credit of each
#: class is exercised while the shared bucket is the binding constraint.
WORKER_PRIORITIES = ("P0", "P1", "P3")


# --------------------------------------------------------------- file helpers
def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("")


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def _wait_files(paths, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(os.path.exists(p) for p in paths):
            return
        time.sleep(0.01)
    raise TimeoutError(f"workers did not produce {paths}")


def _join(p: "mp.Process", timeout: float = 120.0) -> None:
    p.join(timeout)
    assert not p.is_alive(), f"worker pid={p.pid} did not finish"
    assert p.exitcode == 0, f"worker pid={p.pid} exitcode={p.exitcode}"


def _cfg(state_path: str, **over) -> LimiterConfig:
    base = dict(enabled=True, state_path=state_path, g_rps=G_RPS,
                burst=BURST, max_wait_s=MAX_WAIT_S,
                breaker_cooldowns_s=(1.0, 2.0, 3.0),
                recovery_success_target=3, lock_timeout_s=5.0)
    base.update(over)
    return LimiterConfig(**base)


# =========================================================== module workers
def worker_contract_consumer(config_path: str, priority: str,
                             ready_file: str, start_evt, result_file: str):
    """A consumer shaped like a real service: its OWN config file, one
    contract, then hammer the shared bucket for a fixed window."""
    settings = load_settings(path=config_path,
                             env_file=os.path.join(os.path.dirname(config_path),
                                                   ".env.absent"))
    cfg = LimiterConfig.from_settings(settings)
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    if not start_evt.wait(60):
        os._exit(3)

    granted = denied = 0
    attempts = 0
    t_end = time.monotonic() + DURATION_S
    while attempts < ATTEMPTS_PER_WORKER and time.monotonic() < t_end:
        attempts += 1
        out = lim.acquire(priority, deadline=None)
        if out.granted:
            granted += 1
            lim.record_result(out.grant_id, priority, "OK")
        else:
            denied += 1
    _write(result_file, json.dumps({
        "state_path": cfg.state_path,
        "fingerprint": cfg.fingerprint,
        "granted": granted,
        "denied": denied,
        "attempts": attempts,
    }))
    os._exit(0)


def worker_drifted_consumer(config_path: str, ready_file: str,
                            result_file: str):
    """A consumer whose config DRIFTED (no contract): must fail closed."""
    settings = load_settings(path=config_path,
                             env_file=os.path.join(os.path.dirname(config_path),
                                                   ".env.absent"))
    cfg = LimiterConfig.from_settings(settings)
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    out = lim.acquire("P1", deadline=None)
    _write(result_file, json.dumps({
        "state_path": cfg.state_path,
        "fingerprint": cfg.fingerprint,
        "result": out.result.value,
        "reason": out.reason,
        "granted": out.granted,
    }))
    os._exit(0)


def worker_seed(config_path: str, ready_file: str, result_file: str):
    """Create the shared state from a contract-bound configuration."""
    settings = load_settings(path=config_path,
                             env_file=os.path.join(os.path.dirname(config_path),
                                                   ".env.absent"))
    cfg = LimiterConfig.from_settings(settings)
    lim = GlobalBybitRateLimiter(cfg)
    _touch(ready_file)
    out = lim.acquire("P1", deadline=None)
    assert out.granted, out
    lim.record_result(out.grant_id, "P1", "OK")
    _write(result_file, json.dumps({"fingerprint": cfg.fingerprint}))
    os._exit(0)


# ================================================================== helpers
def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _contract(tmp_path: Path, **overrides) -> Path:
    payload = {
        "global_limiter_state_path": str(tmp_path / "shared_state.json"),
        "global_limiter_g_rps": G_RPS,
        "global_limiter_burst": BURST,
        "global_limiter_max_wait_s": MAX_WAIT_S,
        "global_limiter_breaker_cooldowns_s": [1.0, 2.0, 3.0],
        "global_limiter_recovery_success_target": 3,
        "global_limiter_lock_timeout_s": 5.0,
        "global_limiter_demand_ttl_s": 60.0,
        "global_limiter_pending_liveness_ttl_s": 5.0,
        "global_limiter_probe_lease_s": 5.0,
        "global_limiter_registry_capacity": 8192,
        "global_limiter_max_lifetime_s": 60.0,
        "global_limiter_record_retry_attempts": 2,
        "global_limiter_record_retry_backoff_s": 0.0,
    }
    payload.update(overrides)
    return _write_json(tmp_path / "limiter_contract.json", payload)


def _consumer_config(tmp_path: Path, name: str, contract: Path,
                     **overrides) -> Path:
    payload = {
        "global_limiter_contract_path": str(contract),
        "global_bybit_rate_limiter_enabled": True,
    }
    payload.update(overrides)
    return _write_json(tmp_path / name, payload)


# ==================================================================== tests
def test_mp_consumers_share_one_state_and_one_budget(tmp_path):
    """3 consumers, 3 config files, ONE state file and ONE bucket.

    Proves the Issue #253 integration property across real OS processes:
    every worker reports the same resolved state path and the same
    parameter fingerprint, all grants are accounted for by exactly one
    bucket, and the bucket NEVER over-issues
    (``tokens == burst + refill_total - total_grants``).
    """
    contract = _contract(tmp_path)
    configs = [
        str(_consumer_config(tmp_path, f"consumer{i}.json", contract))
        for i in range(3)
    ]
    ready = [str(tmp_path / f"ready{i}") for i in range(3)]
    results = [str(tmp_path / f"result{i}.json") for i in range(3)]
    start_evt = CTX.Event()

    procs = [
        CTX.Process(target=worker_contract_consumer,
                    args=(configs[i], WORKER_PRIORITIES[i],
                          ready[i], start_evt, results[i]))
        for i in range(3)
    ]
    for p in procs:
        p.start()
    _wait_files(ready)
    start_evt.set()
    for p in procs:
        _join(p)

    reports = [json.loads(_read(r)) for r in results]

    # --- shared state identity across independent processes
    assert len({r["state_path"] for r in reports}) == 1
    assert reports[0]["state_path"] == str(tmp_path / "shared_state.json")
    assert len({r["fingerprint"] for r in reports}) == 1

    # --- exactly one accounting point
    granted_total = sum(r["granted"] for r in reports)
    assert all(r["attempts"] > 0 for r in reports)     # everyone participated
    assert sum(r["attempts"] for r in reports) > granted_total, \
        "the shared budget must actually deny somebody"

    state = json.loads(Path(reports[0]["state_path"]).read_text(
        encoding="utf-8"))
    assert state["total_grants"] == granted_total
    assert len(state["grants"]) == state["total_grants"]
    # burst can never be exceeded: the exact bucket invariant
    expected_tokens = BURST + float(state["refill_total"]) \
        - float(state["total_grants"])
    assert float(state["tokens"]) == pytest.approx(expected_tokens, abs=1e-6)
    assert state["tokens"] >= 0.0
    # the configured burst was fully usable by the three consumers
    assert state["total_grants"] >= BURST
    # ... and still bounded by burst + refill
    assert state["total_grants"] <= BURST + float(state["refill_total"]) + 1e-9
    # every recorded grant is terminal (no dangling ISSUED records)
    assert all(rec["outcome"] is not None for rec in state["grants"].values())


def test_mp_drifted_config_fails_closed_and_never_rewrites_state(tmp_path):
    """A consumer configured with different parameters for the SAME state
    path is refused (config_mismatch) and cannot mutate the shared state."""
    contract = _contract(tmp_path)
    good = _consumer_config(tmp_path, "good.json", contract)

    seed_ready, seed_result = str(tmp_path / "seed_ready"), str(tmp_path / "seed.json")
    seed = CTX.Process(target=worker_seed, args=(str(good), seed_ready, seed_result))
    seed.start()
    _wait_files([seed_ready])
    _join(seed)
    seeded_fingerprint = json.loads(_read(seed_result))["fingerprint"]

    state_path = tmp_path / "shared_state.json"
    before = state_path.read_text(encoding="utf-8")

    # same state path, but G deliberately different and NO contract binding
    drifted = _write_json(tmp_path / "drifted.json", {
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_state_path": str(state_path),
        "global_limiter_g_rps": 4.0,          # contract says 6.0
    })
    ready, result = str(tmp_path / "drift_ready"), str(tmp_path / "drift.json")
    proc = CTX.Process(target=worker_drifted_consumer,
                       args=(str(drifted), ready, result))
    proc.start()
    _wait_files([ready])
    _join(proc)

    report = json.loads(_read(result))
    assert report["granted"] is False
    assert report["result"] == GrantResult.STATE_UNAVAILABLE.value
    assert report["reason"] == "config_mismatch"
    assert report["fingerprint"] != seeded_fingerprint
    # fail-closed: the shared state is byte-identical afterwards
    assert state_path.read_text(encoding="utf-8") == before


def test_mp_contract_bound_workers_never_create_a_second_state(tmp_path):
    """Even when each consumer writes its OWN config file, no second state
    file or lock file appears anywhere in the working directory."""
    contract = _contract(tmp_path)
    configs = [str(_consumer_config(tmp_path, f"c{i}.json", contract))
               for i in range(3)]
    ready = [str(tmp_path / f"r{i}") for i in range(3)]
    results = [str(tmp_path / f"o{i}.json") for i in range(3)]
    start_evt = CTX.Event()
    procs = [CTX.Process(target=worker_contract_consumer,
                         args=(configs[i], WORKER_PRIORITIES[i],
                               ready[i], start_evt, results[i]))
             for i in range(3)]
    for p in procs:
        p.start()
    _wait_files(ready)
    start_evt.set()
    for p in procs:
        _join(p)

    state_artifacts = sorted(
        p.name for p in tmp_path.iterdir() if "state" in p.name)
    assert state_artifacts == ["shared_state.json",
                               "shared_state.json.lock"], state_artifacts
    for r in results:
        assert json.loads(_read(r))["state_path"] == str(
            tmp_path / "shared_state.json")
