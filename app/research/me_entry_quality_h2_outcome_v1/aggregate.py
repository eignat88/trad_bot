"""Issue #298 / #312 — aggregate accounting (D-8, D-10, D-11, D-12).

Research-only, pure: no I/O, no network, no database, no clock reads.

THIS is the only place in the package that may emit yield / Net E[R] /
win-rate / MAE-MFE aggregates — and it REFUSES by default:

* every D-1..D-12 decision must be specified (no
  ``PENDING_OWNER_DECISION`` left) — otherwise the result status is
  ``NOT_READY_PENDING_DECISION`` and EVERY metric field is ``None``;
* the policy fingerprint must be approved: the policy must be ``FROZEN``
  via :meth:`OutcomePolicy.freeze` with an explicit approved fingerprint;
* the D-11 statistical acceptance rule must have been declared BEFORE any
  outcome was registered (:class:`OutcomeSet` enforces the ordering
  mechanically — a late declaration makes every aggregate refuse);
* ``MISSED_ENTRY`` / ``ENTRY_EXPIRED`` / ``NO_CONFIRMATION`` /
  ``INCOMPLETE_COVERAGE`` events are NEVER silently dropped: with the
  ``ALL_SOURCE_EVENTS`` denominator they stay in the denominator, and with
  a narrower declared denominator they are still reported as explicit
  ``excluded_by_declared_denominator`` counts.

Net E[R] (D-8) is the mean of per-event net returns in R multiples of each
leg's stop distance over the DECLARED denominator; the return assignment
for non-entered variant legs comes from the D-2 decision
(``ZERO_NET_RETURN_IN_DENOMINATOR`` or ``BASELINE_NET_AS_FORGONE_RETURN``).

Nothing here is publishable: the H2 track remains
``IMPLEMENTATION_MERGED / ACTIVATION_HOLD`` (issue #298).
"""
from __future__ import annotations

from typing import Any

from app.research.me_entry_quality_h2_outcome_v1.policy import (
    DECISION_PENDING,
    DENOM_ALL_CONFIRMED,
    DENOM_ALL_FUNDABLE,
    DENOM_ALL_SOURCE,
    MISSED_STAT_FORGONE_BASELINE,
    NORM_R_STOP_DISTANCE,
    RESULT_STATUS_AGGREGATE_READY,
    RESULT_STATUS_NOT_READY_PENDING_DECISION,
    SLOT_D11,
    SLOT_D12,
    OutcomePolicy,
    OutcomePolicyError,
    StatisticalAcceptanceRule,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    AggregateOutcome,
    ENTRY_ENTERED,
    ENTRY_NO_CONFIRMATION,
    EventPairOutcome,
)

AGGREGATE_VERSION = "H2_OUTCOME_AGGREGATE_1.0.0"

REASON_MISSING_DECISIONS = "MISSING_OWNER_DECISIONS"
REASON_POLICY_NOT_APPROVED = "POLICY_FINGERPRINT_NOT_APPROVED_NOT_FROZEN"
REASON_ACCEPTANCE_ORDER = "ACCEPTANCE_RULE_NOT_DECLARED_BEFORE_OUTCOMES_INSPECTED"
REASON_FINGERPRINT_MISMATCH = "OUTCOME_POLICY_FINGERPRINT_MISMATCH"
REASON_NO_OUTCOMES = "NO_OUTCOMES_REGISTERED"
REASON_EMPTY_DENOMINATOR = "EMPTY_DENOMINATOR_NO_EVENTS_IN_DECLARED_DENOMINATOR"
REASON_OK = "OK"

REASON_IMMUTABLE_CONFLICT = "IMMUTABLE_OUTCOME_CONFLICT"


class OutcomeConflictError(RuntimeError):
    """A terminal outcome was re-delivered with DIFFERENT content
    (fail closed — never rewrite an immutable outcome)."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class AcceptanceRuleOrderError(RuntimeError):
    """The D-11 statistical acceptance rule was declared AFTER outcomes
    were already registered (D-11 requires declaration BEFORE inspection)."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _in_denominator(pair: EventPairOutcome, definition: str) -> bool:
    if definition == DENOM_ALL_SOURCE:
        return True
    if definition == DENOM_ALL_CONFIRMED:
        return pair.variant.entry_status != ENTRY_NO_CONFIRMATION
    assert definition == DENOM_ALL_FUNDABLE
    return pair.variant.entry_status == ENTRY_ENTERED


