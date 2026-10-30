"""BYBIT_RETRY_BUDGET_HARDENING_V1 — unified per-logical-request retry budget.

Issue #229 (Track B). This module owns the retry GOVERNANCE primitives shared
by every Bybit HTTP consumer in this checkout:

* :data:`HARD_MAX_ATTEMPTS` — the single attempt budget: **at most 3 HTTP
  attempts per logical request**, regardless of how many layers sit above the
  transport. There are no independent nested retry loops anywhere
  (``L3 × (L1+L2)`` amplification is removed);
* explicit outcome classification — ``TIMEOUT`` and ``RATE_LIMIT`` are the
  ONLY retryable classes; ``API_ERROR`` (non-rate-limit retCode),
  ``ACCESS_DENIED`` (HTTP 403 / "access too frequent"), plain ``HTTP_ERROR``,
  payload errors and unhandled transport errors are **fail-closed** (no
  retry, the identical exception object propagates);
* per-priority rate-limit retry caps (plan §4.4: ``P0 3 / P1 2 / P2 2 /
  P3 1``) truncated by the unified budget — the hard ≤3 bound always wins;
* exponential backoff with bounded, non-decreasing delays; jitter is applied
  on the rate-limit class (the class that lands inside throttled bursts and
  drives the positive feedback / retry storm), while the timeout schedule is
  kept exact because the frozen scanner regression
  (``tests/test_scanner_runtime.py::test_bybit_timeout_retries_with_linear_
  backoff``) asserts its sleep values verbatim (see BackoffSchedule docs);
* a process-local rate-limit breaker with cooldown gating (stands in for the
  shared cooldown of ``GLOBAL_BYBIT_RATE_LIMITER_V1`` until it lands):
  with an open cooldown ``P3`` defers the cycle, ``P0/P1/P2`` wait (bounded),
  and every retry may only leave after the cooldown;
* a bounded global retry share per sliding window — the storm guard;
* :class:`OrderSubmitGuard` — POST ``/v5/order/create`` double-submit
  protection (fail-closed on an indeterminate submission outcome) with a
  durable cross-process journal, owner-liveness witnesses and atomic
  crash-restart recovery (review #5710398920: orphaned ``in_flight``
  records become reconciliation-visible ``unknown`` records, never an
  automatic unlock).

All state is process-local and observational side-effect free. The retry
accounting fields (``attempt_index``, ``retry_reason``, ``total_attempts``)
are emitted to ``BYBIT_API_TELEMETRY_V1`` by the transport (Issue #228
parity: exactly one ``BYBIT_API_CALL`` record per HTTP attempt).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import weakref
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Callable

#: The unified attempt budget: at most 3 HTTP attempts per logical request.
#: ``bybit_max_attempts`` can lower it but can never raise it (Issue #229
#: acceptance criterion 1).
HARD_MAX_ATTEMPTS = 3

# ----------------------------------------------------------------- classes
CLASS_OK = "OK"
CLASS_TIMEOUT = "TIMEOUT"
CLASS_RATE_LIMIT = "RATE_LIMIT"
CLASS_API_ERROR = "API_ERROR"
CLASS_ACCESS_DENIED = "ACCESS_DENIED"
CLASS_HTTP_ERROR = "HTTP_ERROR"
CLASS_PAYLOAD_ERROR = "PAYLOAD_ERROR"
CLASS_TRANSPORT_ERROR = "TRANSPORT_ERROR"

#: The only retryable outcome classes. Everything else is fail-closed.
RETRYABLE_CLASSES = (CLASS_TIMEOUT, CLASS_RATE_LIMIT)

# ---------------------------------------------------------------- priority
PRIORITY_P0 = "P0"
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITIES = (PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, PRIORITY_P3)

#: Per-class rate-limit retry caps (plan §4.4: P0 3 / P1 2 / P2 2 / P3 1).
#: The unified <= 3 attempt budget truncates these further (a request can
#: never exceed :data:`HARD_MAX_ATTEMPTS` HTTP attempts in total).
RL_RETRY_CAPS: dict[str, int] = {
    PRIORITY_P0: 3,
    PRIORITY_P1: 2,
    PRIORITY_P2: 2,
    PRIORITY_P3: 1,
}

#: Endpoint-derived default priorities (first prefix match wins).
_ENDPOINT_PRIORITY: tuple[tuple[str, str], ...] = (
    ("/v5/order", PRIORITY_P0),
    ("/v5/position", PRIORITY_P0),
    ("/v5/market/kline", PRIORITY_P1),
    ("/v5/market/tickers", PRIORITY_P1),
    ("/v5/market/time", PRIORITY_P1),
    ("/v5/market", PRIORITY_P2),
)
DEFAULT_PRIORITY = PRIORITY_P2


def resolve_priority(explicit: str | None, endpoint: str) -> str:
    """Return the request priority: explicit override wins, then endpoint."""
    if explicit is not None:
        if explicit not in PRIORITIES:
            raise ValueError(f"unknown retry priority {explicit!r}")
        return explicit
    for prefix, priority in _ENDPOINT_PRIORITY:
        if endpoint.startswith(prefix):
            return priority
    return DEFAULT_PRIORITY


def rl_retry_cap(priority: str) -> int:
    """Rate-limit retry cap for a priority class (still budget-truncated)."""
    return RL_RETRY_CAPS.get(priority, RL_RETRY_CAPS[DEFAULT_PRIORITY])


# ------------------------------------------------------------ classification
_RATE_LIMIT_PHRASES = ("too many visits", "exceeded the api rate limit")
_ACCESS_DENIED_PHRASES = ("access too frequent",)


def is_rate_limit_result(ret_code: Any, ret_msg: Any) -> bool:
    """Bybit API result indicates a rate limit (10006 / known phrases)."""
    if ret_code == 10006:
        return True
    lowered = str(ret_msg or "").lower()
    return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)


def is_access_denied_result(ret_msg: Any) -> bool:
    """Bybit API result indicates access throttling that must NOT be retried.

    ``403`` / "access too frequent" is fail-closed by contract (Issue #229
    scope: classification table) — retrying it is exactly the retry-storm
    positive feedback the issue removes.
    """
    lowered = str(ret_msg or "").lower()
    return any(phrase in lowered for phrase in _ACCESS_DENIED_PHRASES)


def classify_api_result(ret_code: Any, ret_msg: Any) -> str:
    """Classify a parsed Bybit result envelope (retCode / retMsg).

    ``ACCESS_DENIED`` takes precedence over the rate-limit class: a
    "access too frequent" answer is fail-closed by contract even when the
    retCode looks like a rate limit (Issue #229 classification table).
    """
    if is_access_denied_result(ret_msg):
        return CLASS_ACCESS_DENIED
    if is_rate_limit_result(ret_code, ret_msg):
        return CLASS_RATE_LIMIT
    if ret_code != 0:
        return CLASS_API_ERROR
    return CLASS_OK


def is_retryable(classification: str) -> bool:
    return classification in RETRYABLE_CLASSES


# ----------------------------------------------------------- setting access
def int_setting(settings: Any, name: str, default: int) -> int:
    """Read an int setting defensively (mock/plain objects fall back)."""
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return int(default)


def float_setting(settings: Any, name: str, default: float) -> float:
    """Read a float setting defensively (mock/plain objects fall back)."""
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return float(default)


def str_setting(settings: Any, name: str, default: str) -> str:
    """Read a str setting defensively (mock/plain objects fall back)."""
    try:
        value = getattr(settings, name, None)
    except Exception:
        return str(default)
    return str(default) if value is None else str(value)


# ---------------------------------------------------------------- backoff
class BackoffSchedule:
    """Bounded, non-decreasing retry backoff for one logical request.

    * rate-limit class: exponential + jitter — ``base * 2**(n-1)`` plus a
      uniform jitter of up to 50% of that value (de-synchronizes the retry
      bursts that used to land inside throttled windows);
    * timeout class: the same exponential curve without jitter — the two
      schedules (old linear ``base * attempt`` and exponential
      ``base * 2**(attempt-1)``) are identical within the ≤3 attempt budget
      (at most two retries: ``[b, 2b]``), so the frozen scanner regression
      sleep contract ``[1, 2]`` is preserved bit-for-bit while the schedule
      is uniformly exponential (Issue #229 acceptance criterion 3).

    Every returned delay is floored at the previous one (non-decreasing per
    sequence) and capped at ``max_delay_s`` (bounded).
    """

    def __init__(
        self,
        timeout_base_s: float,
        rate_limit_base_s: float,
        *,
        max_delay_s: float = 30.0,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.timeout_base_s = max(0.0, float(timeout_base_s))
        self.rate_limit_base_s = max(0.0, float(rate_limit_base_s))
        self.max_delay_s = max(0.0, float(max_delay_s))
        self._jitter = jitter or (lambda span: random.uniform(0.0, span))
        self._last_delay = 0.0

    def _floor_and_bound(self, delay: float) -> float:
        delay = max(delay, self._last_delay)
        delay = min(delay, self.max_delay_s)
        self._last_delay = delay
        return delay

    def next_delay(self, classification: str, attempt_number: int) -> float:
        """Delay before retrying after the n-th (1-based) retryable failure."""
        n = max(1, int(attempt_number))
        if classification == CLASS_RATE_LIMIT:
            base = self.rate_limit_base_s * (2 ** (n - 1))
            delay = base + self._jitter(base * 0.5)
        else:
            base = self.timeout_base_s * (2 ** (n - 1))
            delay = base
        return self._floor_and_bound(delay)


# ----------------------------------------------------------- storm guard
class RetryStormGuard:
    """Bounded global retry share per sliding window (storm protection).

    Retries inside a window are capped by an absolute count AND — once the
    window holds a meaningful sample — by a share of the issued attempts, so
    a throttled burst can never be amplified by a retry storm. Denied
    retries are fail-closed by the caller.
    """

    def __init__(
        self,
        window_s: float = 60.0,
        max_retries: int = 30,
        max_share: float = 0.5,
        min_sample: int = 10,
    ) -> None:
        self.window_s = max(0.001, float(window_s))
        self.max_retries = max(0, int(max_retries))
        self.max_share = min(1.0, max(0.0, float(max_share)))
        self.min_sample = max(1, int(min_sample))
        self._lock = threading.Lock()
        self._attempts: deque[float] = deque()
        self._retries: deque[float] = deque()

    def configure(
        self,
        *,
        window_s: float | None = None,
        max_retries: int | None = None,
        max_share: float | None = None,
        min_sample: int | None = None,
    ) -> None:
        """Apply runtime (settings) caps; process-global, idempotent."""
        with self._lock:
            if window_s is not None and window_s > 0:
                self.window_s = float(window_s)
            if max_retries is not None and max_retries >= 0:
                self.max_retries = int(max_retries)
            if max_share is not None and 0 <= max_share <= 1:
                self.max_share = float(max_share)
            if min_sample is not None and min_sample >= 1:
                self.min_sample = int(min_sample)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._attempts and self._attempts[0] <= cutoff:
            self._attempts.popleft()
        while self._retries and self._retries[0] <= cutoff:
            self._retries.popleft()

    def note_attempt(self, *, is_retry: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._attempts.append(now)
            if is_retry:
                self._retries.append(now)

    def allow_retry(self) -> bool:
        """True when one more retry fits the window caps."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            attempts = len(self._attempts)
            retries = len(self._retries)
            if retries + 1 > self.max_retries:
                return False
            if attempts < self.min_sample:
                return True
            return (retries + 1) <= self.max_share * (attempts + 1)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return {
                "attempts": len(self._attempts),
                "retries": len(self._retries),
                "window_s": self.window_s,
                "max_retries": self.max_retries,
                "max_share": self.max_share,
            }

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()
            self._retries.clear()


# ------------------------------------------------------------- breaker
class RateLimitBreaker:
    """Process-local rate-limit breaker with cooldown-aware retry gating.

    Until ``GLOBAL_BYBIT_RATE_LIMITER_V1`` lands (shared cooldown), this
    process-local breaker is the gating authority (Issue #229 scope:
    "breaker-aware retry gating"). Policy with an open cooldown:

    * ``P3`` — the cycle DEFERS immediately (fail-closed, no HTTP send);
    * ``P0``/``P1``/``P2`` — bounded wait for the cooldown; a retry may only
      leave after the cooldown, and when the bounded wait cannot cover the
      remaining cooldown the request fails closed instead of piling on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cooldown_until = 0.0

    def trip(self, cooldown_s: float) -> None:
        until = time.monotonic() + max(0.0, float(cooldown_s))
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def is_open(self) -> bool:
        return self.remaining() > 0

    def gate(
        self,
        priority: str,
        *,
        max_wait_s: float,
        sleep: Callable[[float], None],
    ) -> None:
        """Fail-closed gate applied before every HTTP attempt."""
        remaining = self.remaining()
        if remaining <= 0:
            return
        if priority == PRIORITY_P3:
            raise_bybit_rate_limit_deferred(remaining)
        wait = min(remaining, max(0.0, float(max_wait_s)))
        if wait > 0:
            sleep(wait)
        left = self.remaining()
        if left > 0 and wait < remaining:
            # bounded wait could not cover the cooldown: fail closed
            raise_bybit_rate_limit_deferred(left)

    def reset(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0


def raise_bybit_rate_limit_deferred(remaining_s: float) -> None:
    """Raise the fail-closed deferral error (import avoids a cycle)."""
    from app.exchange.bybit_client import BybitRateLimitError

    raise BybitRateLimitError(
        f"request deferred: rate-limit cooldown open ({remaining_s:.2f}s remaining)"
    )


# ------------------------------------------------------- order submit guard
# Persistent journal for the double-submit guard (code reviews #5709376849,
# #5709889450): submission state must survive process restarts and must be
# safe across PROCESSES, so every transition is serialized by a dedicated
# cross-process lock file and durably persisted with an integrity checksum.
ORDER_JOURNAL_SCHEMA = 2
ORDER_JOURNAL_DEFAULT_PATH = "var/reports/bybit_order_submit_journal_v1.json"
ORDER_LOCK_TIMEOUT_S = 10.0

_ORDER_ACTIVE_STATES = ("in_flight", "unknown")
_ORDER_ALL_STATES = ("in_flight", "unknown", "resolved")
_logger = logging.getLogger(__name__)

# ------------------------------------------------------- owner liveness
# Review #5710398920 (R3): an ``in_flight`` record of a DEAD submitting
# owner must become reconciliation-visible WITHOUT ever auto-unlocking,
# while an ``in_flight`` record of a LIVE owner must stay untouched.
#
# Ownership model (process/owner liveness — NOT age, NOT TTL):
#
# * every journal entry created by :class:`OrderSubmitGuard` records an
#   ``owner`` block: unique ``owner_id`` (uuid4 hex), the OS ``pid`` for
#   forensics and the name of a per-owner WITNESS lock file;
# * while the owning guard instance is alive it holds an OS-level
#   byte-range lock on its witness file (``msvcrt.locking`` /
#   ``fcntl.flock``). The OS releases that lock when the process (or the
#   handle) dies — so liveness probing is bound to an OS object and is
#   IMMUNE TO PID REUSE: a recycled PID can never re-hold a released
#   witness lock;
# * verdicts (:data:`LIVENESS_ALIVE` / :data:`LIVENESS_ORPHANED` /
#   :data:`LIVENESS_UNVERIFIED`) are produced under the journal lock only;
#   when liveness cannot be established the record stays blocking and the
#   caller receives an explicit :data:`ORDER_RESOLUTION_OWNER_UNVERIFIED`
#   blocker (fail-closed).
LIVENESS_ALIVE = "alive"
LIVENESS_ORPHANED = "orphaned"
LIVENESS_UNVERIFIED = "unverified"

#: Reconciliation result labels for owner-liveness verdicts.
ORDER_RESOLUTION_OWNER_UNVERIFIED = "OWNER_UNVERIFIED_BLOCKER"
ORDER_RESOLUTION_OWNER_ALIVE = "OWNER_ALIVE_BLOCKER"

#: Owner ids / witness file names are machine-generated and strictly
#: validated (no path separators — a witness name is a bare file name).
_OWNER_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _order_uncertain_error(message: str) -> BaseException:
    """Build the fail-closed order error (import avoids a cycle)."""
    from app.exchange.bybit_client import BybitOrderUncertainError

    return BybitOrderUncertainError(message)


class ManualResolutionNotImplemented(RuntimeError):
    """Manual UNKNOWN-unlock is deliberately NOT implemented.

    Review #5709889450 (P2): an arbitrary CLI string is not evidence. A
    manual resolution would require verified operator authorization plus a
    verifiable confirmation record; neither exists yet, so the honest
    behavior is to keep the ``UNKNOWN`` block (fail-closed) instead of
    inventing a fake authorization system.
    """


def _lock_handle(fh: Any) -> None:
    """Acquire the 1-byte OS lock at offset 0 of an open file (non-blocking).

    Windows: ``msvcrt.locking`` byte-range lock; POSIX: ``fcntl.flock``.
    Both are OS-level locks released automatically when the owning process
    (or the file handle) dies. Raises ``OSError`` when the range is held.
    """
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(fh: Any) -> None:
    """Release the 1-byte OS lock taken by :func:`_lock_handle`."""
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


#: Errnos that mean "the range is locked by someone else" (as opposed to a
#: broken file / permission problem). Windows ``msvcrt.locking`` reports a
#: lock violation as ``PermissionError`` (errno 13); POSIX ``flock(LOCK_NB)``
#: reports ``BlockingIOError`` (EAGAIN/EWOULDBLOCK).
_LOCK_CONFLICT_ERRNOS = frozenset(
    x for x in (getattr(errno, "EACCES", None),
                getattr(errno, "EAGAIN", None),
                getattr(errno, "EWOULDBLOCK", None),
                getattr(errno, "EDEADLK", None)) if x is not None)


def _is_lock_conflict(exc: OSError) -> bool:
    """True when ``exc`` means 'lock held', not 'file unusable'."""
    return (isinstance(exc, BlockingIOError)
            or getattr(exc, "errno", None) in _LOCK_CONFLICT_ERRNOS)


class _JournalFileLock:
    """Cross-process exclusive lock on a DEDICATED lock file.

    Review #5709889450 (P1): ``threading.RLock`` only protects threads of
    one process. Two processes sharing one journal must serialize
    ``LOCK → RELOAD → VALIDATE → RESERVE → DURABLE WRITE → UNLOCK``.

    * the lock file is a separate file next to the journal — the journal
      itself is replaced via ``os.replace`` and its inode must never be the
      lock target;
    * the lock file is NEVER unlinked (deleting a lock file while another
      process holds it would break mutual exclusion on POSIX);
    * Windows: byte-range lock via ``msvcrt.locking``; POSIX: ``fcntl.flock``
      — both are OS-level locks released automatically if the process dies;
    * acquisition is bounded (``ORDER_LOCK_TIMEOUT_S``); on timeout the
      caller fails CLOSED without any HTTP attempt.
    """

    def __init__(self, lock_path: str, timeout_s: float) -> None:
        self._lock_path = str(lock_path)
        self._timeout_s = max(0.0, float(timeout_s))
        self._fh = None

    def __enter__(self) -> "_JournalFileLock":
        self._fh = open(self._lock_path, "a+")
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                _lock_handle(self._fh)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise _order_uncertain_error(
                        "order-submit journal lock unavailable "
                        f"(timeout {self._timeout_s:.1f}s) — submission "
                        "blocked (fail-closed, no send)")
                time.sleep(0.02)

    def __exit__(self, *exc: Any) -> bool:
        try:
            _unlock_handle(self._fh)
        finally:
            if self._fh is not None:
                self._fh.close()   # releases the OS lock; file stays on disk
                self._fh = None
        return False


def _fsync_dir(directory: str) -> None:
    """Best-effort directory fsync (POSIX only).

    Windows has no portable directory-fsync primitive available here, so no
    formal crash-durability claim is made on Windows — only atomic
    namespace replacement (``os.replace``) plus file-level fsync.
    """
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _OwnerWitness:
    """OS-lock liveness witness for one order-submit guard owner.

    The witness file is created next to the journal and is NEVER unlinked
    (deleting a lock file would break the probe). While the owning guard
    instance is alive it holds a byte-range OS lock on the witness; the OS
    releases the lock when the process/handle dies, so liveness probing is
    bound to an OS object and is immune to PID reuse (a recycled PID can
    never re-hold a released witness lock).
    """

    def __init__(self, path: str, owner_id: str) -> None:
        self.path = str(path)
        self.owner_id = str(owner_id)
        self.pid = os.getpid()
        self.started_at = _utc_now()
        self._fh = open(self.path, "a+")
        try:
            _lock_handle(self._fh)
        except OSError:
            self._fh.close()
            self._fh = None
            raise
        _LIVE_WITNESSES[self.owner_id] = self

    @property
    def released(self) -> bool:
        return self._fh is None

    def release(self) -> None:
        """Drop the OS lock (owner can never complete new work as 'owner')."""
        fh, self._fh = self._fh, None
        _LIVE_WITNESSES.pop(self.owner_id, None)
        if fh is not None:
            try:
                _unlock_handle(fh)
            except OSError:
                pass          # closing below releases the OS lock anyway
            try:
                fh.close()
            except OSError:
                pass

    def __del__(self) -> None:        # pragma: no cover — GC path
        try:
            self.release()
        except Exception:             # noqa: BLE001 — never raise in GC
            pass


#: Owners with a live witness IN THIS PROCESS (weak: entries disappear when
#: the witness/guard is destroyed). Probing a registered owner means the
#: owner is provably alive without touching the OS lock at all.
_LIVE_WITNESSES: "weakref.WeakValueDictionary[str, _OwnerWitness]" = \
    weakref.WeakValueDictionary()


def verify_owner_witness(witness_path: str) -> str:
    """Probe ONE owner witness and return its liveness verdict.

    * :data:`LIVENESS_ORPHANED` — the witness lock is ACQUIRABLE, i.e. the
      OS released it: the owning owner is provably gone;
    * :data:`LIVENESS_ALIVE` — the lock is held by a live owner;
    * :data:`LIVENESS_UNVERIFIED` — the witness is missing/unusable and
      liveness cannot be established (the caller must FAIL CLOSED).

    No age/TTL heuristics are used: the verdict comes exclusively from the
    OS lock state (review #5710398920). The probe opens the witness WITHOUT
    ``O_CREAT``: a missing/deleted witness is never re-created (that would
    probe a fresh inode and could misjudge a live owner) — it is
    :data:`LIVENESS_UNVERIFIED`, fail-closed.
    """
    try:
        fd = os.open(witness_path, os.O_RDWR)
    except OSError:
        return LIVENESS_UNVERIFIED
    try:
        fh = os.fdopen(fd, "r+b")
    except (OSError, ValueError):
        os.close(fd)
        return LIVENESS_UNVERIFIED
    try:
        try:
            _lock_handle(fh)
        except OSError as exc:
            if _is_lock_conflict(exc):
                return LIVENESS_ALIVE
            return LIVENESS_UNVERIFIED
        try:
            _unlock_handle(fh)
        except OSError:
            pass
        return LIVENESS_ORPHANED
    finally:
        try:
            fh.close()
        except OSError:
            pass


class OrderSubmitGuard:
    """Persistent fail-closed double-submit guard for POST ``/v5/order/create``.

    Code reviews #5709376849 / #5709889450 / #5710398920:

    * an UNKNOWN outcome (timeout / transport failure / 5xx / unparseable
      body — the order may exist server-side) is **never released by a
      timer** and **never by an unverified manual action**; the legacy
      ``bybit_order_resubmit_block_s`` window is kept for config rollback
      compatibility only and unblocks nothing;
    * the block is lifted only by definitive evidence: a parsed API result
      of the submission itself (including a rejection), an HTTP 4xx
      rejection, or a STRICT exchange-confirmed reconciliation (exact
      ``orderLinkId`` + symbol + category + accepted order status);
    * every transition is serialized across processes with a dedicated
      journal lock file and follows
      ``LOCK → RELOAD → VALIDATE → RECOVERY TRANSITION → DURABLE WRITE →
      UNLOCK``; a POST is sent only after the durable write is confirmed;
    * the journal is schema-2 with a checksum and strict validation;
      a corrupted / unverifiable journal FAILS CLOSED and is never silently
      reset to an empty one;
    * durable write: unique temp file in the same directory, controlled
      permissions, ``flush`` + ``fsync`` + atomic ``os.replace`` +
      directory fsync (POSIX; Windows has no directory fsync — no formal
      crash-durability claim there); the checksum is recomputed on every
      change;
    * crash recovery (review #5710398920): each record owns a liveness
      WITNESS (OS byte-range lock — immune to PID reuse). A record left
      ``in_flight`` by an owner that is PROVABLY gone is transitioned to
      ``unknown`` (still blocking, now reconciliation-visible) by a DURABLE
      recovery transition inside the standard transaction — never in memory
      only, so the recovered state survives further restarts. A record whose
      owner liveness CANNOT be verified stays blocking and surfaces as
      ``OWNER_UNVERIFIED_BLOCKER``; a record whose owner is alive is never
      declared orphaned. The recovery transition is idempotent and never
      resolves, releases or rewrites foreign records.

    If the lock cannot be acquired or the journal cannot be read/written the
    guard FAILS CLOSED: the submission is refused with zero HTTP attempts.
    """

    def __init__(self, journal_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._journal_path: str | None = None
        self._load_error: str | None = None
        self._write_degraded = False
        self._entries: dict[str, dict[str, Any]] = {}
        self._witness: _OwnerWitness | None = None
        self._last_recovery: dict[str, Any] | None = None
        self._mutated = False
        if journal_path:
            self.configure(journal_path)

    def __del__(self) -> None:        # pragma: no cover — GC path
        try:
            self._release_witness()
        except Exception:             # noqa: BLE001 — never raise in GC
            pass

    # ------------------------------------------------------- ownership
    def _ensure_witness(self) -> _OwnerWitness:
        """Create (once) this owner's liveness witness for the journal.

        The witness is established BEFORE any record of this owner can reach
        the journal, so a record is never visible without a live witness.
        """
        with self._lock:
            if self._witness is not None and not self._witness.released:
                return self._witness
            if not self._journal_path:
                raise _order_uncertain_error(
                    "order-submit journal not configured — cannot establish "
                    "submission ownership (fail-closed, no send)")
            owner_id = uuid4().hex
            directory = os.path.dirname(self._journal_path) or "."
            name = (f"{os.path.basename(self._journal_path)}"
                    f".owner.{owner_id}.lock")
            try:
                os.makedirs(directory, exist_ok=True)
                self._witness = _OwnerWitness(
                    os.path.join(directory, name), owner_id)
            except OSError as exc:
                # Ownership cannot be established -> the guard can no longer
                # prove who may complete an operation: fail-closed exactly
                # like a journal write failure (no send, no further writes).
                self._write_degraded = True
                raise _order_uncertain_error(
                    "cannot establish order-submit ownership witness "
                    f"({type(exc).__name__}) — submission blocked "
                    "(fail-closed, no send)") from exc
            return self._witness

    def _release_witness(self) -> None:
        witness, self._witness = self._witness, None
        if witness is not None:
            witness.release()

    def _owner_identity(self) -> dict[str, Any]:
        witness = self._ensure_witness()
        return {
            "owner_id": witness.owner_id,
            "pid": witness.pid,
            "witness": os.path.basename(witness.path),
            "started_at": witness.started_at,
        }

    def _verify_owner(self, owner: Any) -> str:
        """Liveness verdict for ONE journal-record owner (lock held).

        Returns :data:`LIVENESS_ALIVE`, :data:`LIVENESS_ORPHANED` or
        :data:`LIVENESS_UNVERIFIED`. Owners of THIS process are alive by
        construction (live-witness registry); everything else is judged
        exclusively by the OS witness lock — never by age or TTL.
        """
        if not isinstance(owner, dict):
            return LIVENESS_UNVERIFIED
        owner_id = str(owner.get("owner_id", ""))
        witness_name = str(owner.get("witness", ""))
        if not _OWNER_ID_RE.match(owner_id):
            return LIVENESS_UNVERIFIED
        if (not witness_name or os.path.basename(witness_name) != witness_name):
            return LIVENESS_UNVERIFIED
        if owner_id in _LIVE_WITNESSES:
            return LIVENESS_ALIVE
        directory = os.path.dirname(self._journal_path or "") or "."
        return verify_owner_witness(os.path.join(directory, witness_name))

    # ------------------------------------------------------- persistence
    def configure(self, journal_path: str) -> None:
        """Bind the journal path and run restart recovery (durable).

        Recovery is NOT done in memory: orphaned ``in_flight`` records are
        transitioned through the standard journal transaction and durably
        persisted (review #5710398920 — the recovered state must survive
        further restarts and be visible to reconciliation). Records whose
        owner liveness cannot be verified stay blocking (fail-closed).
        """
        with self._lock:
            journal_path = str(journal_path)
            if journal_path == self._journal_path:
                return
            self._release_witness()
            self._journal_path = journal_path
            self._load_error = None
            self._write_degraded = False
            self._last_recovery = None
            try:
                self._entries = self._read_journal()
            except (OSError, ValueError) as exc:
                # Fail closed: a corrupted/unreadable journal cannot prove
                # anything — submissions are refused until it is restored.
                # It is NEVER silently reset to an empty journal.
                self._entries = {}
                self._load_error = (
                    f"order-submit journal unreadable/invalid "
                    f"({type(exc).__name__})")
                return
            try:
                self.recover_orphaned_in_flight()
            except Exception as exc:  # noqa: BLE001 — configure stays quiet
                # Restart recovery failed (lock / checksum / durable write):
                # fail-closed — submissions are refused, nothing is unlocked.
                self._load_error = (
                    f"order-submit restart recovery failed "
                    f"({type(exc).__name__})")

    def _read_journal(self) -> dict[str, dict[str, Any]]:
        """Read + VALIDATE the journal (checksum, schema, structure).

        Raises ``ValueError`` on any integrity violation — callers turn this
        into a fail-closed state. No state is rewritten here: crash recovery
        is a separate DURABLE transaction step (review #5710398920), never
        an in-memory conversion that the next reload would undo.
        """
        path = self._journal_path
        if not path or not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return _validate_journal_document(data)

    def _reload(self) -> None:
        """Reload the authoritative on-disk state under the journal lock."""
        try:
            self._entries = self._read_journal()
            self._load_error = None
        except (OSError, ValueError) as exc:
            self._load_error = (
                f"order-submit journal unreadable/invalid "
                f"({type(exc).__name__})")
            raise _order_uncertain_error(
                f"{self._load_error} — submissions are blocked (fail-closed)")

    def _document(self) -> dict[str, Any]:
        canonical = json.dumps(self._entries, sort_keys=True,
                               separators=(",", ":"))
        return {
            "schema": ORDER_JOURNAL_SCHEMA,
            "entries": self._entries,
            "checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    def _write(self) -> bool:
        """Durable atomic write; False on failure (caller goes fail-closed).

        unique temp file in the same directory → controlled permissions →
        serialized content → flush → fsync → atomic os.replace →
        directory fsync (POSIX).
        """
        path = self._journal_path
        if not path:
            return True
        directory = os.path.dirname(path) or "."
        tmp_path = f"{path}.tmp.{os.getpid()}.{uuid4().hex[:8]}"
        try:
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(self._document(), indent=1, sort_keys=True)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            _fsync_dir(directory)
            return True
        except (OSError, TypeError, ValueError) as exc:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            _logger.critical(
                "order-submit journal write failed (%s) — further order "
                "submissions will be refused (fail-closed)",
                type(exc).__name__)
            return False

    @contextmanager
    def _transaction(self, *, strict_write: bool):
        """LOCK → RELOAD → VALIDATE → RECOVERY → (mutate) → WRITE → UNLOCK.

        The RECOVERY step (review #5710398920) runs on EVERY journal
        transaction, so orphaned ``in_flight`` records become durable,
        reconciliation-visible state no matter which caller triggers the
        transaction. The durable write only runs when the transaction
        actually changed something (a recovery pass with nothing to recover
        is a pure read and creates/rewrites no files).
        """
        with self._lock:
            if self._load_error or self._write_degraded:
                raise _order_uncertain_error(
                    f"{self._load_error or 'order-submit journal write degraded'}"
                    " — submissions are blocked (fail-closed)")
            if not self._journal_path:
                # in-memory mode (no journal configured) is refused: the
                # guard only operates on a durable record
                raise _order_uncertain_error(
                    "order-submit journal not configured — submissions are "
                    "blocked (fail-closed)")
            with _JournalFileLock(
                    self._journal_path + ".lock",
                    float(globals()["ORDER_LOCK_TIMEOUT_S"])):
                self._reload()            # authoritative state of THIS moment
                self._mutated = False
                self._recover_locked()    # RECOVERY TRANSITION (durable below)
                yield
                if self._mutated and not self._write():
                    # Not durable -> no recovery/transition result may be
                    # reported (fail-closed, no half-truths).
                    self._last_recovery = None
                    self._write_degraded = True
                    if strict_write:
                        raise _order_uncertain_error(
                            "cannot persist order submission state — "
                            "submission blocked (fail-closed, no send)")

    # ------------------------------------------------------- recovery
    def _recover_locked(self) -> None:
        """RECOVERY TRANSITION step (journal lock held, state just reloaded).

        For every ``in_flight`` record whose owner is PROVABLY gone
        (:data:`LIVENESS_ORPHANED` — the OS released the owner's witness
        lock) the record is transitioned to ``unknown``: still blocking, but
        now visible to the reconciliation procedure. Records whose owner is
        alive are left untouched; records whose owner liveness CANNOT be
        verified keep their block (fail-closed) and are reported as
        ``OWNER_UNVERIFIED_BLOCKER`` candidates.

        ``orderLinkId``, ``fingerprint`` and every audit field are preserved
        and the checksum is recomputed by the durable write. The transition
        is idempotent: a recovered ``unknown`` record is never touched again.
        """
        report: dict[str, Any] = {"recovered": [], "owner_alive": [],
                                  "owner_unverified": []}
        for key, entry in list(self._entries.items()):
            if entry.get("state") != "in_flight":
                continue
            verdict = self._verify_owner(entry.get("owner"))
            if verdict == LIVENESS_ALIVE:
                report["owner_alive"].append(dict(entry))
                continue
            if verdict == LIVENESS_UNVERIFIED:
                report["owner_unverified"].append(dict(entry))
                continue
            recovered = dict(entry)          # preserve ALL audit fields
            recovered["state"] = "unknown"
            if not recovered.get("reason"):
                recovered["reason"] = (
                    "in_flight at crash/restart (outcome UNKNOWN)")
            recovered["recovery"] = {
                "from_state": "in_flight",
                "to_state": "unknown",
                "ts_utc": _utc_now(),
                "detail": ("owner not alive (witness lock released) — "
                           "outcome UNKNOWN, awaiting reconciliation"),
            }
            self._entries[key] = recovered
            self._mutated = True
            report["recovered"].append(dict(recovered))
            _logger.warning(
                "order-submit journal recovery: in_flight -> unknown "
                "(orderLinkId=%s, owner gone) — stays blocking until "
                "reconciliation",
                recovered.get("order_link_id", ""))
        self._last_recovery = report

    def recover_orphaned_in_flight(self) -> dict[str, Any]:
        """Run the durable restart-recovery transaction and report verdicts.

        ``LOCK → RELOAD → VALIDATE → RECOVERY TRANSITION → DURABLE WRITE →
        UNLOCK``; returns the recovery report of THIS pass:

        * ``recovered`` — orphaned ``in_flight`` records transitioned to
          ``unknown`` (durably, reconciliation-visible, still blocking);
        * ``owner_alive`` — ``in_flight`` records of provably LIVE owners
          (never declared orphaned, never touched);
        * ``owner_unverified`` — ``in_flight`` records whose owner liveness
          cannot be established (kept blocking →
          ``OWNER_UNVERIFIED_BLOCKER``).

        Fails closed (raises) on lock / checksum / durable-write errors.
        """
        with self._transaction(strict_write=True):
            pass
        return self._last_recovery or {"recovered": [], "owner_alive": [],
                                       "owner_unverified": []}

    def last_recovery_report(self) -> dict[str, Any] | None:
        """Report of the most recent recovery pass (observational)."""
        with self._lock:
            report = self._last_recovery
            return dict(report) if report is not None else None

    @staticmethod
    def _key(fingerprint: tuple, order_link_id: str) -> str:
        # Unique per submission attempt: the journal is an audit trail —
        # resolved records are preserved, never overwritten (review P1).
        return json.dumps([[str(part) for part in fingerprint],
                           str(order_link_id)], separators=(",", ":"))

    def _new_entry(self, fingerprint: tuple, order_link_id: str,
                   state: str, *, reason: str = "",
                   evidence: str = "") -> dict[str, Any]:
        return {
            "fingerprint": [str(part) for part in fingerprint],
            "order_link_id": str(order_link_id),
            "state": str(state),
            "reason": str(reason),
            "evidence": str(evidence),
            "ts_utc": _utc_now(),
            # Ownership/liveness witness (review #5710398920): proves which
            # owner may complete this operation and how its liveness is
            # verified after a crash.
            "owner": self._owner_identity(),
        }

    # ------------------------------------------------------- transitions
    def begin(self, fingerprint: tuple, *, order_link_id: str) -> bool:
        """Reserve the fingerprint for one submission attempt.

        Returns ``False`` while an identical submission is ``in_flight`` or
        its outcome is still ``unknown`` (no timer release, no unlauter
        manual release). Fails closed (raises, zero HTTP attempts) when the
        lock cannot be acquired or the journal is unusable.
        """
        with self._transaction(strict_write=True):
            fp_list = [str(part) for part in fingerprint]
            for entry in self._entries.values():
                if entry.get("state") not in _ORDER_ACTIVE_STATES:
                    continue
                # blocked by an identical submission OR an active owner of
                # this operation identity (orderLinkId collision)
                if (entry.get("fingerprint") == fp_list
                        or str(entry.get("order_link_id", ""))
                        == str(order_link_id)):
                    return False
            key = self._key(fingerprint, order_link_id)
            new_entry = self._new_entry(
                fingerprint, order_link_id, "in_flight")
            existing = self._entries.get(key)
            if existing is not None:
                # Audit-trail hardening (review #5710398920): a record of
                # ANOTHER owner under the same key must never be lost or
                # silently overwritten — it is preserved as history.
                history = list(existing.get("history") or [])
                prior = dict(existing)
                prior.pop("history", None)
                history.append(prior)
                new_entry["history"] = history
            self._entries[key] = new_entry
            self._mutated = True
            return True

    def mark_unknown(self, fingerprint: tuple, *, order_link_id: str,
                     reason: str) -> None:
        """Record an INDETERMINATE outcome (blocks until reconciliation)."""
        try:
            with self._transaction(strict_write=False):
                key = self._key(fingerprint, order_link_id)
                entry = self._entries.get(key) or self._new_entry(
                    fingerprint, order_link_id, "unknown")
                entry.update({
                    "order_link_id": str(order_link_id),
                    "state": "unknown",
                    "reason": str(reason),
                    "ts_utc": _utc_now(),
                })
                self._entries[key] = entry
                self._mutated = True
        except Exception:  # noqa: BLE001 — never mask the caller's error
            self._write_degraded = True
            _logger.critical(
                "order-submit journal transition failed — further order "
                "submissions will be refused (fail-closed)")

    def mark_resolved(self, fingerprint: tuple, *, order_link_id: str,
                      evidence: str) -> None:
        """Record a DEFINITIVE outcome of the submission itself."""
        try:
            with self._transaction(strict_write=False):
                key = self._key(fingerprint, order_link_id)
                entry = self._entries.get(key) or self._new_entry(
                    fingerprint, order_link_id, "resolved")
                entry.update({
                    "state": "resolved",
                    "evidence": str(evidence),
                    "ts_utc": _utc_now(),
                })
                self._entries[key] = entry
                self._mutated = True
        except Exception:  # noqa: BLE001 — never mask the caller's error
            self._write_degraded = True
            _logger.critical(
                "order-submit journal transition failed — further order "
                "submissions will be refused (fail-closed)")

    def resolve_reconciled(self, order_link_id: str, *, evidence: str) -> bool:
        """Lift the block with strict exchange-confirmed evidence.

        The ONLY automatic unlock path (review #5709889450 P2-A): the
        caller must have verified an exact ``orderLinkId`` match, symbol,
        category and an accepted order status; the verifiable confirmation
        record is stored as evidence. Never time-based.
        """
        with self._transaction(strict_write=True):
            for entry in self._entries.values():
                if (str(entry.get("order_link_id", "")) == str(order_link_id)
                        and entry.get("state") in _ORDER_ACTIVE_STATES):
                    entry["state"] = "resolved"
                    entry["evidence"] = str(evidence)
                    entry["ts_utc"] = _utc_now()
                    self._mutated = True
                    return True
            return False

    def resolve_manual(self, **_fields: Any) -> None:
        """Manual UNKNOWN-unlock: NOT IMPLEMENTED — the block is kept.

        Review #5709889450 (P2-B): a manual resolution needs verified
        operator authorization and a verifiable confirmation record
        (operator identity, confirmation reference, orderLinkId, symbol,
        status-check result, UTC timestamp, reason). Without a real
        authorization mechanism this stays fail-closed instead of trusting
        free-form strings.
        """
        raise ManualResolutionNotImplemented(
            "MANUAL_RESOLUTION_NOT_IMPLEMENTED — manual unlock of an UNKNOWN "
            "order submission requires a verified operator authorization "
            "mechanism, which is deliberately not implemented; the block is "
            "kept (fail-closed)")

    # ------------------------------------------------------- inspection
    def blocking_entry(self, fingerprint: tuple) -> tuple[str | None, str]:
        """Return ``(state, order_link_id)`` of the active blocking entry."""
        fp_list = [str(part) for part in fingerprint]
        with self._lock:
            for entry in self._entries.values():
                if (entry.get("state") in _ORDER_ACTIVE_STATES
                        and entry.get("fingerprint") == fp_list):
                    return (str(entry.get("state")),
                            str(entry.get("order_link_id", "")))
            return None, ""

    def pending_unknown(self) -> list[dict[str, Any]]:
        """Entries awaiting reconciliation (state == ``unknown``)."""
        with self._lock:
            return [dict(entry) for entry in self._entries.values()
                    if entry.get("state") == "unknown"]

    def active_unknown(self) -> list[dict[str, Any]]:
        """All entries blocking a re-submission (in_flight + unknown)."""
        with self._lock:
            return [dict(entry) for entry in self._entries.values()
                    if entry.get("state") in _ORDER_ACTIVE_STATES]

    def reset(self) -> None:
        """In-memory reset (tests / process teardown).

        The journal file and the witness files are the durable record and
        are deliberately NOT deleted here — restart recovery depends on
        them. The owner witness lock is released, so records this guard can
        never complete are correctly seen as orphaned by a later recovery.
        """
        with self._lock:
            self._release_witness()
            self._entries = {}
            self._load_error = None
            self._write_degraded = False
            self._last_recovery = None
            self._journal_path = None


def _validate_journal_document(data: Any) -> dict[str, dict[str, Any]]:
    """Strict integrity validation of a journal document.

    Schema, structure, allowed states, required fields and checksum must all
    verify — anything else raises ``ValueError`` and the guard FAILS CLOSED
    (a corrupted journal is never silently treated as an empty one).
    """
    if not isinstance(data, dict):
        raise ValueError("journal document must be an object")
    if data.get("schema") != ORDER_JOURNAL_SCHEMA:
        raise ValueError("journal schema mismatch")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("journal entries must be an object")
    checksum = data.get("checksum")
    if not isinstance(checksum, str) or not checksum:
        raise ValueError("journal checksum missing")
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    if not hashlib.sha256(canonical.encode("utf-8")).hexdigest() == checksum:
        raise ValueError("journal checksum mismatch")
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("journal entry must be an object")
        fingerprint = entry.get("fingerprint")
        if (not isinstance(fingerprint, list)
                or any(not isinstance(part, str) for part in fingerprint)):
            raise ValueError("journal entry fingerprint must be str[]")
        if not isinstance(entry.get("order_link_id"), str) or \
                not entry.get("order_link_id"):
            raise ValueError("journal entry order_link_id must be non-empty")
        if entry.get("state") not in _ORDER_ALL_STATES:
            raise ValueError("journal entry state not allowed")
        for field in ("reason", "evidence", "ts_utc"):
            if not isinstance(entry.get(field), str):
                raise ValueError(f"journal entry {field} must be a string")
        # Optional ownership/recovery audit blocks (review #5710398920):
        # present-when-written, strictly typed, never required for legacy
        # records (a record WITHOUT owner metadata is simply liveness-
        # unverifiable and stays blocking).
        owner = entry.get("owner")
        if owner is not None:
            if not isinstance(owner, dict):
                raise ValueError("journal entry owner must be an object")
            if not isinstance(owner.get("owner_id"), str) or \
                    not _OWNER_ID_RE.match(str(owner.get("owner_id", ""))):
                raise ValueError("journal entry owner_id invalid")
            witness = owner.get("witness")
            if not isinstance(witness, str) or not witness or \
                    os.path.basename(witness) != witness:
                raise ValueError("journal entry owner witness invalid")
            if not isinstance(owner.get("pid"), int):
                raise ValueError("journal entry owner pid must be an int")
            if not isinstance(owner.get("started_at"), str):
                raise ValueError("journal entry owner started_at must be a string")
        recovery = entry.get("recovery")
        if recovery is not None:
            if not isinstance(recovery, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in recovery.items()):
                raise ValueError("journal entry recovery must be str->str")
        history = entry.get("history")
        if history is not None:
            if not isinstance(history, list) or any(
                    not isinstance(item, dict) for item in history):
                raise ValueError("journal entry history must be a list of objects")
    return entries


# ------------------------------------------------------- process singletons
_state_lock = threading.Lock()
_breaker: RateLimitBreaker | None = None
_storm_guard: RetryStormGuard | None = None
_order_guard: OrderSubmitGuard | None = None


def get_breaker() -> RateLimitBreaker:
    global _breaker
    if _breaker is None:
        with _state_lock:
            if _breaker is None:
                _breaker = RateLimitBreaker()
    return _breaker


def get_storm_guard() -> RetryStormGuard:
    global _storm_guard
    if _storm_guard is None:
        with _state_lock:
            if _storm_guard is None:
                _storm_guard = RetryStormGuard()
    return _storm_guard


def get_order_guard() -> OrderSubmitGuard:
    global _order_guard
    if _order_guard is None:
        with _state_lock:
            if _order_guard is None:
                _order_guard = OrderSubmitGuard()
    return _order_guard


def reset_process_state() -> None:
    """Reset all process-local retry governance state (tests / restarts)."""
    get_breaker().reset()
    get_storm_guard().reset()
    get_order_guard().reset()
