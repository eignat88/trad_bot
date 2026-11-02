"""ISSUE231 R2 — multi-user persistence safety for the shared limiter.

Regression suite for D1-D5 of the multi-user permissions review
(ISSUE231_R2_MULTI_USER_PERMISSIONS_REVIEW), covering the required
checks of ISSUE231_R2_MULTI_USER_PERSISTENCE_FIX_V1:

1. STATE JSON (D1) — group-shared ``0660`` rights after creation AND
   after every atomic ``os.replace``; the group is inherited from the
   setgid directory; an existing foreign file is NEVER re-moded (no
   implicit chmod migration); files are never published world-open.
2. LOCK / WITNESS (D2) — create-then-publish strategy: ``O_CREAT|O_EXCL``
   at private ``0600`` (umask can only strip bits), then ``fchmod(0660)``
   on our OWN descriptor before the file is usable by others — umask
   ``022`` and ``002`` both yield exactly ``0660``; an existing file is
   opened without create and without re-moding, keeping the flock inode
   stable across UIDs.
3. RECOVERY PROBE (D3) — ``EACCES``/``EPERM``/``ELOOP`` on a witness are
   UNDEFINED observations and must read as "still held" (fail-closed, no
   second recovery probe); only ``FileNotFoundError`` keeps the existing
   "removed ⇒ not held" contract.
4. PATH SECURITY (D4) — every state/lock/witness open uses ``O_NOFOLLOW``
   (symlinks fail closed), and a world-writable state directory is
   rejected at config-load time.
5. MULTI-USER CONFIGURATION (D5) — documented in
   docs/issue231_r2_limiter_config_contract_v1.md §3.1; this suite never
   performs user/group/systemd changes (the two-UID integration only
   runs as root inside an isolated temporary tree and cleans up after
   itself).

No token-bucket, breaker, fairness, retry, accounting, GET/POST,
scanner, paper/live or OOS behaviour is changed by the fix under test;
fault injection happens at the ``os.open`` boundary only.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import app.exchange.bybit_client as bybit_client_module
import app.exchange.global_rate_limiter as gr_module
from app.config.settings import Settings, load_settings
from app.exchange import retry_budget
from app.exchange.bybit_client import (
    BybitClient,
    BybitRateLimitError,
    _RateLimiter,
)
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    GrantResult,
    LimiterConfig,
    LimiterUnavailableError,
)

FLAG = "global_bybit_rate_limiter_enabled"
ENV_FLAG = "GLOBAL_BYBIT_RATE_LIMITER_ENABLED"
REPO_ROOT = Path(__file__).resolve().parents[1]
_POSIX_ONLY = pytest.mark.skipif(os.name != "posix",
                                 reason="POSIX file modes/semantics required")


# ------------------------------------------------------------------ fakes
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
    """Records every transport call (an empty script means: none allowed)."""

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


@pytest.fixture(autouse=True)
def isolated_limiter_env(monkeypatch):
    """The activation ENV must never leak in from the shell or .env."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ALLOW_ENV", raising=False)


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
    """Any real Bybit request (urlopen) fails the test immediately."""
    def _poisoned(*a, **k):
        raise AssertionError("real network request attempted in test")
    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


@pytest.fixture
def telemetry_env(monkeypatch, caplog):
    from app.exchange.api_telemetry import ApiTelemetry
    monkeypatch.setattr(bybit_client_module, "_telemetry", None)
    ApiTelemetry.get_instance().begin_cycle([])
    yield


