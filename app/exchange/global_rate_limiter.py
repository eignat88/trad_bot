"""GLOBAL_BYBIT_RATE_LIMITER_V1 — cross-process Bybit request limiter core.

Issue #231, Phase 3 (CORE ONLY / DEFAULT-OFF / NO DEPLOY). This module is an
INDEPENDENT core implementing the Phase 2 R2–R4 architecture contracts. It is
NOT wired into ``BybitClient``, the scanner, paper trading or any other
consumer yet — integration is a separate phase.

Contracts implemented here
--------------------------
* **Global token bucket** — G=8 req/s sustained, B=12 burst, one shared
  budget across processes guarded by an OS file lock (``fcntl.flock`` on
  POSIX, ``msvcrt.locking`` byte ranges on Windows for local/unit testing).
* **Priority fairness** — weights P0:P1:P2:P3 = 4:2:1.5:1.5 normalized to
  G (``s_i = G·w_i/Σw``): 3.5556 / 1.7778 / 1.3333 / 1.3333 req/s. Shared
  pending registry, strict priority order among eligible waiters, FIFO
  inside a class, work-conserving borrowing with a reserve rule (a borrow
  leaves ≥1 token for every other backlogged class).
* **Shared breaker** — ``CLOSED / OPEN / RECOVERY``; cooldowns 5/30/120 s;
  20 unique successful results reset to CLOSED; ``epoch`` fencing (every
  trip revokes outstanding grants); dedup by ``grant_id``; at most ONE
  admitted recovery probe at a time.
* **Durable state** — crash-safe init, checksum-validated JSON state,
  atomic replace (temp O_EXCL → fsync file → rename → fsync parent dir),
  corrupt state → FAIL_CLOSED, clock-jump protection, safe restart.
* **FAIL_CLOSED** — when enabled, every unavailable-state condition surfaces
  as ``GrantResult.STATE_UNAVAILABLE`` (or ``False`` from ``prepare_send``);
  there is no degraded local fallback mode.
* **Default-off** — with ``enabled=False`` no shared-state file is opened
  and no lock is taken (zero I/O).

Safety contracts (R4 / Phase 3 §3)
----------------------------------
* **Recovery in-flight (§3.1):** lease/stall expiry is NEVER treated as
  evidence that the HTTP attempt finished. A new probe is admitted only
  when the previous holder is PROVABLY gone (its OS witness lock was
  released — process death) or has delivered its own ``record_result``.
  A live-but-stalled holder keeps the slot blocked (fail-closed), surfacing
  a ``probe_stalled`` anomaly after the stall horizon.
* **Grant lifecycle (§3.2):** ``ISSUED → TERMINAL`` in a bounded registry;
  a terminal record is written exactly once (replay → no streak/level
  change); non-terminal records are never evicted; registry overflow fails
  closed (``registry_capacity``).
* **Accounting (§3.3):** ``total_grants``, ``epoch_grants``,
  ``usage_by_priority``, ``tokens_available``, ``refill_total`` all updated
  in ONE lock section; one GRANTED == exactly one token (``usage += 1``);
  exact invariant ``tokens == B + refill_total − total_grants``.

Phase 3D remediation (independent code review F1–F8)
----------------------------------------------------
* **F1 breaker epoch fencing:** a ``RATE_LIMIT`` from a STALE epoch is
  terminal diagnostic evidence only — it never re-trips or re-escalates the
  breaker. Fresh (current-epoch) rate limits escalate the 5→30→120 ladder
  exactly once per episode.
* **F2 bounded grant lifecycle:** ordinary grants carry ``pid``/``boot_id``/
  ``sent``. A provably dead owner's non-terminal record is reconciled to a
  neutral terminal outcome (``UNKNOWN`` when a send-marker exists, else
  ``NOT_SENT``) — never a success, never a token refund, never a breaker
  change. Terminal records age out by retention, keeping the registry bounded.
* **F3 record retry:** ``record_result`` retries a bounded number of times on
  a transient FAIL_CLOSED error without re-sending HTTP; persistence is atomic
  so a retry cannot double-count a transition. The probe witness is released
  only AFTER the result is durable; on exhaustion the slot stays held.
* **F4 read-only snapshots:** ``write=False`` transactions take a POSIX shared
  lock and never mutate/refresh state.
* **F5 pending liveness:** a pending demand is renewed by its (live) owner and
  lapses after a short liveness window when abandoned, so one process's leaked
  entry cannot block a whole class for the full ``demand_ttl_s``.
* **F6 witness cleanup:** ``prune_witness_files()`` is a separate, provably-safe
  maintenance contract; normal operation never unlinks a witness.
* **F7 bounded wait:** ``acquire(deadline=None)`` waits at most ``max_wait_s``;
  the poll sleep never overshoots the deadline.
* **F8 send fencing:** ``prepare_send`` is a best-effort pre-send fence to be
  called immediately before the transport; it cannot cancel an HTTP request
  that already started.

Public API
----------
``acquire(priority, deadline, logical_request_id) -> GrantOutcome``
    Register demand, wait deadline-aware, get one grant (persisted BEFORE
    any HTTP send may happen). ``deadline=None`` is bounded by ``max_wait_s``.
``prepare_send(grant_id) -> bool``
    Best-effort pre-send fence (F8): ``False`` → the attempt must NOT be sent
    (stale epoch, terminal or unknown grant). Durably marks the grant
    ``sent``. Must be called immediately before the transport.
``record_result(grant_id, priority, outcome, ...) -> str``
    Exactly-once terminal recording driving the breaker (dedup by
    ``grant_id``; caller-supplied ``epoch`` is informational — the epoch
    embedded in ``grant_id`` is authoritative). Bounded retry, no HTTP re-send.
``refresh()`` / ``snapshot()`` — maintenance and observability.
``prune_witness_files()`` — safe, periodic witness-file cleanup (F6).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator
from uuid import uuid4

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ constants
PRIORITY_P0 = "P0"
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITY_ORDER = (PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, PRIORITY_P3)

#: Approved starting weights (Issue #231) — ratios only; normalized to G.
DEFAULT_WEIGHTS: dict[str, float] = {
    PRIORITY_P0: 4.0, PRIORITY_P1: 2.0, PRIORITY_P2: 1.5, PRIORITY_P3: 1.5,
}
DEFAULT_G_RPS = 8.0
DEFAULT_BURST = 12.0
DEFAULT_COOLDOWNS_S = (5.0, 30.0, 120.0)
RECOVERY_SUCCESS_TARGET = 20
DEFAULT_STATE_PATH = "var/state/global_bybit_limiter_v1.json"
STATE_SCHEMA = 1

BKR_CLOSED = "CLOSED"
BKR_OPEN = "OPEN"
BKR_RECOVERY = "RECOVERY"
BROKEN_STATES = (BKR_CLOSED, BKR_OPEN, BKR_RECOVERY)

#: Valid ``record_result`` outcomes.
OUTCOME_OK = "OK"
OUTCOME_RATE_LIMIT = "RATE_LIMIT"
VALID_OUTCOMES = frozenset({
    OUTCOME_OK, OUTCOME_RATE_LIMIT, "TIMEOUT", "AUTH", "ERROR",
    "NOT_SENT", "REVOKED",
})

#: Registry record values for terminal outcomes (subset used by recovery).
OUTCOME_UNKNOWN = "UNKNOWN"

_LOCK_ERRNOS = frozenset(
    x for x in (getattr(errno, "EACCES", None), getattr(errno, "EAGAIN", None),
                getattr(errno, "EWOULDBLOCK", None),
                getattr(errno, "EDEADLK", None)) if x is not None)


# --------------------------------------------------------------------- errors
class LimiterUnavailableError(Exception):
    """Any FAIL_CLOSED shared-state failure (never carries secrets)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------- result types
class GrantResult(str, Enum):
    GRANTED = "GRANTED"
    SKIPPED = "SKIPPED"
    BREAKER_OPEN = "BREAKER_OPEN"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"


@dataclass(frozen=True)
class GrantOutcome:
    result: GrantResult
    grant_id: str = ""
    epoch: int = 0
    waited_ms: int = 0
    retry_after_ms: int = 0
    probe: bool = False
    reason: str = ""

    @property
    def granted(self) -> bool:
        return self.result is GrantResult.GRANTED


