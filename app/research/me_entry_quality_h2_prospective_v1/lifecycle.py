"""Issue #312 / FR-04 — prospective event lifecycle rules (pure).

Research-only, pure: no I/O of any kind.  Deterministic mapping from ONE H2
pipeline evaluation to a lifecycle decision for the key
(``source_event_key`` x ``variant_id`` x ``policy_fingerprint``).

State machine (one-way, restart-safe — the store persists it):

``PENDING_CONFIRMATION`` -> ``CONFIRMED_DELAYED``   (pipeline CONFIRMED)
                        -> ``EXPIRED``              (valid data, window done)
                        -> ``INVALID_SOURCE``       (source/feature integrity)
                        -> ``NOT_READY``            (unprespecified peak rule)
                        -> ``INCOMPLETE_COVERAGE``  (window elapsed AND the
                             coverage defect is permanent OR persisted across
                             ``coverage_finalize_after_attempts`` attempts)

Hard rules (FR-04 / criteria 6, 7, 12):

* MISSING CANDLE DATA NEVER becomes ``EXPIRED`` or ``CONFIRMED_DELAYED``:
  an evaluation that cannot see a complete, valid window keeps the event
  ``PENDING_CONFIRMATION`` (or finalizes as ``INCOMPLETE_COVERAGE`` under the
  explicit rules above) — nothing is ever fabricated;
* a terminal outcome is terminal forever: re-evaluations and redeliveries can
  never recreate or change it;
* ``EXPIRED`` is only reachable from VALID data with the full observation
  window actually observed (``observed_window_candles >= window``).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
    STATE_PENDING_CONFIRMATION,
    TERMINAL_STATES,
    is_permanent_coverage_reason,
)

H2P_LIFECYCLE_VERSION = "H2P_LIFECYCLE_1.0.0"

# ─── decision reasons ────────────────────────────────────────────────────────
REASON_OK = "OK"
REASON_WINDOW_EXHAUSTED = "OBSERVATION_WINDOW_EXHAUSTED_NO_CONFIRMATION"
REASON_COVERAGE_PERMANENT = "COVERAGE_DEFECT_PERMANENT"
REASON_COVERAGE_PERSISTENT = "COVERAGE_DEFECT_PERSISTENT"
REASON_AWAITING_COVERAGE = "AWAITING_COMPLETE_COVERAGE"
REASON_TERMINAL_LOCKED = "TERMINAL_STATE_LOCKED"


@dataclass(frozen=True)
class LifecycleDecision:
    """Immutable outcome of :func:`decide_next_state`."""

    #: ``None`` = keep the current state (still pending); otherwise the
    #: exact new lifecycle state.
    new_state: str | None
    terminal: bool
    reason: str
    detail: str = ""

    @property
    def changes(self) -> bool:
        return self.new_state is not None


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


@dataclass(frozen=True)
class EvaluationObservation:
    """What one evaluation attempt observed for ONE (event, variant, policy).

    ``pipeline_status`` is the pure H2 result status; ``coverage_reason`` is
    the H2 reason code when data coverage is the blocker (else ``""``);
    ``coverage_attempts`` counts evaluation attempts that ended in
    incomplete coverage INCLUDING the current one.
    """

    current_state: str
    pipeline_status: str
    coverage_reason: str = ""
    observed_window_candles: int = 0
    observation_window: int = 1
    window_elapsed: bool = False
    coverage_attempts: int = 1
    coverage_finalize_after_attempts: int = 3


def decide_next_state(obs: EvaluationObservation) -> LifecycleDecision:
    """Deterministic lifecycle decision for one evaluation attempt.

    Pure function of ``obs``: reruns are identical and no appended future
    data can change an earlier decision.
    """
    if obs.current_state in TERMINAL_STATES:
        return LifecycleDecision(
            new_state=None,
            terminal=True,
            reason=REASON_TERMINAL_LOCKED,
            detail=f"{obs.current_state} is terminal; never re-decided",
        )

    status = obs.pipeline_status

    # Source / policy integrity: terminal immediately (fail closed).
    if status == STATE_INVALID_SOURCE:
        return LifecycleDecision(
            new_state=STATE_INVALID_SOURCE,
            terminal=True,
            reason=REASON_OK,
            detail="source/feature integrity failure at evaluation",
        )
    if status == STATE_NOT_READY:
        return LifecycleDecision(
            new_state=STATE_NOT_READY,
            terminal=True,
            reason=REASON_OK,
            detail="policy not ready (peak rule not prespecified)",
        )

    # Confirmed delayed entry (verbatim from the pure engine).
    if status == STATE_CONFIRMED_DELAYED:
        return LifecycleDecision(
            new_state=STATE_CONFIRMED_DELAYED,
            terminal=True,
            reason=REASON_OK,
            detail="RSI weakening confirmed on closed candles",
        )

    # Expiry: ONLY from valid, fully observed windows — never from missing
    # data (missing data can never fabricate an EXPIRED outcome).
    if status == STATE_EXPIRED:
        if obs.observed_window_candles < obs.observation_window:
            return LifecycleDecision(
                new_state=None,
                terminal=False,
                reason=REASON_AWAITING_COVERAGE,
                detail=(
                    "EXPIRED refused: the observation window was not fully "
                    f"observed ({obs.observed_window_candles}/"
                    f"{obs.observation_window})"
                ),
            )
        return LifecycleDecision(
            new_state=STATE_EXPIRED,
            terminal=True,
            reason=REASON_WINDOW_EXHAUSTED,
            detail="observation window fully closed without confirmation",
        )

    # Incomplete coverage: never a market mode, never EXPIRED.
    if status == STATE_INCOMPLETE_COVERAGE:
        if not obs.window_elapsed:
            return LifecycleDecision(
                new_state=None,
                terminal=False,
                reason=REASON_AWAITING_COVERAGE,
                detail=(
                    "observation window still open; incomplete coverage is "
                    "not finalizable yet"
                ),
            )
        if is_permanent_coverage_reason(obs.coverage_reason):
            return LifecycleDecision(
                new_state=STATE_INCOMPLETE_COVERAGE,
                terminal=True,
                reason=REASON_COVERAGE_PERMANENT,
                detail=f"permanent coverage defect: {obs.coverage_reason}",
            )
        if obs.coverage_attempts >= obs.coverage_finalize_after_attempts:
            return LifecycleDecision(
                new_state=STATE_INCOMPLETE_COVERAGE,
                terminal=True,
                reason=REASON_COVERAGE_PERSISTENT,
                detail=(
                    f"coverage defect {obs.coverage_reason or 'UNKNOWN'} "
                    f"persisted for {obs.coverage_attempts} attempts after "
                    "the window elapsed"
                ),
            )
        return LifecycleDecision(
            new_state=None,
            terminal=False,
            reason=REASON_AWAITING_COVERAGE,
            detail=(
                f"transient coverage defect {obs.coverage_reason or 'UNKNOWN'} "
                f"(attempt {obs.coverage_attempts}/"
                f"{obs.coverage_finalize_after_attempts})"
            ),
        )

    # PENDING_CONFIRMATION (incl. awaiting first observation) — keep waiting.
    return LifecycleDecision(
        new_state=None,
        terminal=False,
        reason=REASON_OK,
        detail="confirmation window still open",
    )