# ---------------------------------------------------------------- helpers
def _load(tmp_path, payload):
    """load_settings() against an isolated JSON config and no .env file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(config_path, env_file=tmp_path / ".env.absent")


def _fast_limiter(tmp_path, **overrides):
    """A real, fast, workspace-isolated shared limiter (never prod path)."""
    cfg = dict(
        enabled=True,
        state_path=str(Path(tmp_path) / "shared_state.json"),
        g_rps=1000.0,
        burst=100000.0,
        registry_capacity=500000,     # >= ceil(2 * g_rps * max_lifetime)
        breaker_cooldowns_s=(0.05, 0.1, 0.2),
        lock_timeout_s=5.0,
        max_wait_s=5.0,
    )
    cfg.update(overrides)
    return GlobalBybitRateLimiter(LimiterConfig(**cfg))


def _grant(lim):
    out = lim.acquire("P0", deadline=lim.clock.mono() + 5.0)
    assert out.granted, f"expected grant, got {out.result}: {out.reason}"
    return out


def _enabled_client(session, tmp_path):
    """Client with the limiter flag ON and an isolated limiter injected."""
    settings = Settings(
        global_bybit_rate_limiter_enabled=True,
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=2,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
        bybit_retry_deadline_s=10.0,
    )
    client = BybitClient(settings, session)
    assert client._limiter_enabled() is True
    client._shared_limiter_obj = _fast_limiter(tmp_path)
    return client


def _inject_open_error(monkeypatch, predicate, exc_factory):
    """Fault injection at the os.open boundary (unit-level, restored)."""
    real_open = os.open

    def _open(path, flags, *args, **kwargs):
        if predicate(str(path)):
            raise exc_factory()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(gr_module.os, "open", _open)


# ============================ 1. D1 — state/lock file modes (POSIX)
@_POSIX_ONLY
def test_state_and_lock_group_shared_after_every_replace(tmp_path):
    """Creation AND every atomic os.replace publish exactly 0660 + the
    directory group (the multi-user contract for tradbot/tradbot-analytics)."""
    lim = _fast_limiter(tmp_path)
    out = _grant(lim)
    assert lim.record_result(out.grant_id, "P0", "OK",
                             epoch=out.epoch) == "APPLIED"
    state = Path(lim.config.state_path)
    lock = Path(lim.lock_path)
    dir_gid = Path(tmp_path).stat().st_gid
    for f in (state, lock):
        stt = f.stat()
        assert stat.S_IMODE(stt.st_mode) == 0o660, f"{f.name} mode"
        assert stt.st_gid == dir_gid, f"{f.name} group"
    # second durable update — a FRESH inode via os.replace must PRESERVE
    # the rights (this is what D1 said the old 0600 tmp file broke)
    out2 = _grant(lim)
    assert lim.record_result(out2.grant_id, "P0", "OK",
                             epoch=out2.epoch) == "APPLIED"
    for f in (state, lock):
        stt = f.stat()
        assert stat.S_IMODE(stt.st_mode) == 0o660, f"{f.name} mode after replace"
        assert stt.st_gid == dir_gid, f"{f.name} group after replace"


@_POSIX_ONLY
@pytest.mark.parametrize("mask", [0o022, 0o002], ids=["umask022", "umask002"])
def test_shared_modes_are_umask_independent(tmp_path, mask):
    """D2: rights are applied with fchmod on our own fd, so the process
    umask (022 → 0640 and 002 → 0660 under a naive os.open(mode=0660))
    can never weaken the published0660."""
    old = os.umask(mask)
    try:
        lim = _fast_limiter(tmp_path)
        out = _grant(lim)
        assert lim.record_result(out.grant_id, "P0", "OK",
                                 epoch=out.epoch) == "APPLIED"
        state = Path(lim.config.state_path)
        lock = Path(lim.lock_path)
        assert stat.S_IMODE(state.stat().st_mode) == 0o660
        assert stat.S_IMODE(lock.stat().st_mode) == 0o660
        # witness files use the same create-then-publish strategy
        fh = lim._hold_witness("shared_state.json.manual.witness")
        fh.close()
        witness = tmp_path / "shared_state.json.manual.witness"
        assert stat.S_IMODE(witness.stat().st_mode) == 0o660
    finally:
        os.umask(old)


@_POSIX_ONLY
def test_existing_lock_file_rights_never_rewritten(tmp_path):
    """No implicit migration: a pre-existing file (legacy 0640 layout)
    keeps ITS mode — the code only ever modes files it created itself."""
    lock = tmp_path / "shared_state.json.lock"
    lock.write_bytes(b"")
    os.chmod(lock, 0o640)
    lim = _fast_limiter(tmp_path)
    out = _grant(lim)                       # owner still has rw → works
    assert out.granted
    assert stat.S_IMODE(lock.stat().st_mode) == 0o640  # untouched


# ============================ 2. D2/D3 — EACCES/EPERM fault injection
@pytest.mark.parametrize("err", [errno.EACCES, errno.EPERM],
                         ids=["eacces", "eperm"])
def test_witness_access_error_means_held_fail_closed(tmp_path, monkeypatch,
                                                     err):
    """D3: an UNDEFINED witness observation (permission denied) must read
    as STILL HELD — never as 'released' (no second recovery probe)."""
    lim = _fast_limiter(tmp_path)
    witness = tmp_path / "shared_state.json.g1-1.witness"
    witness.write_bytes(b"")               # the foreign holder's file
    _inject_open_error(
        monkeypatch,
        lambda p: p.endswith(".witness"),
        lambda: PermissionError(err, "injected witness access error"))
    assert lim._witness_alive(witness.name) is True


def test_witness_missing_means_not_held_contract(tmp_path):
    """FileNotFoundError keeps the EXISTING contract (removed ⇒ not held)."""
    lim = _fast_limiter(tmp_path)
    assert lim._witness_alive("shared_state.json.absent.witness") is False


@_POSIX_ONLY
def test_witness_eacces_never_resolves_or_reissues_probe(tmp_path,
                                                         monkeypatch):
    """D3 end-to-end through _refresh: with an UNDEFINABLE witness the
    OPEN breaker keeps its probe (stalled at most) — the dead-probe path
    never runs, so no second recovery probe can be admitted."""
    lim = _fast_limiter(tmp_path)
    st = gr_module._default_state(lim.config, lim.clock.wall(),
                                  gr_module.current_boot_id())
    br = st["breaker"]
    br["state"] = gr_module.BKR_OPEN
    # cooldown in the future: the OPEN→RECOVERY transition cannot mask
    # the probe-resolution assertion below
    br["cooldown_until_wall"] = lim.clock.wall() + 3600.0
    br["probe"] = {
        "gid": "e9:1", "holder": "e9:1",
        "witness": "shared_state.json.e9-1.witness",
        "boot_id": gr_module.current_boot_id(),
        "marker": True, "stall_until": 0.0, "stalled": False,
    }
    witness = tmp_path / "shared_state.json.e9-1.witness"
    witness.write_bytes(b"")               # foreign holder's witness
    _inject_open_error(
        monkeypatch,
        lambda p: p.endswith(".witness"),
        lambda: PermissionError(errno.EACCES, "injected EACCES"))
    lim._refresh(st)
    # probe KEPT (fail-closed); the stall is surfaced, never resolved
    assert br["probe"] is not None
    assert br["state"] == gr_module.BKR_OPEN
    assert st["anomalies"]["probe_stalled"] == 1


@_POSIX_ONLY
def test_witness_missing_still_resolves_probe_contract(tmp_path):
    """The negative control: a truly REMOVED witness still releases the
    probe slot (the pre-existing contract D3 must not break)."""
    lim = _fast_limiter(tmp_path)
    st = gr_module._default_state(lim.config, lim.clock.wall(),
                                  gr_module.current_boot_id())
    br = st["breaker"]
    br["state"] = gr_module.BKR_OPEN
    br["cooldown_until_wall"] = lim.clock.wall() + 3600.0
    br["probe"] = {
        "gid": "e9:2", "holder": "e9:2",
        "witness": "shared_state.json.e9-2.witness",   # never created
        "boot_id": gr_module.current_boot_id(),
        "marker": True, "stall_until": 0.0, "stalled": False,
    }
    lim._refresh(st)
    assert br["probe"] is None              # removed ⇒ released (contract)
    assert br["state"] == gr_module.BKR_OPEN  # marker probe re-opens


def test_lock_eacces_denies_state_unavailable(tmp_path, monkeypatch):
    """D2: a lock file this user cannot open fails closed — the bucket
    never degrades to an in-process fallback."""
    _inject_open_error(
        monkeypatch,
        lambda p: p.endswith(".lock"),
        lambda: PermissionError(errno.EACCES, "injected EACCES on lock"))
    lim = _fast_limiter(tmp_path)
    out = lim.acquire("P0", deadline=lim.clock.mono() + 3.0)
    assert out.granted is False
    assert out.result is GrantResult.STATE_UNAVAILABLE
    assert out.reason == "lock_unavailable"


def test_state_write_eacces_denies_state_unavailable(tmp_path, monkeypatch):
    """D1: an EACCES while writing the state (tmp publish path) yields
    STATE_UNAVAILABLE and never publishes a file at all."""
    _inject_open_error(
        monkeypatch,
        lambda p: ".json.tmp." in p,
        lambda: PermissionError(errno.EACCES, "injected EACCES on state"))
    lim = _fast_limiter(tmp_path)
    out = lim.acquire("P0", deadline=lim.clock.mono() + 3.0)
    assert out.granted is False
    assert out.result is GrantResult.STATE_UNAVAILABLE
    assert out.reason == "write_failed"
    assert not Path(lim.config.state_path).exists()   # nothing published


# ============================ 3. D4 — symlink rejection (POSIX)
@_POSIX_ONLY
def test_symlink_state_file_fails_closed_read_failed(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked_state.json"
    link.symlink_to(target)
    lim = _fast_limiter(tmp_path, state_path=str(link))
    out = lim.acquire("P0", deadline=lim.clock.mono() + 3.0)
    assert out.granted is False
    assert out.result is GrantResult.STATE_UNAVAILABLE
    assert out.reason == "read_failed"


@_POSIX_ONLY
def test_symlink_lock_fails_closed_lock_unavailable(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "linked_state.json.lock"
    link.symlink_to(target)
    lim = _fast_limiter(tmp_path,
                        state_path=str(tmp_path / "linked_state.json"))
    out = lim.acquire("P0", deadline=lim.clock.mono() + 3.0)
    assert out.granted is False
    assert out.result is GrantResult.STATE_UNAVAILABLE
    assert out.reason == "lock_unavailable"


@_POSIX_ONLY
def test_symlink_witness_fails_closed_and_reads_as_held(tmp_path):
    target = tmp_path / "victim2.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "wlink.witness"
    link.symlink_to(target)
    lim = _fast_limiter(tmp_path)
    with pytest.raises(LimiterUnavailableError) as excinfo:
        lim._hold_witness("wlink.witness")
    assert excinfo.value.code == "witness_failed"
    # ELOOP is an UNDEFINED observation → never "released" (D3)
    assert lim._witness_alive("wlink.witness") is True


@_POSIX_ONLY
def test_world_writable_state_dir_rejected_at_load(tmp_path):
    """D5: an enabled limiter refuses to start in a world-writable
    directory (any local account could tamper with the bucket)."""
    ww = tmp_path / "world_writable"
    ww.mkdir()
    os.chmod(ww, 0o777)
    with pytest.raises(ValueError, match="world-writable"):
        _load(tmp_path, {FLAG: True,
                         "global_limiter_state_path": str(ww / "s.json")})


@_POSIX_ONLY
def test_disabled_limiter_never_probes_world_writable_dir(tmp_path):
    """Default-off keeps the zero-I/O contract: no path probing at all."""
    ww = tmp_path / "world_writable_off"
    ww.mkdir()
    os.chmod(ww, 0o777)
    settings = _load(tmp_path, {"global_limiter_state_path":
                                str(ww / "s.json")})
    assert settings.global_bybit_rate_limiter_enabled is False


@_POSIX_ONLY
def test_symlink_state_path_rejected_at_load(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        _load(tmp_path, {FLAG: True,
                         "global_limiter_state_path": str(link)})


# ============================ 4. persistence errors → zero HTTP, no fallback
def test_persistence_eacces_get_fail_closed_zero_http(tmp_path, monkeypatch,
                                                      telemetry_env):
    """D1/D2 at the client boundary: a persistence EACCES during acquire
    stops the request before ANY HTTP attempt — no fallback to the
    process-local limiter, no retry at another layer."""
    session = ScriptedSession()
    client = _enabled_client(session, tmp_path)
    _inject_open_error(
        monkeypatch,
        lambda p: ".json.tmp." in p,
        lambda: PermissionError(errno.EACCES, "injected EACCES on state"))
    with pytest.raises(BybitRateLimitError, match="fail-closed"):
        client.get_klines("BTCUSDT")
    assert session.calls == []              # zero HTTP sends


# ============================ 5. two-UID Linux integration (root, isolated)
_TWO_UID_WORKER = '''
import json
import sys

from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    LimiterConfig,
)

state_path = sys.argv[1]
lim = GlobalBybitRateLimiter(LimiterConfig(enabled=True,
                                           state_path=state_path))
out = lim.acquire("P0", deadline=lim.clock.mono() + 10.0)
if not out.granted:
    print(json.dumps({"granted": False, "result": out.result.value,
                      "reason": out.reason}))
    sys.exit(2)
record = lim.record_result(out.grant_id, "P0", "OK", epoch=out.epoch)
print(json.dumps({"granted": True, "record": record}))
'''


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="two-UID integration needs root (setuid workers in a temp tree)")
def test_two_uids_share_one_absolute_state_path():
    """T1/T6 integration: tradbot-like and analytics-like UIDs share ONE
    absolute state_path through a setgid 2770 directory.

    * workers run with PRIMARY group 4243 while the directory group is
      4242 → a ``st_gid == 4242`` assertion proves setgid inheritance
      (group access is granted via the supplementary group only);
    * A writes → B reads+writes → A reads again: every step must grant
      and every record must APPLY, with the state file at 0660/4242 after
      each atomic replace;
    * nothing outside the isolated temporary tree is touched and no
      real user/group accounts are created or modified.
    """
    shared_gid = 4242          # the shared service group (dir group)
    primary_gid = 4243         # workers' PRIMARY group differs on purpose
    uid_a, uid_b = 14241, 14242
    work = Path(tempfile.mkdtemp(prefix="issue231_twouid_"))
    try:
        os.chown(work, -1, shared_gid)     # root: group = shared group
        os.chmod(work, 0o2770)             # setgid + group rwx (2770)
        worker = work / "worker.py"
        worker.write_text(_TWO_UID_WORKER, encoding="utf-8")
        state = work / "state.json"

        def run_as(uid):
            def _preexec():
                os.setgroups([shared_gid])
                os.setgid(primary_gid)
                os.setuid(uid)

            env = os.environ.copy()
            env["PYTHONPATH"] = (str(REPO_ROOT) + os.pathsep
                                 + env.get("PYTHONPATH", ""))
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [sys.executable, str(worker), str(state)],
                capture_output=True, text=True, timeout=60,
                cwd=str(work), env=env, preexec_fn=_preexec)
            assert proc.returncode == 0, (
                f"uid={uid} failed rc={proc.returncode}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}")
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            assert payload["granted"] is True, payload
            assert payload["record"] == "APPLIED", payload

        run_as(uid_a)             # first writer: creates state 0660/4242
        run_as(uid_b)             # second UID: reads A's state, writes own
        run_as(uid_a)             # first UID again: reads B's replacement

        stt = state.stat()
        assert stat.S_IMODE(stt.st_mode) == 0o660
        assert stt.st_gid == shared_gid     # setgid inheritance, NOT 4243
        assert stt.st_uid in (uid_a, uid_b)
        lock = Path(str(state) + ".lock")
        ltt = lock.stat()
        assert stat.S_IMODE(ltt.st_mode) == 0o660
        assert ltt.st_gid == shared_gid
        assert ltt.st_uid in (uid_a, uid_b)
    finally:
        shutil.rmtree(work, ignore_errors=True)