# --------------------------------------------------------------------- config
@dataclass(frozen=True)
class LimiterConfig:
    """Core configuration. Defaults reproduce the approved contract exactly."""

    enabled: bool = False
    state_path: str = DEFAULT_STATE_PATH
    g_rps: float = DEFAULT_G_RPS
    burst: float = DEFAULT_BURST
    weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS))
    credit_k_s: float = 1.0
    lock_timeout_s: float = 5.0
    min_poll_s: float = 0.005
    max_poll_s: float = 0.05
    demand_ttl_s: float = 120.0
    pending_capacity: int = 1024
    breaker_cooldowns_s: tuple[float, ...] = DEFAULT_COOLDOWNS_S
    recovery_success_target: int = RECOVERY_SUCCESS_TARGET
    probe_lease_s: float = 30.0
    clock_step_cap_s: float = 30.0
    registry_capacity: int = 8192
    max_lifetime_s: float = 60.0
    loop_iter_cap: int = 10000
    # --- Phase 3D remediation knobs (all default-safe / default-off) ---
    #: Absolute wait cap when ``acquire`` is called with ``deadline=None``
    #: (F7): bounds polling to a sane horizon instead of ``loop_iter_cap``.
    max_wait_s: float = 30.0
    #: Shared-state liveness window for a pending demand (F5). A live
    #: waiter renews its entry before it lapses; an abandoned entry (drop
    #: failed / owner idle) is pruned by others within this window instead
    #: of the full ``demand_ttl_s``.
    pending_liveness_ttl_s: float = 15.0
    #: Bounded retries of ``record_result`` persistence on a transient
    #: FAIL_CLOSED error (F3). Retries never re-send HTTP.
    record_retry_attempts: int = 3
    record_retry_backoff_s: float = 0.05

    def __post_init__(self) -> None:
        if self.g_rps <= 0:
            raise ValueError("g_rps must be positive")
        if self.burst < 1:
            raise ValueError("burst must be >= 1")
        for prio in PRIORITY_ORDER:
            if float(self.weights.get(prio, 0.0)) <= 0:
                raise ValueError(f"weight[{prio}] must be positive")
        if len(self.breaker_cooldowns_s) < 1 or \
                any(c < 0 for c in self.breaker_cooldowns_s):
            raise ValueError("breaker_cooldowns_s must be non-negative")
        if self.recovery_success_target < 1:
            raise ValueError("recovery_success_target must be >= 1")
        if self.max_lifetime_s <= 0:
            raise ValueError("max_lifetime_s must be positive")
        if self.max_wait_s <= 0:
            raise ValueError("max_wait_s must be positive")
        if self.pending_liveness_ttl_s <= 0:
            raise ValueError("pending_liveness_ttl_s must be positive")
        if self.record_retry_attempts < 1:
            raise ValueError("record_retry_attempts must be >= 1")
        if self.record_retry_backoff_s < 0:
            raise ValueError("record_retry_backoff_s must be non-negative")
        # R4-2 retention rule: capacity must cover the replay window.
        import math
        needed = math.ceil(2.0 * self.g_rps * self.max_lifetime_s)
        if self.registry_capacity < needed:
            raise ValueError(
                f"registry_capacity {self.registry_capacity} < "
                f"2*G*max_lifetime ({needed})")

    @property
    def shares(self) -> dict[str, float]:
        """Effective per-class rates s_i = G·w_i/Σw (Σ == G exactly)."""
        total = sum(float(self.weights[p]) for p in PRIORITY_ORDER)
        return {p: self.g_rps * float(self.weights[p]) / total
                for p in PRIORITY_ORDER}

    @property
    def credits(self) -> dict[str, float]:
        """Eligibility credit per class, in requests (K · s_i)."""
        return {p: self.credit_k_s * s for p, s in self.shares.items()}

    def cooldown_for(self, level: int) -> float:
        """Cooldown for an escalation ``level`` (0 = never tripped, 1 = the
        first trip → ``cooldowns[0]`` = 5 s, 2 → 30 s, 3+ → 120 s)."""
        idx = min(max(int(level) - 1, 0), len(self.breaker_cooldowns_s) - 1)
        return float(self.breaker_cooldowns_s[idx])

    @classmethod
    def from_settings(cls, settings: Any = None) -> "LimiterConfig":
        """Read config defensively; production Settings knows none of these
        keys yet ⇒ default-off (no production configuration is modified)."""
        def get(name: str, default: Any) -> Any:
            return getattr(settings, name, default) if settings is not None \
                else default

        cooldowns = get("global_limiter_breaker_cooldowns_s",
                        DEFAULT_COOLDOWNS_S)
        return cls(
            enabled=bool(get("global_bybit_rate_limiter_enabled", False)),
            state_path=str(get("global_limiter_state_path",
                               DEFAULT_STATE_PATH)),
            g_rps=float(get("global_limiter_g_rps", DEFAULT_G_RPS)),
            burst=float(get("global_limiter_burst", DEFAULT_BURST)),
            lock_timeout_s=float(get("global_limiter_lock_timeout_s", 5.0)),
            demand_ttl_s=float(get("global_limiter_demand_ttl_s", 120.0)),
            breaker_cooldowns_s=tuple(float(c) for c in cooldowns),
            recovery_success_target=int(
                get("global_limiter_recovery_success_target",
                    RECOVERY_SUCCESS_TARGET)),
            probe_lease_s=float(get("global_limiter_probe_lease_s", 30.0)),
            registry_capacity=int(get("global_limiter_registry_capacity",
                                      8192)),
            max_lifetime_s=float(get("global_limiter_max_lifetime_s", 60.0)),
            max_wait_s=float(get("global_limiter_max_wait_s", 30.0)),
            pending_liveness_ttl_s=float(
                get("global_limiter_pending_liveness_ttl_s", 15.0)),
            record_retry_attempts=int(
                get("global_limiter_record_retry_attempts", 3)),
            record_retry_backoff_s=float(
                get("global_limiter_record_retry_backoff_s", 0.05)),
        )


# ---------------------------------------------------------------------- clock
class SystemClock:
    """Real monotonic/wall clock (sleep = real time)."""

    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def current_boot_id() -> str:
    """Stable per-boot identifier (POSIX); 'local' fallback elsewhere.

    Phase 3F / D3 — documented Windows/dev limitation: Windows has no
    stdlib per-boot identity (no ``/proc/sys/kernel/random/boot_id``
    equivalent; ``wmic`` is deprecated/removed on Win11; a
    ``GetTickCount64``-derived id would break existing state files written
    with ``"local"`` by reconciling every in-flight record of a
    mixed-version deployment as foreign-boot — an unsafe recovery). The
    reboot dimension is therefore deliberately inert on Windows and F2
    relies on the tri-state owner-PID check below. A Windows PID reused
    after a crash can only DELAY reconciliation (fail-closed cleanup),
    never free a live in-flight request.
    """
    if os.name == "posix":
        try:
            with open("/proc/sys/kernel/random/boot_id",
                      encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return "local"


# ---------------------------------------------------------------- pid liveness
# Phase 3F / D3: tri-state owner-liveness model.
#   ALIVE    — the PID is (still) a running process.
#   DEAD     — the PID is PROVABLY gone: the only state that may close an
#              orphaned non-terminal grant (F2) or prune a foreign demand.
#   UNKNOWN  — the check could not be performed. UNKNOWN is NEVER treated
#              as DEAD: reconciliation waits instead of freeing a grant
#              that may still back a live in-flight HTTP operation.
PID_ALIVE = "alive"
PID_DEAD = "dead"
PID_UNKNOWN = "unknown"


def _posix_pid_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
        return PID_ALIVE
    except ProcessLookupError:            # ESRCH: no such process
        return PID_DEAD
    except PermissionError:               # EPERM: exists, foreign owner
        return PID_ALIVE
    except OSError:                       # exotic errno — not provable
        return PID_UNKNOWN


def _win_pid_state(pid: int) -> str:
    """Windows tri-state via kernel32 (ctypes — stdlib only, no deps).

    Phase 3F / D3: NEVER use ``os.kill(pid, 0)`` as a Windows liveness
    probe — it maps to ``TerminateProcess`` and would KILL the target.
    ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + exit-code check:
      * OpenProcess → ERROR_INVALID_PARAMETER (87): no such PID → DEAD;
      * OpenProcess → ERROR_ACCESS_DENIED (5): the PID exists but is
        protected → ALIVE (conservative — never frees anything);
      * GetExitCodeProcess == STILL_ACTIVE (259) → ALIVE (259 is also a
        legitimate exit code — conservative);
      * any other failure → UNKNOWN.
    PID reuse after a crash can only turn a DEAD owner into ALIVE:
    cleanup is delayed fail-closed, never wrongly granted.
    """
    if pid <= 0:
        return PID_UNKNOWN
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return PID_UNKNOWN
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                             int(pid) & 0xFFFFFFFF)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return PID_DEAD
        if err == ERROR_ACCESS_DENIED:    # exists, not ours to query
            return PID_ALIVE
        return PID_UNKNOWN
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return PID_UNKNOWN
        return PID_ALIVE if code.value == STILL_ACTIVE else PID_DEAD
    finally:
        k32.CloseHandle(handle)


