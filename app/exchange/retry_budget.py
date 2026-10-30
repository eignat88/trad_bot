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
  protection (fail-closed on an indeterminate submission outcome).

All state is process-local and observational side-effect free. The retry
accounting fields (``attempt_index``, ``retry_reason``, ``total_attempts``)
are emitted to ``BYBIT_API_TELEMETRY_V1`` by the transport (Issue #228
parity: exactly one ``BYBIT_API_CALL`` record per HTTP attempt).
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
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
# Persistent journal for the double-submit guard (code review #5709376849):
# submission state must survive process restarts, so it is stored as JSON and
# never released by a timer.
ORDER_JOURNAL_SCHEMA = 1
ORDER_JOURNAL_DEFAULT_PATH = "var/reports/bybit_order_submit_journal_v1.json"

_ORDER_ACTIVE_STATES = ("in_flight", "unknown")
_logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _order_uncertain_error(message: str) -> BaseException:
    """Build the fail-closed order error (import avoids a cycle)."""
    from app.exchange.bybit_client import BybitOrderUncertainError

    return BybitOrderUncertainError(message)


class OrderSubmitGuard:
    """Persistent fail-closed double-submit guard for POST ``/v5/order/create``.

    Code review #5709376849 (P1 — indeterminate create_order outcome):

    * an UNKNOWN outcome (timeout / transport failure / 5xx / unparseable
      body — the order may exist server-side) is **never released by a
      timer**. The legacy ``bybit_order_resubmit_block_s`` window is kept
      for config rollback compatibility only; it does not unblock anything;
    * the block is lifted only by definitive evidence: a parsed API result
      of the submission itself (including a rejection), an HTTP 4xx
      rejection, or an explicit **reconciliation** against the exchange by
      the recorded ``orderLinkId`` (or a documented operator resolution);
    * state is journaled (schema 1) and survives restarts: a crash while a
      POST was ``in_flight`` reloads as ``unknown`` — fail-closed;
    * ``in_flight`` persists until the operation actually finishes,
      regardless of any TTL — concurrent identical submits are blocked
      (code review P1 — concurrent POSTs).

    If the journal cannot be read or written the guard FAILS CLOSED: order
    submission is refused instead of silently losing the block.
    """

    def __init__(self, journal_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._journal_path: str | None = None
        self._load_error: str | None = None
        self._write_degraded = False
        self._entries: dict[str, dict[str, Any]] = {}
        if journal_path:
            self.configure(journal_path)

    # ------------------------------------------------------- persistence
    def configure(self, journal_path: str) -> None:
        """Bind the journal path (reload when it changes)."""
        with self._lock:
            journal_path = str(journal_path)
            if journal_path == self._journal_path:
                return
            self._journal_path = journal_path
            self._load_error = None
            self._write_degraded = False
            self._entries = {}
            self._load()

    def _load(self) -> None:
        path = self._journal_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries", {})
            if not isinstance(entries, dict):
                raise ValueError("journal entries must be an object")
        except (OSError, ValueError) as exc:
            # Fail closed: an unreadable journal cannot prove anything was
            # resolved — refuse submissions until an operator restores it.
            self._load_error = (
                f"order-submit journal unreadable ({type(exc).__name__})")
            return
        for key, entry in entries.items():
            entry = dict(entry)
            if entry.get("state") == "in_flight":
                # Crashed/restarted mid-send: the outcome is UNKNOWN.
                entry["state"] = "unknown"
                entry["reason"] = (
                    str(entry.get("reason") or "")
                    or "in_flight at crash/restart (outcome UNKNOWN)")
            self._entries[str(key)] = entry

    def _write(self) -> bool:
        """Persist the journal atomically; False on failure (degraded)."""
        path = self._journal_path
        if not path:
            return True
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"schema": ORDER_JOURNAL_SCHEMA,
                           "entries": self._entries},
                          fh, indent=1, sort_keys=True)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            self._write_degraded = True
            _logger.critical(
                "order-submit journal write failed (%s) — further order "
                "submissions will be refused (fail-closed)", type(exc).__name__)
            return False

    @staticmethod
    def _key(fingerprint: tuple, order_link_id: str) -> str:
        # Unique per submission attempt: the journal is an audit trail —
        # resolved records are preserved, never overwritten (review P1).
        return json.dumps([[str(part) for part in fingerprint],
                           str(order_link_id)], separators=(",", ":"))

    # ------------------------------------------------------- transitions
    def begin(self, fingerprint: tuple, *, order_link_id: str) -> bool:
        """Reserve the fingerprint for one submission attempt.

        Returns ``False`` while an identical submission is ``in_flight`` or
        its outcome is still ``unknown`` (no timer release). Fails closed
        (raises) when the journal is unusable.
        """
        with self._lock:
            if self._load_error or self._write_degraded:
                raise _order_uncertain_error(
                    f"{self._load_error or 'order-submit journal write degraded'}"
                    " — submissions are blocked (fail-closed)")
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
            self._entries[self._key(fingerprint, order_link_id)] = {
                "fingerprint": fp_list,
                "order_link_id": str(order_link_id),
                "state": "in_flight",
                "reason": "",
                "evidence": "",
                "ts_utc": _utc_now(),
            }
            if not self._write():
                self._entries.pop(self._key(fingerprint, order_link_id), None)
                raise _order_uncertain_error(
                    "cannot persist order submission state — submission "
                    "blocked (fail-closed, no send)")
            return True

    def mark_unknown(self, fingerprint: tuple, *, order_link_id: str,
                     reason: str) -> None:
        """Record an INDETERMINATE outcome (blocks until reconciliation)."""
        with self._lock:
            key = self._key(fingerprint, order_link_id)
            entry = self._entries.get(key) or {
                "fingerprint": [str(part) for part in fingerprint],
                "order_link_id": str(order_link_id)}
            entry.update({
                "order_link_id": str(order_link_id),
                "state": "unknown",
                "reason": str(reason),
                "ts_utc": _utc_now(),
            })
            self._entries[key] = entry
            self._write()

    def mark_resolved(self, fingerprint: tuple, *, order_link_id: str,
                      evidence: str) -> None:
        """Record a DEFINITIVE outcome of the submission itself."""
        with self._lock:
            key = self._key(fingerprint, order_link_id)
            entry = self._entries.get(key) or {
                "fingerprint": [str(part) for part in fingerprint],
                "order_link_id": str(order_link_id)}
            entry.update({
                "state": "resolved",
                "evidence": str(evidence),
                "ts_utc": _utc_now(),
            })
            self._entries[key] = entry
            self._write()

    def resolve_reconciled(self, order_link_id: str, *, evidence: str) -> bool:
        """Lift the block with explicit exchange-confirmed evidence.

        Used by the reconciliation procedure (or a documented operator
        resolution). Never time-based. Returns False when no active entry
        owns ``order_link_id``.
        """
        with self._lock:
            for entry in self._entries.values():
                if (str(entry.get("order_link_id", "")) == str(order_link_id)
                        and entry.get("state") in _ORDER_ACTIVE_STATES):
                    entry["state"] = "resolved"
                    entry["evidence"] = str(evidence)
                    entry["ts_utc"] = _utc_now()
                    self._write()
                    return True
            return False

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

        The journal file is the durable record and is deliberately NOT
        deleted here — restart recovery depends on it.
        """
        with self._lock:
            self._entries = {}
            self._load_error = None
            self._write_degraded = False
            self._journal_path = None


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