def _effective_net_r(pair: EventPairOutcome, policy: OutcomePolicy) -> float | None:
    """Per-event net return in R under the D-2 assignment policy.

    Non-entered variant legs stay in the accounting with an EXPLICIT
    return assignment; unresolved entered legs (D-6
    ``STOP_OR_TARGET_ONLY`` without a touch) yield ``None`` and are
    reported, never invented.
    """
    variant = pair.variant
    if variant.entered:
        return variant.net_return_r
    fill_policy = policy.decision("d_2_fill_missed_entry_policy")
    if fill_policy.missed_entry_stat_policy == MISSED_STAT_FORGONE_BASELINE:
        baseline_net = pair.baseline.net_return_r
        return None if baseline_net is None else -float(baseline_net)
    return 0.0


class OutcomeSet:
    """Idempotent, append-only registry of paired outcomes plus the ONLY
    aggregate entry point.

    Idempotency key (D-12 plumbing): (``source_event_key``, ``variant_id``,
    ``policy_fingerprint``).  A redelivered identical outcome is a no-op; a
    redelivered DIFFERENT outcome raises :class:`OutcomeConflictError`
    (immutable terminal outcomes — mirroring the H2 store).
    """

    def __init__(self, policy: OutcomePolicy) -> None:
        if not isinstance(policy, OutcomePolicy):
            raise OutcomePolicyError("policy must be an OutcomePolicy")
        self._policy = policy
        self._outcomes: dict[tuple[str, str, str], EventPairOutcome] = {}
        self._order: list[tuple[str, str, str]] = []
        self._acceptance_declared_before_outcomes = (
            policy.slots()[SLOT_D11].status != DECISION_PENDING
        )

    # ── D-11 ordering enforcement ───────────────────────────────────────────
    def declare_acceptance_rule(self, rule: StatisticalAcceptanceRule) -> None:
        """Attach the D-11 statistical acceptance rule.

        Only legal BEFORE the first outcome is registered; a late
        declaration raises :class:`AcceptanceRuleOrderError` (D-11: the
        acceptance rule must be declared before outcomes are inspected).
        """
        if self._outcomes:
            raise AcceptanceRuleOrderError(REASON_ACCEPTANCE_ORDER)
        self._policy = self._policy.with_preliminary(SLOT_D11, rule)
        self._acceptance_declared_before_outcomes = True

    # ── append-only registry ────────────────────────────────────────────────
    def add_outcome(self, outcome: EventPairOutcome) -> str:
        """Register ONE paired outcome.  Returns ``"STORED"`` or
        ``"IDEMPOTENT_REDELIVERY"``; conflicting rewrites raise."""
        if not isinstance(outcome, EventPairOutcome):
            raise OutcomePolicyError("outcome must be an EventPairOutcome")
        key = outcome.outcome_key
        existing = self._outcomes.get(key)
        if existing is not None:
            if existing.to_dict() == outcome.to_dict():
                return "IDEMPOTENT_REDELIVERY"
            raise OutcomeConflictError(REASON_IMMUTABLE_CONFLICT)
        self._outcomes[key] = outcome
        self._order.append(key)
        return "STORED"

    def __len__(self) -> int:
        return len(self._outcomes)

    @property
    def policy(self) -> OutcomePolicy:
        return self._policy

    # ── the ONLY aggregate entry point ──────────────────────────────────────
    def aggregate(self) -> AggregateOutcome:
        policy = self._policy
        fingerprint = policy.policy_fingerprint()
        snapshot = {
            name: decision.to_dict() for name, decision in policy.slots().items()
        }

        def refusal(reason: str, missing: tuple[str, ...]) -> AggregateOutcome:
            return AggregateOutcome(
                status=RESULT_STATUS_NOT_READY_PENDING_DECISION,
                reason=reason,
                missing_decisions=missing,
                policy_fingerprint=fingerprint,
                policy_status=policy.policy_status,
                denominator_definition="",
                acceptance_rule=snapshot.get(SLOT_D11, {}).get("value") or {},
                n_total_source_events=None,
                n_in_denominator=None,
                n_excluded_by_denominator=None,
                counts_by_status={},
                net_er=None,
                win_rate=None,
                mean_mae=None,
                mean_mfe=None,
                missed_winner_count=None,
                missed_winner_keys=(),
                denominator_notes=(),
                metric_labels={},
            )

        # 0) D-11: the acceptance rule must have been declared BEFORE any
        # outcome was registered (a violation poisons the whole run)
        if self._outcomes and not self._acceptance_declared_before_outcomes:
            return refusal(REASON_ACCEPTANCE_ORDER, (SLOT_D11,))

        # 1) every D-1..D-11 decision specified?
        missing = policy.missing_spec_slots()
        if missing:
            return refusal(REASON_MISSING_DECISIONS, missing)

        # 2) policy fingerprint approved (FROZEN via approved fingerprint)?
        if not policy.is_ready_for_aggregates():
            return refusal(
                REASON_POLICY_NOT_APPROVED, (SLOT_D12,)
            )

        outcomes = [self._outcomes[key] for key in self._order]
        if not outcomes:
            return refusal(REASON_NO_OUTCOMES, ())

        # 4) every outcome must belong to THIS exact policy fingerprint
        mismatched = [
            outcome
            for outcome in outcomes
            if outcome.policy_fingerprint != fingerprint
        ]
        if mismatched:
            return refusal(REASON_FINGERPRINT_MISMATCH, (SLOT_D12,))

        denominator_spec = policy.decision("d_8_net_er_denominator")
        definition = denominator_spec.definition
        fill_policy = policy.decision("d_2_fill_missed_entry_policy")
        mae_mfe_spec = policy.decision("d_9_mae_mfe_conventions")
        acceptance: StatisticalAcceptanceRule = policy.decision(
            "d_11_statistical_acceptance_rule"
        )

        counts_by_status: dict[str, int] = {}
        for outcome in outcomes:
            status = outcome.variant.entry_status
            counts_by_status[status] = counts_by_status.get(status, 0) + 1

        in_denom = [
            outcome for outcome in outcomes if _in_denominator(outcome, definition)
        ]
        excluded = [
            outcome for outcome in outcomes if not _in_denominator(outcome, definition)
        ]

        effective: list[float] = []
        unresolved = 0
        for outcome in in_denom:
            value = _effective_net_r(outcome, policy)
            if value is None:
                unresolved += 1
            else:
                effective.append(value)

        n_in = len(in_denom)
        net_er = sum(effective) / len(effective) if effective else None
        wins = sum(1 for value in effective if value > 0.0)
        win_rate = wins / len(effective) if effective else None

        use_r = mae_mfe_spec.normalization == NORM_R_STOP_DISTANCE
        mae_values = [
            (outcome.variant.mae_r if use_r else outcome.variant.mae_pct)
            for outcome in in_denom
            if outcome.variant.entered
            and outcome.variant.mae_r is not None
            and outcome.variant.mae_pct is not None
        ]
        mfe_values = [
            (outcome.variant.mfe_r if use_r else outcome.variant.mfe_pct)
            for outcome in in_denom
            if outcome.variant.entered
            and outcome.variant.mfe_r is not None
            and outcome.variant.mfe_pct is not None
        ]
        mean_mae = sum(mae_values) / len(mae_values) if mae_values else None
        mean_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else None

        missed_keys = tuple(
            f"{outcome.source_event_key}|{outcome.variant_id}"
            for outcome in outcomes
            if outcome.missed_winner
        )

        notes = [
            f"denominator={definition}",
            f"missed_entry_stat_policy="
            f"{fill_policy.missed_entry_stat_policy}",
            f"n_unresolved_entered_legs_excluded_from_mean={unresolved}",
            f"n_excluded_by_declared_denominator={len(excluded)}",
        ]
        if len(effective) < int(acceptance.min_paired_n):
            notes.append(
                f"acceptance_rule_min_paired_n={acceptance.min_paired_n} "
                f"NOT_REACHED (n_defined_returns={len(effective)})"
            )
        else:
            notes.append(
                f"acceptance_rule_min_paired_n={acceptance.min_paired_n} "
                f"REACHED (n_defined_returns={len(effective)}) — the "
                f"acceptance DECISION itself stays an owner decision"
            )

        return AggregateOutcome(
            status=RESULT_STATUS_AGGREGATE_READY,
            reason=REASON_OK,
            missing_decisions=(),
            policy_fingerprint=fingerprint,
            policy_status=policy.policy_status,
            denominator_definition=definition,
            acceptance_rule=acceptance.to_dict(),
            n_total_source_events=len(outcomes),
            n_in_denominator=n_in,
            n_excluded_by_denominator=len(excluded),
            counts_by_status=counts_by_status,
            net_er=net_er,
            win_rate=win_rate,
            mean_mae=mean_mae,
            mean_mfe=mean_mfe,
            missed_winner_count=len(missed_keys),
            missed_winner_keys=missed_keys,
            denominator_notes=tuple(notes),
            metric_labels={
                "net_er": "MEAN_OF_PER_EVENT_NET_RETURN_R_OVER_DECLARED_DENOMINATOR",
                "win_rate": "SHARE_OF_DEFINED_RETURNS_WITH_NET_RETURN_R_GT_ZERO",
                "mean_mae": f"MEAN_MAE_{mae_mfe_spec.normalization}_"
                f"{mae_mfe_spec.path_sampling}",
                "mean_mfe": f"MEAN_MFE_{mae_mfe_spec.normalization}_"
                f"{mae_mfe_spec.path_sampling}",
            },
        )