def _pid_state(pid: int) -> str:
    """Tri-state owner liveness (D3). UNKNOWN ≠ DEAD, always."""
    if pid <= 0:
        return PID_UNKNOWN
    if os.name == "posix":
        return _posix_pid_state(pid)
    return _win_pid_state(pid)


def _pid_alive(pid: int) -> bool:
    """Fail-closed liveness: True unless the owner is PROVABLY DEAD.

    UNKNOWN (Windows ACCESS_DENIED, exotic POSIX errno, pid <= 0) maps to
    True — reconciliation must never free a grant while a live in-flight
    HTTP operation remains possible (D3).
    """
    return _pid_state(pid) != PID_DEAD


# ---------------------------------------------------------------- file locking
def _try_lock(fh: Any, shared: bool = False) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt
        # Windows byte-range locks have no shared mode; a "read" lock is
        # still exclusive here (POSIX gets true LOCK_SH below).
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fh.fileno(), flags | fcntl.LOCK_NB)


def _unlock(fh: Any) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _is_lock_conflict(exc: OSError) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    if getattr(exc, "winerror", None) in (33, 36):   # Win8/lock violation
        return True
    return getattr(exc, "errno", None) in _LOCK_ERRNOS


def _release_fh(fh: Any) -> None:
    """Unlock + close a witness handle; never raises (best-effort cleanup).

    Phase 3F / D2: single release path used whenever a grant transaction
    fails to commit — the OS lock must not outlive the operation that
    took it (an fd + lock leak would block probe resolution until the
    process exits).
    """
    try:
        _unlock(fh)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


class _FileLock:
    """Non-blocking flock loop with a real-time timeout (FAIL_CLOSED).

    ``shared=True`` takes a POSIX shared (read) lock so observability
    (``snapshot``/``grant_outcome``) can run concurrently with other
    readers while still excluding writers. Windows degrades to exclusive.
    """

    def __init__(self, path: str, timeout_s: float,
                 shared: bool = False) -> None:
        self._path = path
        self._timeout_s = max(0.01, float(timeout_s))
        self._shared = bool(shared)
        self._fh: Any = None

    def __enter__(self) -> "_FileLock":
        try:
            self._fh = open(self._path, "a+")
        except OSError as exc:
            raise LimiterUnavailableError("lock_unavailable") from exc
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                _try_lock(self._fh, self._shared)
                return self
            except OSError as exc:
                if not _is_lock_conflict(exc):
                    self._close()
                    raise LimiterUnavailableError(
                        "lock_unavailable") from exc
                if time.monotonic() >= deadline:
                    self._close()
                    raise LimiterUnavailableError("lock_timeout") from exc
                time.sleep(random.uniform(0.002, 0.01))

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __exit__(self, *exc: Any) -> bool:
        if self._fh is not None:
            try:
                _unlock(self._fh)
            except OSError:
                pass
        self._close()
        return False


# -------------------------------------------------------------- durable state
def _ensure_dir(directory: str) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise LimiterUnavailableError("dir_unavailable") from exc
    if not os.path.isdir(directory):
        raise LimiterUnavailableError("dir_unavailable")


def _fsync_dir(directory: str) -> None:
    if os.name == "nt":
        return          # no portable directory fsync on Windows
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _checksum(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "checksum"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        raise LimiterUnavailableError("corrupt_state")
    required = ("wall_ts", "last_wall", "boot_id", "epoch",
                "epoch_start_wall", "seq", "tokens", "total_grants",
                "epoch_grants", "refill_total", "usage_by_priority",
                "pending", "grants", "breaker", "anomalies", "checksum")
    if any(k not in doc for k in required):
        raise LimiterUnavailableError("corrupt_state")
    br = doc.get("breaker")
    if not isinstance(br, dict) or br.get("state") not in BROKEN_STATES:
        raise LimiterUnavailableError("corrupt_state")
    usage = doc.get("usage_by_priority")
    if not isinstance(usage, dict) or set(usage) != set(PRIORITY_ORDER):
        raise LimiterUnavailableError("corrupt_state")
    for key in ("pending", "grants", "anomalies"):
        if not isinstance(doc.get(key), dict):
            raise LimiterUnavailableError("corrupt_state")
    for key in ("tokens", "total_grants", "epoch_grants", "refill_total",
                "seq", "epoch"):
        if not isinstance(doc.get(key), (int, float)) or \
                isinstance(doc.get(key), bool):
            raise LimiterUnavailableError("corrupt_state")
    if doc.get("checksum") != _checksum(doc):
        raise LimiterUnavailableError("corrupt_state")
    return doc


def _default_state(cfg: LimiterConfig, wall: float, boot: str) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "checksum": "",
        "wall_ts": wall,
        "last_wall": wall,
        "boot_id": boot,
        "epoch": 1,
        "epoch_start_wall": wall,
        "seq": 0,
        "tokens": float(cfg.burst),
        "total_grants": 0,
        "epoch_grants": 0,
        "refill_total": 0.0,
        "usage_by_priority": {p: 0 for p in PRIORITY_ORDER},
        "pending": {},
        "grants": {},
        "breaker": {
            "state": BKR_CLOSED,
            "level": 0,
            "cooldown_until_wall": 0.0,
            "success_streak": 0,
            "unknown_count": 0,
            "probe": None,
        },
        "anomalies": {"unknown_grant": 0, "probe_stalled": 0,
                      "pending_pruned": 0,
                      # Phase 3F / D1: live waiter re-registered after its
                      # own pending record was pruned mid-wait.
                      "pending_reregistered": 0},
    }


def _witness_name(state_path: str, gid: str) -> str:
    return f"{os.path.basename(state_path)}.{gid.replace(':', '-')}.witness"


