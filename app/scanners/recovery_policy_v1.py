"""Scanner AC7 Recovery V1: offline scheduling decision.

No Bybit calls, database writes, or limiter modifications.
Recovery must be a NEW cycle with a NEW run_id.
Historical incomplete cycles are never rewritten.

Scheduling contract
-------------------
``decide_recovery`` is a pure function: it only shortens the delay before the
NEXT ordinary cycle.  It never spawns a cycle of its own, never reuses a
``run_id`` and never touches history — every rescan is the regular main-loop
iteration with its own ``repository.start_run()`` row.

Delay rule (review: the fixed 130 s constant is NOT a proven-safe value)
------------------------------------------------------------------------
The historical default ``RECOVERY_DELAY_SECONDS`` (130 s) was picked to
exceed the DEFAULT level-3 shared-breaker cooldown (``DEFAULT_COOLDOWNS_S``
= ``(5.0, 30.0, 120.0)``) plus margin — but production cooldowns come from
``global_limiter_breaker_cooldowns_s`` and are configurable.  Waking the
rescan before the breaker cooldown actually expires is provably futile: the
shared limiter denies every request pre-send (``breaker_cooldown_exceeds_
deadline``) and the one-shot recovery is burned on another 0/N cycle.

So the expedited delay is a well-defined scheduling condition::

    delay = min(normal_delay_seconds,
                max(RECOVERY_DELAY_SECONDS, breaker_cooldown_hint_seconds))

* never shorter than the moment the shared breaker can grant again
  (``breaker_cooldown_hint_seconds``, read from the limiter's read-only
  snapshot — see :func:`breaker_cooldown_hint_seconds`);
* never shorter than the experimental floor (130 s);
* never longer than the normal schedule (recovery may only accelerate).

When that value cannot beat ``normal_delay_seconds`` the decision reports
``RECOVERY_NOT_BENEFICIAL`` and the normal schedule is kept unchanged.

Fail-closed everywhere: an unprovable cooldown hint raises
:class:`RecoveryHintUnavailable`; the caller keeps the normal schedule and
the one-shot guard is left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

#: EXPERIMENTAL floor for the expedited recovery delay.  Derived from the
#: default level-3 breaker cooldown (120 s) + 10 s margin — NOT a proven-safe
#: constant: it only ever shortens a delay, and
#: :func:`decide_recovery` additionally clamps it against the LIVE breaker
#: cooldown hint, so a longer configured cooldown can never be slept through.
RECOVERY_DELAY_SECONDS = 130.0

# Decision reasons (stable, greppable, asserted by tests).
REASON_NORMAL_SCHEDULE = "NORMAL_SCHEDULE"
REASON_ALREADY_ATTEMPTED = "RECOVERY_ALREADY_ATTEMPTED"
REASON_NOT_BENEFICIAL = "RECOVERY_NOT_BENEFICIAL"
REASON_PRE_SEND_SKIP = "LIMITER_PRE_SEND_SKIP_NEW_CYCLE"

# Breaker states as reported by ``GlobalBybitRateLimiter.snapshot()``
# (``app.exchange.global_rate_limiter``: BKR_CLOSED/BKR_OPEN/BKR_RECOVERY).
_BKR_OPEN = "OPEN"
_BKR_RECOVERY = "RECOVERY"
_BKR_CLOSED = "CLOSED"
_BKR_STATES = (_BKR_CLOSED, _BKR_OPEN, _BKR_RECOVERY)


class RecoveryHintUnavailable(RuntimeError):
    """The breaker cooldown hint could not be PROVEN from a read-only view.

    Raised by :func:`breaker_cooldown_hint_seconds`; the caller keeps the
    normal schedule (fail-closed) instead of trusting the unverified fixed
    delay.
    """


def breaker_cooldown_hint_seconds(client, *, now_wall: float | None = None) -> float:
    """Seconds until the shared breaker can grant requests again (read-only).

    Reads ONLY the limiter's ``snapshot()`` observability view — no Bybit
    request, no acquire, no limiter mutation, no database access.

    Returns
    -------
    ``cooldown_until_wall - now`` while the breaker is ``OPEN`` (that is the
    moment it transitions to ``RECOVERY`` and starts admitting probe
    traffic), and ``0.0`` in ``RECOVERY`` / ``CLOSED`` states — both already
    admit requests (``RECOVERY`` serializes them one probe at a time, but
    traffic is flowing and a successful probe streak closes the breaker).

    Raises
    ------
    RecoveryHintUnavailable
        whenever the hint cannot be proven: no shared-limiter accessor,
        limiter disabled/unavailable, or a snapshot shape this code does not
        understand.  Never guesses.
    """
    accessor = getattr(client, "_shared_limiter", None)
    if accessor is None:
        raise RecoveryHintUnavailable("client exposes no shared limiter accessor")
    limiter = accessor() if callable(accessor) else accessor
    if limiter is None:
        raise RecoveryHintUnavailable("shared limiter disabled")
    snapshot_method = getattr(limiter, "snapshot", None)
    if not callable(snapshot_method):
        raise RecoveryHintUnavailable("limiter exposes no read-only snapshot()")
    snap = snapshot_method()
    if (
        not isinstance(snap, dict)
        or snap.get("enabled") is False
        or snap.get("available") is False
    ):
        raise RecoveryHintUnavailable(
            f"limiter snapshot unavailable: {snap!r}"
        )
    state = snap.get("state")
    if state not in _BKR_STATES:
        raise RecoveryHintUnavailable(f"unknown breaker state {state!r}")
    if state != _BKR_OPEN:
        # RECOVERY / CLOSED already admit requests — no cooldown to sleep off.
        return 0.0
    until = snap.get("cooldown_until_wall")
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        raise RecoveryHintUnavailable(f"cooldown_until_wall not numeric: {until!r}")
    if not math.isfinite(float(until)):
        raise RecoveryHintUnavailable(f"cooldown_until_wall not finite: {until!r}")
    now = time.time() if now_wall is None else float(now_wall)
    return max(0.0, float(until) - now)


@dataclass(frozen=True)
class RecoveryDecision:
    expedite: bool
    delay_seconds: float
    reason: str


def decide_recovery(
    *,
    enabled: bool,
    expected_symbols: int,
    processed_symbols: int,
    limiter_pre_send_skips: int,
    previous_recovery_attempted: bool,
    normal_delay_seconds: float,
    recovery_delay_seconds: float = RECOVERY_DELAY_SECONDS,
    limiter_cooldown_hint_seconds: float = 0.0,
) -> RecoveryDecision:
    """Decide whether the NEXT cycle may start early (one-shot, fail-closed).

    ``limiter_pre_send_skips`` must count DISTINCT symbols that produced no
    market context this cycle and were refused by the shared limiter BEFORE
    send (scan-stage outcomes of processed symbols never count).  The
    ``<= missing`` check below is the consistency invariant: a report whose
    skip set exceeds the unprocessed set is corrupt and fails closed.

    ``limiter_cooldown_hint_seconds`` is the read-only breaker cooldown hint
    (:func:`breaker_cooldown_hint_seconds`); the expedited delay is never
    shorter than it — see the module docstring for the full rule.
    """

    if (
        not math.isfinite(normal_delay_seconds)
        or normal_delay_seconds < 0
        or not math.isfinite(recovery_delay_seconds)
        or recovery_delay_seconds <= 0
    ):
        raise ValueError("INVALID_DELAY")

    if (
        not math.isfinite(limiter_cooldown_hint_seconds)
        or limiter_cooldown_hint_seconds < 0
    ):
        raise ValueError("INVALID_COOLDOWN_HINT")

    if expected_symbols < 0:
        raise ValueError("INVALID_EXPECTED")

    if not 0 <= processed_symbols <= expected_symbols:
        raise ValueError("INVALID_PROCESSED")

    missing = expected_symbols - processed_symbols

    if not 0 <= limiter_pre_send_skips <= missing:
        raise ValueError("INVALID_SKIPS")

    normal = RecoveryDecision(
        expedite=False,
        delay_seconds=normal_delay_seconds,
        reason=REASON_NORMAL_SCHEDULE,
    )

    if not enabled:
        return normal

    if previous_recovery_attempted:
        return RecoveryDecision(
            expedite=False,
            delay_seconds=normal_delay_seconds,
            reason=REASON_ALREADY_ATTEMPTED,
        )

    if limiter_pre_send_skips == 0:
        return normal

    if missing == 0:
        return normal

    # Well-defined scheduling condition: wake no earlier than the breaker
    # can grant (cooldown hint), no earlier than the experimental floor, and
    # never later than the normal schedule.
    delay = min(
        normal_delay_seconds,
        max(recovery_delay_seconds, limiter_cooldown_hint_seconds),
    )

    if delay >= normal_delay_seconds:
        # No acceleration is possible — the normal schedule is already the
        # soonest sensible wake-up (or sooner than the cooldown allows).
        return RecoveryDecision(
            expedite=False,
            delay_seconds=normal_delay_seconds,
            reason=REASON_NOT_BENEFICIAL,
        )

    return RecoveryDecision(
        expedite=True,
        delay_seconds=delay,
        reason=REASON_PRE_SEND_SKIP,
    )
