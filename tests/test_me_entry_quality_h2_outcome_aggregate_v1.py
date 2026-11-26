"""Issue #298 / #312 — aggregate accounting tests (D-8, D-10, D-11, D-12).

Covers the honesty gate: WITHOUT every D-1..D-12 decision specified AND an
approved (FROZEN) policy fingerprint the aggregate REFUSES with
``NOT_READY_PENDING_DECISION`` and carries NO number at all.  Also covers
declared-denominator accounting (missed/expired events are never silently
dropped), the D-2 missed-entry return assignment, D-10 missed-winner
counts, D-11 declaration ordering and the D-12 idempotency/conflict rules.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.research.me_entry_quality_h2_outcome_v1.aggregate import (
    REASON_ACCEPTANCE_ORDER,
    REASON_FINGERPRINT_MISMATCH,
    REASON_MISSING_DECISIONS,
    REASON_POLICY_NOT_APPROVED,
    AcceptanceRuleOrderError,
    OutcomeConflictError,
    OutcomeSet,
)
from app.research.me_entry_quality_h2_outcome_v1.paired import evaluate_pair
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    DENOM_ALL_FUNDABLE,
    DENOM_ALL_SOURCE,
    MISSED_STAT_FORGONE_BASELINE,
    MISSED_STAT_ZERO_NET,
    RESULT_STATUS_AGGREGATE_READY,
    RESULT_STATUS_NOT_READY_PENDING_DECISION,
    MissedWinnerRule,
    OutcomePolicy,
    StatisticalAcceptanceRule,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    ENTRY_ENTERED,
    ENTRY_MISSED,
    ENTRY_NO_CONFIRMATION,
    ConfirmationInfo,
    SourceEventInput,
)

# shared fixtures/helpers from the paired suite (same test directory)
from test_me_entry_quality_h2_outcome_paired_v1 import (
    ASOF_US,
    CONFIRMATION,
    DEFAULT_CANDLES,
    EVENT,
    bar,
    make_policy,
)

NO_CONFIRMATION = ConfirmationInfo(state="EXPIRED", confirmation_time_us=None)


def locked_first_bar_candles():
    """A locked first candidate bar -> the variant is MISSED_ENTRY."""
    return [
        bar(0, 100.0, 100.5, 99.8, 100.2),
        bar(1, 100.3, 100.3, 100.3, 100.3),
        bar(2, 100.3, 100.6, 100.1, 100.4),
        bar(3, 100.4, 100.5, 99.5, 99.7),
        bar(4, 99.7, 99.9, 98.2, 98.5),
        bar(5, 98.5, 98.8, 97.9, 98.1),
    ]


def make_outcome(policy, *, event=None, confirmation=CONFIRMATION,
                 candles=None):
    return evaluate_pair(
        event=event or EVENT,
        confirmation=confirmation,
        candles=DEFAULT_CANDLES if candles is None else candles,
        policy=policy,
        evaluation_asof_us=ASOF_US,
    )


def frozen_policy(**kwargs) -> OutcomePolicy:
    policy = make_policy(**kwargs)
    return policy.freeze(policy.spec_fingerprint())


class TestRefusalGate:
    def test_refuses_while_any_decision_is_pending(self):
        policy = OutcomePolicy()
        result = OutcomeSet(policy).aggregate()
        assert result.status == RESULT_STATUS_NOT_READY_PENDING_DECISION
        assert result.refused
        assert result.reason == REASON_MISSING_DECISIONS
        assert "d_1_entry_price_class" in result.missing_decisions
        assert len(result.missing_decisions) == 11  # D-1..D-11 (D-12 is the freeze)

    def test_refuses_when_policy_is_specified_but_not_approved(self):
        policy = make_policy()  # PRELIMINARY_CANDIDATE_NOT_FROZEN
        outcome = make_outcome(policy)
        registry = OutcomeSet(policy)
        registry.add_outcome(outcome)
        result = registry.aggregate()
        assert result.status == RESULT_STATUS_NOT_READY_PENDING_DECISION
        assert result.reason == REASON_POLICY_NOT_APPROVED
        assert result.missing_decisions == ("d_12_policy_freeze",)

    def test_refusal_carries_no_number_at_all(self):
        policy = make_policy()
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(policy))
        result = registry.aggregate()
        for field_name in (
            "net_er",
            "win_rate",
            "mean_mae",
            "mean_mfe",
            "missed_winner_count",
            "n_total_source_events",
            "n_in_denominator",
        ):
            assert getattr(result, field_name) is None, field_name
        assert result.to_dict()["net_er"] is None

    def test_refuses_when_acceptance_rule_was_not_declared_before_outcomes(self):
        policy = OutcomePolicy()  # D-11 pending at construction
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(make_policy()))
        result = registry.aggregate()
        assert result.status == RESULT_STATUS_NOT_READY_PENDING_DECISION
        assert result.reason == REASON_ACCEPTANCE_ORDER

    def test_late_acceptance_rule_declaration_is_rejected(self):
        registry = OutcomeSet(OutcomePolicy())
        registry.add_outcome(make_outcome(make_policy()))
        with pytest.raises(AcceptanceRuleOrderError):
            registry.declare_acceptance_rule(
                StatisticalAcceptanceRule(
                    min_paired_n=5,
                    evaluation_cadence="WEEKLY",
                    decision_rule="RULE",
                    declared_before_outcomes_inspected=True,
                )
            )

    def test_acceptance_rule_may_be_declared_before_any_outcome(self):
        registry = OutcomeSet(OutcomePolicy())
        registry.declare_acceptance_rule(
            StatisticalAcceptanceRule(
                min_paired_n=5,
                evaluation_cadence="WEEKLY",
                decision_rule="RULE",
                declared_before_outcomes_inspected=True,
            )
        )
        # now the run is only blocked by the remaining pending decisions
        result = registry.aggregate()
        assert result.reason == REASON_MISSING_DECISIONS

    def test_refuses_outcomes_of_a_different_policy_fingerprint(self):
        frozen = frozen_policy()
        foreign = make_policy()  # different (unfrozen) fingerprint
        registry = OutcomeSet(frozen)
        registry.add_outcome(make_outcome(frozen))
        registry.add_outcome(make_outcome(foreign))
        result = registry.aggregate()
        assert result.status == RESULT_STATUS_NOT_READY_PENDING_DECISION
        assert result.reason == REASON_FINGERPRINT_MISMATCH


class TestAggregatesUnderFrozenPolicy:
    def _registry(self, **kwargs):
        policy = frozen_policy(**kwargs)
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(policy))
        return policy, registry

    def test_frozen_policy_emits_aggregates_with_labels(self):
        policy, registry = self._registry()
        result = registry.aggregate()
        assert result.status == RESULT_STATUS_AGGREGATE_READY
        assert result.policy_status == "FROZEN"
        assert result.policy_fingerprint == policy.policy_fingerprint()
        assert result.net_er is not None
        assert result.win_rate == 1.0
        assert result.metric_labels["net_er"].startswith("MEAN_OF_PER_EVENT")
        assert result.to_dict()["publication_status"] == (
            "NOT_FOR_PUBLICATION_H2_ACTIVATION_HOLD"
        )

    def test_net_er_is_the_mean_of_per_event_net_returns_r(self):
        policy, registry = self._registry()
        result = registry.aggregate()
        expected = policy  # variant net R of the single entered event
        outcome = make_outcome(expected)
        assert result.net_er == pytest.approx(outcome.variant.net_return_r)
        assert result.denominator_definition == DENOM_ALL_SOURCE
        assert result.n_in_denominator == 1
        assert result.n_excluded_by_denominator == 0

    def test_missed_and_unconfirmed_events_stay_in_the_denominator(self):
        policy = frozen_policy()
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(policy))  # entered
        registry.add_outcome(
            make_outcome(
                policy,
                event=replace(EVENT, source_event_key="src-2"),
                confirmation=NO_CONFIRMATION,
            )
        )
        registry.add_outcome(
            make_outcome(
                policy,
                event=replace(EVENT, source_event_key="src-3"),
                candles=locked_first_bar_candles(),
            )
        )
        result = registry.aggregate()
        assert result.n_total_source_events == 3
        assert result.n_in_denominator == 3  # ALL_SOURCE_EVENTS keeps them all
        assert result.counts_by_status[ENTRY_ENTERED] == 1
        assert result.counts_by_status[ENTRY_NO_CONFIRMATION] == 1
        assert result.counts_by_status[ENTRY_MISSED] == 1

    def test_narrower_declared_denominator_reports_exclusions(self):
        policy = frozen_policy(denominator=DENOM_ALL_FUNDABLE)
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(policy))
        registry.add_outcome(
            make_outcome(
                policy,
                event=replace(EVENT, source_event_key="src-2"),
                confirmation=NO_CONFIRMATION,
            )
        )
        result = registry.aggregate()
        assert result.n_total_source_events == 2
        assert result.n_in_denominator == 1
        assert result.n_excluded_by_denominator == 1
        assert any(
            "n_excluded_by_declared_denominator=1" in note
            for note in result.denominator_notes
        )
        assert result.counts_by_status[ENTRY_NO_CONFIRMATION] == 1

    def test_missed_entry_zero_return_assignment(self):
        policy = frozen_policy(missed_stat=MISSED_STAT_ZERO_NET)
        registry = OutcomeSet(policy)
        entered = make_outcome(policy)
        missed = make_outcome(
            policy,
            event=replace(EVENT, source_event_key="src-2"),
            candles=locked_first_bar_candles(),
        )
        registry.add_outcome(entered)
        registry.add_outcome(missed)
        result = registry.aggregate()
        expected = entered.variant.net_return_r / 2.0  # (x + 0) / 2
        assert result.net_er == pytest.approx(expected)

    def test_missed_entry_forgone_baseline_assignment(self):
        policy = frozen_policy(missed_stat=MISSED_STAT_FORGONE_BASELINE)
        registry = OutcomeSet(policy)
        entered = make_outcome(policy)
        missed = make_outcome(
            policy,
            event=replace(EVENT, source_event_key="src-2"),
            candles=locked_first_bar_candles(),
        )
        registry.add_outcome(entered)
        registry.add_outcome(missed)
        result = registry.aggregate()
        expected = (
            entered.variant.net_return_r - missed.baseline.net_return_r
        ) / 2.0
        assert result.net_er == pytest.approx(expected)
        assert any(
            "BASELINE_NET_AS_FORGONE_RETURN" in note
            for note in result.denominator_notes
        )

    def test_win_rate_and_mae_mfe_over_the_declared_denominator(self):
        policy = frozen_policy()
        losing_event = replace(EVENT, source_event_key="src-2")
        losing_candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 101.5, 100.3, 101.2),  # stops out the variant
            bar(3, 101.2, 101.8, 101.0, 101.5),
        ]
        registry = OutcomeSet(policy)
        winner = make_outcome(policy)
        loser = make_outcome(policy, event=losing_event,
                             candles=losing_candles)
        registry.add_outcome(winner)
        registry.add_outcome(loser)
        result = registry.aggregate()
        assert result.win_rate == pytest.approx(0.5)
        assert result.mean_mae is not None
        assert result.mean_mfe is not None
        expected = (
            winner.variant.net_return_r + loser.variant.net_return_r
        ) / 2.0
        assert result.net_er == pytest.approx(expected)

    def test_missed_winners_are_counted_and_listed(self):
        policy = frozen_policy()
        registry = OutcomeSet(policy)
        registry.add_outcome(
            make_outcome(policy, confirmation=NO_CONFIRMATION)
        )
        result = registry.aggregate()
        assert result.missed_winner_count == 1
        assert result.missed_winner_keys == ("src-1|H2_RSI_TURN_1",)

    def test_acceptance_min_paired_n_is_reported_not_invented(self):
        policy = frozen_policy()
        registry = OutcomeSet(policy)
        registry.add_outcome(make_outcome(policy))
        result = registry.aggregate()
        assert any(
            "acceptance_rule_min_paired_n=10" in note
            for note in result.denominator_notes
        )


class TestIdempotencyAndConflicts:
    def test_identical_redelivery_is_idempotent(self):
        policy = frozen_policy()
        registry = OutcomeSet(policy)
        outcome = make_outcome(policy)
        assert registry.add_outcome(outcome) == "STORED"
        assert registry.add_outcome(make_outcome(policy)) == (
            "IDEMPOTENT_REDELIVERY"
        )
        assert len(registry) == 1

    def test_conflicting_rewrite_of_a_terminal_outcome_is_refused(self):
        policy = frozen_policy()
        registry = OutcomeSet(policy)
        outcome = make_outcome(policy)
        registry.add_outcome(outcome)
        tampered = replace(outcome, reason="TAMPERED")
        assert tampered.outcome_key == outcome.outcome_key
        with pytest.raises(OutcomeConflictError):
            registry.add_outcome(tampered)

    def test_outcome_key_spans_event_variant_and_policy_fingerprint(self):
        policy = frozen_policy()
        outcome = make_outcome(policy)
        key = outcome.outcome_key
        assert key[0] == "src-1"
        assert key[1] == "H2_RSI_TURN_1"
        assert key[2] == policy.policy_fingerprint()