# ================================================================== the core
class GlobalBybitRateLimiter:
    """Cross-process Bybit rate limiter (see module docstring)."""

    _instance: "GlobalBybitRateLimiter | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, config: LimiterConfig | None = None, *,
                 clock: Any | None = None,
                 settings: Any | None = None) -> None:
        self.config = config or LimiterConfig.from_settings(settings)
        self.clock = clock or SystemClock()
        # In-process serialization (reduces same-process file-lock convoys).
        self._txn_lock = threading.Lock()
        # Own live probe witnesses: gid -> open handle (lock held).
        self._witnesses: dict[str, Any] = {}
        # Pending entries whose drop failed under lock contention (this
        # instance only): they must never block this owner's later grants.
        self._leaked: set[str] = set()
        self._dirty = False

    # ------------------------------------------------------------- singleton
    @classmethod
    def get_instance(cls, settings: Any = None
                     ) -> "GlobalBybitRateLimiter":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(settings=settings)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    # ---------------------------------------------------------------- paths
    @property
    def state_path(self) -> str:
        return self.config.state_path

    @property
    def lock_path(self) -> str:
        return self.state_path + ".lock"

    def _witness_path(self, name: str) -> str:
        return os.path.join(os.path.dirname(self.state_path) or ".", name)

    # --------------------------------------------------------------- witness
    def _hold_witness(self, name: str) -> Any:
        path = self._witness_path(name)
        try:
            fh = open(path, "a+")
            _try_lock(fh)
            return fh
        except OSError as exc:
            try:
                fh.close()          # type: ignore[has-type]
            except Exception:
                pass
            raise LimiterUnavailableError("witness_failed") from exc

    def _release_witness(self, gid: str) -> None:
        fh = self._witnesses.pop(gid, None)
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def _witness_alive(self, name: str) -> bool:
        """True when SOMEONE (possibly this process) holds the witness lock.
        Open WITHOUT create: a missing witness is not proof of liveness —
        but for probe resolution the probe record always stores a witness
        that was created before persistence, so 'missing' means removed ⇒
        not held."""
        path = self._witness_path(name)
        try:
            fh = open(path, "r+b")
        except OSError:
            return False
        try:
            try:
                _try_lock(fh)
            except OSError as exc:
                if _is_lock_conflict(exc):
                    return True
                return False
            try:
                _unlock(fh)
            except OSError:
                pass
            return False
        finally:
            try:
                fh.close()
            except OSError:
                pass

    # ----------------------------------------------------------- transactions
    def _ensure_paths(self) -> None:
        _ensure_dir(os.path.dirname(self.state_path) or ".")

    def _load(self, write: bool) -> dict[str, Any]:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            if write:
                return _default_state(self.config, self.clock.wall(),
                                      current_boot_id())
            return _default_state(self.config, self.clock.wall(),
                                  current_boot_id())
        except OSError as exc:
            raise LimiterUnavailableError("read_failed") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise LimiterUnavailableError("corrupt_state") from exc
        return _validate(doc)

    def _write_state(self, st: dict[str, Any]) -> None:
        directory = os.path.dirname(self.state_path) or "."
        tmp = (f"{self.state_path}.tmp.{os.getpid()}."
               f"{uuid4().hex[:8]}")
        st["wall_ts"] = self.clock.wall()
        st["checksum"] = _checksum(st)
        payload = json.dumps(st, sort_keys=True, separators=(",", ":"))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
            _fsync_dir(directory)
        except Exception as exc:                     # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise LimiterUnavailableError("write_failed") from exc

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[dict[str, Any]]:
        """LOCK → read(+init) → yield → fsync → atomic replace → unlock.

        Read-only transactions (``write=False``) take a POSIX SHARED lock so
        observability runs concurrently with other readers yet still excludes
        writers; they never mutate or refresh state (F4).

        Mutations are persisted on EVERY exit path (including ``return``
        inside the with-block). A write failure overrides the return value
        (FAIL_CLOSED). If the block is already unwinding an exception, a
        write failure is logged and does not mask the original error.
        """
        with self._txn_lock:
            self._ensure_paths()
            with _FileLock(self.lock_path, self.config.lock_timeout_s,
                           shared=not write):
                self._dirty = False
                st: dict[str, Any] | None = None
                code = "unknown"
                returned = False
                try:
                    st = self._load(write)
                    yield st
                    returned = True
                except LimiterUnavailableError as exc:
                    code = exc.code
                    raise
                finally:
                    if write and self._dirty and st is not None:
                        try:
                            self._write_state(st)
                        except LimiterUnavailableError:
                            if returned:
                                raise
                            logger.critical(
                                "global_rate_limiter: state write failed "
                                "while unwinding (%s)", code)

    # ------------------------------------------------------------- bookkeeping
    def _set_dirty(self) -> None:
        self._dirty = True

    # ---------------------------------------------------------------- refresh
    def _refresh(self, st: dict[str, Any]) -> None:
        """Clock rules, pending prune, probe resolution, breaker transition,
        refill, registry eviction. All mutations mark the txn dirty."""
        # self-heal: remove this instance's stale duplicates whose drop
        # failed earlier (never another owner's live demand).
        for rid in list(self._leaked):
            if rid in st["pending"]:
                st["pending"].pop(rid)
                st["anomalies"]["pending_pruned"] += 1
                self._set_dirty()
            self._leaked.discard(rid)
        cfg = self.config
        wall = self.clock.wall()
        boot = current_boot_id()
        br = st["breaker"]
        dirty = False

        # --- clock rules (R2 §6 / Phase 3 §2): never mint extra tokens.
        if st["boot_id"] != boot:
            # reboot: re-baseline, no credit for the gap; counters survive.
            st["boot_id"] = boot
            st["last_wall"] = wall
            dirty = True
            delta = 0.0
        else:
            delta = wall - st["last_wall"]
            if delta < 0:
                delta = 0.0                          # backward: freeze credit
            elif delta > cfg.clock_step_cap_s:
                # forward jump: re-baseline WITHOUT credit; never shorten
                # an open cooldown.
                st["last_wall"] = wall
                dirty = True
                delta = 0.0
                if br["state"] == BKR_OPEN:
                    br["cooldown_until_wall"] = max(
                        br["cooldown_until_wall"],
                        wall + cfg.cooldown_for(br["level"]))
                    dirty = True
            elif delta > 0:
                credit = cfg.g_rps * delta
                applied = min(credit, max(0.0, cfg.burst - st["tokens"]))
                st["tokens"] += applied
                st["refill_total"] += applied
                st["last_wall"] = wall
                # Credit is NEVER persisted on its own: it is fully
                # re-derivable from the persisted (last_wall, tokens) pair
                # in every later transaction, and persisting it on each
                # poll would turn a read poll into a durable write (an
                # fsync convoy under cross-process contention). The next
                # grant / drop / trip persists it together with its own
                # mutation; crash re-derivation loses nothing (forward
                # jumps and reboots deliberately discard credit).

        # --- pending registry prune (TTL / foreign boot / dead owner).
        # Owner-PID pruning is tri-state (D3): only PROVABLY DEAD pids
        # prune here; UNKNOWN waits for the liveness TTL (fail-closed).
        for rid, entry in list(st["pending"].items()):
            dead = False
            if not isinstance(entry, dict):
                dead = True
            elif entry.get("expire_wall", 0) < wall:
                dead = True
            elif entry.get("boot_id") != boot:
                dead = True
            elif entry.get("pid") != os.getpid() and \
                    not _pid_alive(int(entry.get("pid", -1))):
                dead = True
            if dead:
                st["pending"].pop(rid, None)
                st["anomalies"]["pending_pruned"] = (
                    st["anomalies"].get("pending_pruned", 0) + 1)
                dirty = True

        # --- recovery probe resolution (§3.1): time NEVER frees the slot.
        probe = br.get("probe")
        if probe is not None and isinstance(probe, dict):
            if probe.get("boot_id") != boot or \
                    not self._witness_alive(str(probe.get("witness", ""))):
                self._resolve_dead_probe(st, probe, wall)
                dirty = True
            elif wall >= probe.get("stall_until", 0) and \
                    not probe.get("stalled"):
                # Live-but-stalled holder: keep blocking (fail-closed),
                # only surface the anomaly. A new probe is NOT admitted.
                probe["stalled"] = True
                st["anomalies"]["probe_stalled"] = (
                    st["anomalies"].get("probe_stalled", 0) + 1)
                dirty = True
                logger.warning(
                    "global_rate_limiter: probe holder alive past stall "
                    "horizon (gid=%s) — recovery stays blocked",
                    probe.get("holder", "?"))

        # --- OPEN → RECOVERY after cooldown (lazy, idempotent).
        if br["state"] == BKR_OPEN and \
                wall >= br["cooldown_until_wall"]:
            br["state"] = BKR_RECOVERY
            br["probe"] = None
            br["success_streak"] = 0
            dirty = True

        # --- owner-death reconciliation for ORDINARY grants (F2).
        # A provably dead owner (foreign boot, or a tri-state-provably
        # DEAD owner PID — Windows included since D3; UNKNOWN, e.g.
        # Windows ACCESS_DENIED or a reused PID, is NEVER dead here)
        # leaves a non-terminal record. Close it as a NEUTRAL terminal
        # outcome:
        #   * no durable send-marker  -> NOT_SENT  (prepare_send is the
        #     transport gate, so the attempt provably never left the process)
        #   * durable send-marker set -> UNKNOWN   (indeterminate — never
        #     mis-reported as NOT_SENT)
        # It never counts as a success, never returns a token, and never
        # changes breaker state. Probe grants are owned by the witness path
        # above and are skipped here. Terminal records age out via the
        # retention policy below, keeping the registry bounded.
        for gid, rec in list(st["grants"].items()):
            if not isinstance(rec, dict) or rec.get("outcome") is not None:
                continue
            if rec.get("probe"):
                continue                      # witness path owns probes
            if "pid" not in rec or "boot_id" not in rec:
                continue                      # legacy identity: cannot prove
            gpid = int(rec.get("pid", -1))
            gboot = str(rec.get("boot_id", ""))
            if gboot == boot and gpid == os.getpid():
                continue                      # our own live grant
            dead = (gboot != boot) or (
                gpid != os.getpid() and not _pid_alive(gpid))
            if not dead:
                continue
            rec["outcome"] = OUTCOME_UNKNOWN if rec.get("sent") else "NOT_SENT"
            dirty = True
            logger.warning(
                "global_rate_limiter: dead owner reconciled (gid=%s) "
                "→ %s (neutral, no breaker change)", gid, rec["outcome"])

        # --- bounded registry: evict only TERMINAL + aged records (R4-2).
        for gid, rec in list(st["grants"].items()):
            if not isinstance(rec, dict):
                st["grants"].pop(gid, None)
                dirty = True
                continue
            if rec.get("outcome") is not None and \
                    (wall - float(rec.get("issued_wall", wall))) >= \
                    cfg.max_lifetime_s:
                st["grants"].pop(gid, None)
                dirty = True

        if dirty:
            self._set_dirty()

    def _resolve_dead_probe(self, st: dict[str, Any],
                            probe: dict[str, Any], wall: float) -> None:
        """Holder PROVABLY gone (witness released / other boot)."""
        gid = str(probe.get("holder", ""))
        marker = bool(probe.get("marker"))
        rec = st["grants"].get(gid)
        if isinstance(rec, dict) and rec.get("outcome") is None:
            rec["outcome"] = OUTCOME_UNKNOWN if marker else "NOT_SENT"
        br = st["breaker"]
        br["probe"] = None
        if marker:
            # Send possibly happened → indeterminate → FAIL_CLOSED: reopen
            # at the CURRENT level (no level bump — no rate-limit evidence),
            # revoke outstanding grants (epoch++), reset the streak.
            self._apply_open(st, wall, bump_level=False)
            br["unknown_count"] = br.get("unknown_count", 0) + 1
            logger.critical(
                "global_rate_limiter: indeterminate probe outcome "
                "(gid=%s) — breaker re-opened fail-closed", gid)
        else:
            logger.warning(
                "global_rate_limiter: probe holder died before send "
                "(gid=%s) — slot released without breaker change", gid)
        self._release_witness(gid)
        self._set_dirty()

    def _apply_open(self, st: dict[str, Any], wall: float, *,
                    bump_level: bool) -> None:
        cfg = self.config
        br = st["breaker"]
        if bump_level:
            br["level"] = min(br["level"] + 1,
                              len(cfg.breaker_cooldowns_s))
        br["state"] = BKR_OPEN
        br["cooldown_until_wall"] = wall + cfg.cooldown_for(br["level"])
        br["epoch"] = st["epoch"] + 1                 # fence outstanding
        st["epoch"] = br["epoch"]
        st["epoch_start_wall"] = wall
        st["epoch_grants"] = 0
        st["usage_by_priority"] = {p: 0 for p in PRIORITY_ORDER}
        br["success_streak"] = 0
        if br.get("probe"):
            self._release_witness(str(br["probe"].get("holder", "")))
        br["probe"] = None
        self._set_dirty()

    # ------------------------------------------------------------ selection
    def _head_of(self, st: dict[str, Any], prio: str) -> str | None:
        """FIFO head of a class: earliest registered, req_id tie-break.

        Entries whose drop failed earlier in THIS instance (``_leaked``)
        are skipped: a stale duplicate of a live owner must never block
        that owner's later demands. Scope is per instance only — entries
        of dead foreign owners are removed by the refresh prune/TTL.
        """
        best: tuple[float, str] | None = None
        for rid, entry in st["pending"].items():
            if not isinstance(entry, dict) or entry.get("prio") != prio:
                continue
            if rid in self._leaked:
                continue
            key = (float(entry.get("registered_wall", 0.0)), str(rid))
            if best is None or key < best:
                best = key
        return None if best is None else best[1]

    def _eligible(self, st: dict[str, Any], prio: str) -> bool:
        """usage_i ≤ s_i·(t − epoch_start) + credit_i   [req ≤ req]."""
        usage = int(st["usage_by_priority"].get(prio, 0))
        shares = self.config.shares
        credits = self.config.credits
        elapsed = max(0.0, self.clock.wall() - st["epoch_start_wall"])
        return usage <= shares[prio] * elapsed + credits[prio]

    def _best_eligible(self, st: dict[str, Any]) -> tuple[str, str] | None:
        """Strict priority order among eligible backlogged classes."""
        for prio in PRIORITY_ORDER:
            head = self._head_of(st, prio)
            if head is not None and self._eligible(st, prio):
                return (prio, head)
        return None

    def _borrow_candidate(self, st: dict[str, Any]) -> tuple[str, str] | None:
        """Work-conserving borrow: only against FACTUALLY free capacity —
        after this grant every other backlogged class keeps ≥1 token.

        The surplus goes to the class closest to its own curve (smallest
        usage/allowance ratio — the least over-curve, i.e. the most
        deserving); strict priority order breaks ties. This keeps a
        burst surplus from systematically feeding the top class.
        """
        elapsed = max(0.0, self.clock.wall() - st["epoch_start_wall"])
        best: tuple[float, int, str, str] | None = None
        for prio in PRIORITY_ORDER:
            head = self._head_of(st, prio)
            if head is None:
                continue
            others = sum(1 for other in PRIORITY_ORDER
                         if other != prio and self._head_of(st, other))
            if st["tokens"] - 1.0 < others:
                continue                      # reserve: every class ≥1
            usage = int(st["usage_by_priority"].get(prio, 0))
            allowance = (self.config.shares[prio] * elapsed
                         + self.config.credits[prio])
            ratio = usage / max(allowance, 1e-9)
            key = (ratio, PRIORITY_ORDER.index(prio), prio, head)
            if best is None or key < best:
                best = key
        return None if best is None else (best[2], best[3])

    def _wait_hint(self, st: dict[str, Any], prio: str) -> float:
        """Sleep hint until the next grant opportunity (fairness-capped)."""
        cfg = self.config
        hints: list[float] = []
        if st["tokens"] < 1.0:
            hints.append((1.0 - st["tokens"]) / cfg.g_rps)
        usage = int(st["usage_by_priority"].get(prio, 0))
        share = cfg.shares[prio]
        credit = cfg.credits[prio]
        if usage > credit:
            t_elig = st["epoch_start_wall"] + (usage - credit) / share
            hints.append(max(0.0, t_elig - self.clock.wall()))
        hint = min(hints) if hints else cfg.max_poll_s
        return min(max(hint, cfg.min_poll_s), cfg.max_poll_s)

    # --------------------------------------------------------------- grant
    def _try_grant(self, st: dict[str, Any], prio: str,
                   req: str) -> dict[str, Any] | None:
        cfg = self.config
        if st["tokens"] < 1.0:
            return None
        br = st["breaker"]
        recovery = br["state"] == BKR_RECOVERY
        if recovery and br.get("probe") is not None:
            return None                      # ONE probe at a time (§3.1)
        cand = self._best_eligible(st)
        if cand is not None:
            if cand != (prio, req):
                return None                  # better waiter has the turn
        else:
            if self._borrow_candidate(st) != (prio, req):
                return None
        if len(st["grants"]) >= cfg.registry_capacity:
            raise LimiterUnavailableError("registry_capacity")

        wall = self.clock.wall()
        boot = current_boot_id()
        next_seq = int(st["seq"]) + 1
        gid = f"e{st['epoch']}:{next_seq}"
        witness_fh = None
        witness = None
        if recovery:
            # Witness BEFORE any state mutation (§3.1): a failed witness
            # means no probe record may exist.
            witness = _witness_name(self.state_path, gid)
            try:
                witness_fh = self._hold_witness(witness)
            except LimiterUnavailableError:
                raise
        try:
            st["seq"] = next_seq
            st["tokens"] -= 1.0
            st["usage_by_priority"][prio] += 1        # exactly 1 (§3.3)
            st["epoch_grants"] += 1
            st["total_grants"] += 1
            st["grants"][gid] = {"prio": prio, "issued_wall": wall,
                                 "outcome": None, "probe": bool(recovery),
                                 # owner identity for bounded lifecycle (F2):
                                 # lets a provably dead owner's record be
                                 # reconciled to a neutral terminal outcome.
                                 "pid": os.getpid(), "boot_id": boot,
                                 "sent": False}
            if recovery:
                assert witness is not None
                br["probe"] = {
                    "holder": gid,
                    "pid": os.getpid(),
                    "boot_id": boot,
                    "witness": witness,
                    "granted_wall": wall,
                    "lease_until": wall + cfg.probe_lease_s,
                    "stall_until": wall + cfg.probe_lease_s,
                    "marker": False,
                    "stalled": False,
                }
                # Phase 3F / D2: deliberately NO ownership transfer here —
                # the commit runs only when the transaction exits; the
                # handle stays local and is registered by acquire() after
                # a successful write (see the returned dict below).
            st["pending"].pop(req, None)
            self._set_dirty()
            return {"gid": gid, "epoch": int(st["epoch"]),
                    "probe": bool(recovery),
                    # Phase 3F / D2: witness ownership passes to the CALLER
                    # with the result. The durable write has not happened
                    # yet (it runs at the transaction's exit), so the
                    # persistent mapping may only take it AFTER a
                    # successful commit; on a write failure the caller
                    # releases it (fail-closed, no fd/lock leak).
                    "witness_fh": witness_fh}
        except BaseException:
            if witness_fh is not None:       # never leak a held witness
                _release_fh(witness_fh)
            raise

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _gid_epoch(gid: str) -> int | None:
        try:
            epoch_part, _ = gid.split(":", 1)
            if not epoch_part.startswith("e"):
                return None
            return int(epoch_part[1:])
        except (ValueError, AttributeError):
            return None

    def _drop_locked(self, st: dict[str, Any], req: str) -> None:
        if req in st["pending"]:
            st["pending"].pop(req, None)
            self._set_dirty()

    def _drop(self, req: str) -> None:
        for attempt in (0, 1):
            try:
                with self._transaction(write=True) as st:
                    self._refresh(st)
                    self._drop_locked(st, req)
                return
            except LimiterUnavailableError as exc:
                if attempt == 0:              # contention spike: one retry
                    time.sleep(0.05)
                    continue
                # TTL prune is the documented backstop; until then this
                # stale duplicate must not block the owner's own grants.
                self._leaked.add(req)
                logger.warning("global_rate_limiter: pending drop failed "
                               "(%s)", exc.code)

    def _unavailable(self, exc: LimiterUnavailableError,
                     t0: float) -> GrantOutcome:
        return GrantOutcome(
            GrantResult.STATE_UNAVAILABLE,
            waited_ms=int(max(0.0, self.clock.mono() - t0) * 1000),
            reason=exc.code)

    # ------------------------------------------------------------------ API
    def acquire(self, priority: str, deadline: float | None = None,
                logical_request_id: str = "") -> GrantOutcome:
        """Try to obtain ONE grant for one HTTP attempt (see docstring)."""
        if priority not in PRIORITY_ORDER:
            raise ValueError(f"unknown priority {priority!r}")
        if not self.config.enabled:
            # default-off: ZERO shared-state I/O.
            return GrantOutcome(GrantResult.GRANTED, grant_id="-", epoch=0,
                                reason="disabled")
        cfg = self.config
        t0 = self.clock.mono()
        if deadline is None:
            # F7: bound an unbounded wait — deadline=None becomes a finite
            # horizon so acquire can never poll forever.
            deadline = t0 + cfg.max_wait_s
        if t0 >= deadline:
            return GrantOutcome(GrantResult.SKIPPED,
                                waited_ms=0, reason="deadline")
        req = uuid4().hex
        # Registration is merged into the FIRST loop transaction: a grant
        # that is immediately available then costs ONE durable write
        # instead of a register-write + grant-write convoy (each fsync can
        # stall every other process behind the lock).
        registered = False
        # Phase 3F / D1: the ORIGINAL FIFO anchor of this demand. It is
        # captured at first registration and reused if the shared record is
        # pruned mid-wait (clock step / liveness expiry), so a re-registered
        # waiter keeps its true arrival position and never displaces a
        # request that registered before it.
        first_reg_wall: float | None = None
        # Phase 3F / D2: the in-flight grant result and its witness handle
        # (recovery probes only). Ownership of the handle stays LOCAL until
        # the grant transaction commits; both error paths below release it
        # so a failed durable write can never leak an fd + OS lock.
        res: dict[str, Any] | None = None
        witness_fh: Any = None
        try:
            for _ in range(cfg.loop_iter_cap):
                now = self.clock.mono()
                if deadline is not None and now >= deadline:
                    retry_after = 0
                    try:
                        with self._transaction(write=True) as st:
                            self._refresh(st)
                            self._drop_locked(st, req)
                            if st["tokens"] < 1.0:
                                retry_after = int(
                                    (1.0 - st["tokens"]) / cfg.g_rps * 1000)
                    except LimiterUnavailableError as exc:
                        return self._unavailable(exc, t0)
                    return GrantOutcome(
                        GrantResult.SKIPPED,
                        waited_ms=int((now - t0) * 1000),
                        retry_after_ms=retry_after, reason="deadline")

                hint: float | None = None
                res = None                  # D2: per-iteration locals — a
                witness_fh = None           # committed grant always returns
                try:
                    with self._transaction(write=True) as st:
                        self._refresh(st)
                        if not registered:
                            if len(st["pending"]) >= cfg.pending_capacity:
                                raise LimiterUnavailableError(
                                    "pending_capacity")
                            now_wall = self.clock.wall()
                            st["pending"][req] = {
                                "prio": priority,
                                "pid": os.getpid(),
                                "boot_id": current_boot_id(),
                                "registered_wall": now_wall,
                                # F5: the entry lapses after the (short)
                                # liveness window unless renewed; it is
                                # hard-capped at registered+demand_ttl.
                                "expire_wall": min(
                                    now_wall + cfg.demand_ttl_s,
                                    now_wall + cfg.pending_liveness_ttl_s),
                                "lrid": str(logical_request_id)[:64],
                            }
                            self._set_dirty()
                            registered = True
                            # Phase 3F / D1: the FIFO anchor of THIS waiter,
                            # captured on first successful registration; used
                            # for a safe re-registration if the shared record
                            # is pruned mid-wait (see the else-branch below).
                            first_reg_wall = now_wall
                        else:
                            # F5 shared-state reconciliation: renew an
                            # already-registered demand before its liveness
                            # window lapses (throttled to < liveness/2 so it
                            # costs at most one write per window). A live
                            # waiter keeps its slot; an ABANDONED entry (drop
                            # failed / owner idle) is pruned by other
                            # processes within liveness, not the full TTL.
                            entry = st["pending"].get(req)
                            now_wall = self.clock.wall()
                            if entry is None:
                                # Phase 3F / D1: OUR OWN record was pruned
                                # mid-wait (forward wall-clock step past the
                                # liveness window, prune race with another
                                # process, leaked-drop self-heal) while this
                                # waiter is provably alive inside the SAME
                                # acquire() call. Re-register under the SAME
                                # request identity (same req key — a duplicate
                                # is impossible) anchored to the ORIGINAL
                                # first_reg_wall: the waiter keeps its true
                                # arrival position instead of being demoted
                                # behind requests that arrived during the
                                # gap, and no earlier-arriving request is
                                # displaced (their registered_wall is still
                                # smaller). The monotonic deadline is never
                                # touched, and the hard demand-TTL cap stays
                                # anchored at the ORIGINAL registration, so a
                                # stale demand (forward wall jump past the
                                # TTL / rollback re-baseline) is NOT restored.
                                anchor = first_reg_wall \
                                    if first_reg_wall is not None else now_wall
                                if now_wall - anchor >= cfg.demand_ttl_s:
                                    # demand outlived its hard wall TTL
                                    # mid-wait: stale demand must not be
                                    # resurrected — drop it fail-closed.
                                    self._drop_locked(st, req)
                                    return GrantOutcome(
                                        GrantResult.SKIPPED,
                                        waited_ms=int((now - t0) * 1000),
                                        reason="demand_expired")
                                if len(st["pending"]) >= cfg.pending_capacity:
                                    raise LimiterUnavailableError(
                                        "pending_capacity")
                                st["pending"][req] = {
                                    "prio": priority,
                                    "pid": os.getpid(),
                                    "boot_id": current_boot_id(),
                                    "registered_wall": anchor,
                                    # hard cap anchored at the ORIGINAL
                                    # registration; liveness renewed from now
                                    "expire_wall": min(
                                        anchor + cfg.demand_ttl_s,
                                        now_wall
                                        + cfg.pending_liveness_ttl_s),
                                    "lrid": str(logical_request_id)[:64],
                                }
                                st["anomalies"]["pending_reregistered"] = (
                                    st["anomalies"].get(
                                        "pending_reregistered", 0) + 1)
                                self._set_dirty()
                            else:
                                exp = float(entry.get("expire_wall", 0.0))
                                if exp - now_wall < \
                                        cfg.pending_liveness_ttl_s / 2.0:
                                    reg = float(entry.get(
                                        "registered_wall", now_wall))
                                    entry["expire_wall"] = min(
                                        reg + cfg.demand_ttl_s,
                                        now_wall + cfg.pending_liveness_ttl_s)
                                    self._set_dirty()
                        br = st["breaker"]
                        if br["state"] == BKR_OPEN:
                            if priority == PRIORITY_P3:
                                # P3 defers while the breaker is open.
                                self._drop_locked(st, req)
                                return GrantOutcome(
                                    GrantResult.BREAKER_OPEN,
                                    waited_ms=int((now - t0) * 1000),
                                    reason="breaker_open")
                            wait = max(
                                0.0,
                                br["cooldown_until_wall"] - self.clock.wall())
                            budget = (deadline - now) if deadline is not None \
                                else float("inf")
                            if wait >= budget:
                                # deadline cannot cover the cooldown.
                                self._drop_locked(st, req)
                                return GrantOutcome(
                                    GrantResult.BREAKER_OPEN,
                                    waited_ms=int((now - t0) * 1000),
                                    retry_after_ms=int(wait * 1000),
                                    reason="breaker_cooldown_exceeds_deadline")
                            hint = wait if wait > 0 else cfg.min_poll_s
                        else:
                            res = self._try_grant(st, priority, req)
                            if res is not None:
                                # Phase 3F / D2: take the witness handle out
                                # of the result; ownership stays local until
                                # the transaction below has committed.
                                witness_fh = res.pop("witness_fh", None)
                            else:
                                hint = self._wait_hint(st, priority)
                    # ---- durable commit reached (with-block exited
                    # cleanly). A write failure instead raises
                    # LimiterUnavailableError from the transaction's exit
                    # and is handled by the except below. ----
                    if res is not None:
                        if witness_fh is not None:
                            # transfer to the persistent mapping ONLY now
                            # that the probe record is durable (D2); after
                            # a failed write the handle is released below.
                            self._witnesses[res["gid"]] = witness_fh
                            witness_fh = None
                        return GrantOutcome(
                            GrantResult.GRANTED,
                            grant_id=res["gid"],
                            epoch=res["epoch"],
                            waited_ms=int((self.clock.mono() - t0) * 1000),
                            probe=res["probe"],
                            reason=("recovery_probe" if res["probe"]
                                    else "granted"))
                except LimiterUnavailableError as exc:
                    if witness_fh is not None:
                        # Phase 3F / D2: the grant transaction did NOT
                        # commit (fsync/replace failed) — release the
                        # witness this attempt acquired; the probe record
                        # is not durable, so no slot semantics change.
                        _release_fh(witness_fh)
                        witness_fh = None
                    self._drop(req)
                    return self._unavailable(exc, t0)

                # deadline-aware sleep, outside the lock, never busy.
                sleep_s = max(hint or cfg.max_poll_s, cfg.min_poll_s)
                if deadline is not None:
                    remaining = deadline - self.clock.mono()
                    if remaining <= 0:
                        continue                     # loop top → SKIPPED
                    if remaining <= cfg.min_poll_s:
                        # Too close to add jitter: sleep exactly up to the
                        # deadline (overshoot from sleeping == 0) and let the
                        # loop-top check return SKIPPED.
                        self.clock.sleep(remaining)
                        continue
                    # Cap so the jitter quantum can never push the sleep past
                    # the deadline: total sleep <= remaining (the only
                    # residual overshoot is the final transaction's latency).
                    sleep_s = min(sleep_s, remaining - cfg.min_poll_s)
                self.clock.sleep(sleep_s + random.uniform(
                    0.0, cfg.min_poll_s))

            self._drop(req)
            return self._unavailable(
                LimiterUnavailableError("loop_exceeded"), t0)
        except BaseException:
            # Any unexpected failure: fail-closed, never leave our demand
            # registered (TTL is the backstop).
            if witness_fh is not None:
                # Phase 3F / D2: release an acquired-but-uncommitted witness
                # before unwinding (ownership never reached the persistent
                # mapping on this path).
                _release_fh(witness_fh)
                witness_fh = None
            self._drop(req)
            raise

    def prepare_send(self, grant_id: str) -> bool:
        """Best-effort pre-send fence. ``False`` → do NOT send.

        Phase 3D / F8 contract: this is a *best-effort* fence that must be
        called immediately before the HTTP transport (Phase 4 wiring). It
        durably marks the grant as ``sent`` so a later crash can distinguish
        "died before send" (NOT_SENT) from "died around send" (UNKNOWN).

        It does NOT, and cannot, cancel an HTTP request that has already
        started when a new breaker trip occurs afterwards (TOCTOU between
        this call and ``urlopen`` is inherent to any preflight fence). Do
        not rely on it to abort in-flight requests.
        """
        if not self.config.enabled:
            return True
        try:
            with self._transaction(write=True) as st:
                self._refresh(st)
                rec = st["grants"].get(grant_id)
                if rec is None:
                    st["anomalies"]["unknown_grant"] = \
                        st["anomalies"].get("unknown_grant", 0) + 1
                    self._set_dirty()
                    logger.critical(
                        "global_rate_limiter: prepare_send for unknown "
                        "grant — blocked (fail-closed)")
                    return False
                if rec.get("outcome") is not None:
                    return False                     # already terminal
                if self._gid_epoch(grant_id) != st["epoch"]:
                    rec["outcome"] = "REVOKED"       # epoch fencing
                    self._set_dirty()
                    return False
                if rec.get("probe"):
                    probe = st["breaker"].get("probe")
                    if not isinstance(probe, dict) or \
                            probe.get("holder") != grant_id:
                        rec["outcome"] = "REVOKED"
                        self._set_dirty()
                        return False
                    if not probe.get("marker"):
                        # durable send-marker BEFORE urlopen (§3.1)
                        probe["marker"] = True
                        self._set_dirty()
                if not rec.get("sent"):
                    # durable per-grant send evidence BEFORE urlopen (F2):
                    # the transport gate is now passed, so a crash from here
                    # on is indeterminate (UNKNOWN), not NOT_SENT.
                    rec["sent"] = True
                    self._set_dirty()
                return True
        except LimiterUnavailableError as exc:
            logger.critical(
                "global_rate_limiter: prepare_send unavailable (%s) — "
                "send blocked (fail-closed)", exc.code)
            return False

    def record_result(self, grant_id: str, priority: str, outcome: str,
                      *, epoch: int | None = None,
                      http_status: int | None = None,
                      ret_code: int | None = None) -> str:
        """Record the attempt result exactly once (dedup by grant_id).

        Returns ``APPLIED | DUPLICATE | UNKNOWN_GRANT | STATE_UNAVAILABLE |
        DISABLED``. ``epoch`` is informational — the epoch embedded in
        ``grant_id`` is authoritative. An unknown grant can NEVER change
        breaker state (fail-closed accounting).

        Phase 3D / **F1**: a ``RATE_LIMIT`` from a STALE epoch is retained as
        terminal diagnostic evidence but never re-trips or re-escalates the
        breaker — a fenced epoch's episode is counted exactly once. Fresh
        (current-epoch) rate limits escalate the 5→30→120 ladder normally.

        Phase 3D / **F3**: persistence is retried a bounded number of times on
        a transient FAIL_CLOSED error WITHOUT re-sending HTTP. State
        persistence is atomic, so a failed attempt applied nothing and a retry
        can never double-count a breaker transition. The probe witness is
        released only AFTER the result is durable; on exhaustion the slot is
        left held (fail-closed) and ``STATE_UNAVAILABLE`` is returned.
        """
        if not self.config.enabled:
            return "DISABLED"
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        attempts = self.config.record_retry_attempts
        backoff = self.config.record_retry_backoff_s
        last_code = "unknown"
        for attempt in range(attempts):
            result_var = "STATE_UNAVAILABLE"
            try:
                with self._transaction(write=True) as st:
                    self._refresh(st)
                    rec = st["grants"].get(grant_id)
                    gid_epoch = self._gid_epoch(grant_id)
                    if rec is None:
                        st["anomalies"]["unknown_grant"] = \
                            st["anomalies"].get("unknown_grant", 0) + 1
                        self._set_dirty()
                        logger.critical(
                            "global_rate_limiter: result for unknown grant "
                            "— ignored (fail-closed)")
                        result_var = "UNKNOWN_GRANT"
                    elif rec.get("outcome") is not None:
                        result_var = "DUPLICATE"      # replay → no mutation
                    elif gid_epoch is None:
                        st["anomalies"]["unknown_grant"] = \
                            st["anomalies"].get("unknown_grant", 0) + 1
                        self._set_dirty()
                        result_var = "UNKNOWN_GRANT"
                    else:
                        rec["outcome"] = outcome      # ISSUED → TERMINAL (×1)
                        self._set_dirty()
                        br = st["breaker"]
                        probe = br.get("probe")
                        owns_slot = isinstance(probe, dict) and \
                            probe.get("holder") == grant_id

                        if outcome == OUTCOME_RATE_LIMIT:
                            if gid_epoch != st["epoch"]:
                                # F1: stale-epoch rate limit — terminal
                                # diagnostic evidence only; the fenced
                                # epoch's episode already escalated once.
                                logger.warning(
                                    "global_rate_limiter: stale-epoch rate "
                                    "limit (gid=%s, epoch=%s != %s) — "
                                    "terminal only, no re-trip",
                                    grant_id, gid_epoch, st["epoch"])
                            else:
                                # Fresh negative signal from the CURRENT
                                # epoch: trip once (dedup'd above).
                                self._apply_open(st, self.clock.wall(),
                                                 bump_level=True)
                                logger.warning(
                                    "global_rate_limiter: rate limit "
                                    "recorded (gid=%s) — breaker OPEN "
                                    "level=%s", grant_id,
                                    st["breaker"]["level"])
                        elif outcome == OUTCOME_OK:
                            if gid_epoch != st["epoch"]:
                                # Stale success from a fenced epoch:
                                # terminal only — never advances streak.
                                pass
                            else:
                                if owns_slot:
                                    probe.clear()
                                    br["probe"] = None
                                    self._set_dirty()
                                br["success_streak"] += 1
                                self._set_dirty()
                                target = \
                                    self.config.recovery_success_target
                                if br["state"] == BKR_RECOVERY and \
                                        br["success_streak"] >= target:
                                    br["state"] = BKR_CLOSED
                                    br["level"] = 0
                                    br["success_streak"] = 0
                                    self._set_dirty()
                        else:
                            # TIMEOUT/AUTH/ERROR/NOT_SENT/REVOKED — not a
                            # success; free our probe slot if we held it.
                            if owns_slot:
                                probe.clear()
                                br["probe"] = None
                                self._set_dirty()
                        result_var = "APPLIED"
                # ---- result is DURABLE here (transaction persisted) ----
                # Release our local probe witness only AFTER persistence (F3):
                # on a write failure we never reach here, so the slot stays
                # held (fail-closed) across the retry.
                self._release_witness(grant_id)
                return result_var
            except LimiterUnavailableError as exc:
                last_code = exc.code
                # Bounded retry (F3): persistence is atomic, so a failed
                # attempt applied nothing — re-running cannot double-count a
                # breaker transition and never re-sends HTTP.
                if attempt + 1 < attempts:
                    if backoff > 0:
                        time.sleep(backoff * (attempt + 1))
                    continue
        logger.critical(
            "global_rate_limiter: record_result unavailable (%s) after %d "
            "attempt(s) — result NOT applied (fail-closed)",
            last_code, attempts)
        return "STATE_UNAVAILABLE"

    def refresh(self) -> dict[str, Any]:
        """Run one persisted refresh (maintenance) and return a snapshot."""
        if not self.config.enabled:
            return {"enabled": False}
        try:
            with self._transaction(write=True) as st:
                self._refresh(st)
        except LimiterUnavailableError as exc:
            return {"enabled": True, "available": False, "error": exc.code}
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Read-only view of the shared state (observability/tests)."""
        if not self.config.enabled:
            return {"enabled": False}
        try:
            with self._transaction(write=False) as st:
                br = st["breaker"]
                return {
                    "enabled": True,
                    "available": True,
                    "state": br["state"],
                    "level": br["level"],
                    "epoch": st["epoch"],
                    "success_streak": br["success_streak"],
                    "cooldown_until_wall": br["cooldown_until_wall"],
                    "unknown_count": br.get("unknown_count", 0),
                    "probe": json.loads(json.dumps(br.get("probe"))),
                    "tokens": st["tokens"],
                    "total_grants": st["total_grants"],
                    "epoch_grants": st["epoch_grants"],
                    "refill_total": st["refill_total"],
                    "usage_by_priority": dict(st["usage_by_priority"]),
                    "pending_count": len(st["pending"]),
                    "grants_count": len(st["grants"]),
                    "anomalies": dict(st["anomalies"]),
                    "shares": self.config.shares,
                }
        except LimiterUnavailableError as exc:
            return {"enabled": True, "available": False, "error": exc.code}

    # ------------------------------------------------------------------ tests
    def grant_outcome(self, grant_id: str) -> str | None:
        """Terminal outcome of a grant (None = ISSUED), or 'ABSENT'."""
        if not self.config.enabled:
            return "DISABLED"
        try:
            with self._transaction(write=False) as st:
                rec = st["grants"].get(grant_id)
                if rec is None:
                    return "ABSENT"
                return rec.get("outcome")
        except LimiterUnavailableError as exc:
            return f"UNAVAILABLE:{exc.code}"

    # ---------------------------------------------------------------- cleanup
    def prune_witness_files(self, *, max_files: int = 64) -> int:
        """Safe maintenance: unlink witness files that are provably inert.

        Phase 3D/F6 + Phase 3E TOCTOU fix. Normal operation NEVER unlinks a
        witness file; growth is bounded by invoking this contract fromops/Phase-4 tooling.

        A witness file is removed only when ALL hold:
          * its grant record is terminal (the probe is resolved), and
          * we hold its exclusive lock CONTINUOUSLY from the "no live
            holder" proof THROUGH the unlink — closing the prove→delete
            TOCTOU so the proof is still valid at removal time, and
          * it is opened WITHOUT create, so this path can never mint a
            second lock inode for the same grant, and
          * its gid is never reused (monotonic ``seq``), so no future probe
            needs this name.

        On platforms where unlink-while-lock-held is not expressible
        (Windows: builtin ``open()`` lacks ``FILE_SHARE_DELETE``, so
        ``os.remove`` on an open file fails) cleanup is DISABLED fail-safe
        and files are retained. Returns files removed.
        """
        if not self.config.enabled:
            return 0
        if os.name != "posix":
            # Fail-safe disable (Phase 3E req3): the prove→unlink window
            # cannot be closed here — os.remove on an open file fails and
            # there is no safe alternative. Keep the files; bound growth by
            # running cleanup on POSIX.
            logger.warning(
                "global_rate_limiter: witness cleanup disabled on this "
                "platform (cannot unlink-while-lock safely) — files retained")
            return 0
        directory = os.path.dirname(self.state_path) or "."
        prefix = os.path.basename(self.state_path) + "."
        suffix = ".witness"
        # Collect the terminal gids under a shared (read-only) transaction.
        terminal: set[str] = set()
        try:
            with self._transaction(write=False) as st:
                for gid, rec in st["grants"].items():
                    if isinstance(rec, dict) and rec.get("outcome") is not None:
                        terminal.add(str(gid))
        except LimiterUnavailableError:
            return 0
        try:
            names = os.listdir(directory)
        except OSError:
            return 0
        removed = 0
        for name in names:
            if removed >= max_files:
                break
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            gid = name[len(prefix):-len(suffix)].replace("-", ":")
            if gid not in terminal:
                continue                      # unresolved/unknown → keep
            path = os.path.join(directory, name)
            # Open WITHOUT create: a prune must never mint a new inode for
            # this path (that is exactly how two lock inodes for one grant
            # would appear when racing another pruner's unlink).
            try:
                fh = open(path, "r+b")
            except OSError:
                continue                      # already gone → nothing to do
            try:
                _try_lock(fh)                 # exclusive; raises if any holder
            except OSError:
                try:
                    fh.close()
                except OSError:
                    pass
                continue                      # live holder → fail-safe keep
            try:
                # Unlink WHILE STILL HOLDING the exclusive lock: the "no live
                # holder" proof stays valid through the removal, closing the
                # prove→unlink TOCTOU (Phase 3E). POSIX-only (guarded above).
                os.remove(path)
                removed += 1
            except OSError:
                pass                          # raced another pruner (gone)
            finally:
                try:
                    _unlock(fh)
                except OSError:
                    pass
                try:
                    fh.close()
                except OSError:
                    pass
        return removed


__all__ = [
    "GlobalBybitRateLimiter",
    "LimiterConfig",
    "GrantOutcome",
    "GrantResult",
    "LimiterUnavailableError",
    "SystemClock",
    "current_boot_id",
    "PRIORITY_ORDER",
    "VALID_OUTCOMES",
    "DEFAULT_STATE_PATH",
]
