"""Issue #298 / #312 — OutcomePolicy tests (D-1..D-12 decision object).

Covers the three-state decision vocabulary (PENDING_OWNER_DECISION /
PRELIMINARY_CANDIDATE_NOT_FROZEN / FROZEN), the requirement that FROZEN is
only reachable through an EXPLICIT approved fingerprint, deterministic
fingerprints, and the refusal vocabulary (``NOT_READY_PENDING_DECISION``).
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h2_outcome_v1.costs import (
    CostModel,
    CostModelError,
)
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    BASELINE_ORIGINAL_SIGNAL,
    DECISION_FROZEN,
    DECISION_PENDING,
    DECISION_PRELIMINARY,
    DECISION_STATUSES,
    DENOM_ALL_SOURCE,
    ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    EXIT_RULE_STOP_TARGET_OR_HORIZON,
    GAP_THROUGH_FILL_AT_OPEN,
    GEOMETRY_RE_DERIVED,
    GEOMETRY_REBASED_AT_ENTRY,
    MISSED_STAT_ZERO_NET,
    MISSED_WINNER_BASELINE_NET_R,
    MISSED_WINNER_REPORT_COUNT_AND_LIST,
    NORM_R_STOP_DISTANCE,
    PATH_WICK,
    RESULT_STATUS_NOT_READY_PENDING_DECISION,
    SLOT_D1,
    SLOT_D12,
    TIE_BREAK_STOP_FIRST,
    BaselineSpec,
    Decision,
    DenominatorSpec,
    EntryDeadline,
    EntrySpec,
    ExitRule,
    FillPolicy,
    MaeMfeSpec,
    MissedWinnerRule,
    OutcomePolicy,
    OutcomePolicyError,
    SlTpGeometryPolicy,
    StatisticalAcceptanceRule,
)

ALL_SLOTS = (
    "d_1_entry_price_class",
    "d_2_fill_missed_entry_policy",
    "d_3_cost_model",
    "d_4_entry_deadline",
    "d_5_sl_tp_geometry",
    "d_6_exit_rule",
    "d_7_baseline_definition",
    "d_8_net_er_denominator",
    "d_9_mae_mfe_conventions",
    "d_10_missed_winner_rule",
    "d_11_statistical_acceptance_rule",
    "d_12_policy_freeze",
)


def cost_model() -> CostModel:
    return CostModel(
        fee_model="FEE_FIXED_BPS_PER_SIDE",
        fee_bps_per_side=5.0,
        fee_data_source="TEST_FEE_SCHEDULE",
        slippage_model="SLIPPAGE_FIXED_BPS_PER_SIDE",
        slippage_bps_per_side=2.0,
        slippage_data_source="TEST_SLIPPAGE_ASSUMPTION",
        funding_model="FUNDING_NONE_EXPLICIT_ZERO",
        funding_bps_per_interval=0.0,
        funding_interval_ms=28_800_000,
        funding_data_source="TEST_FUNDING_ASSUMPTION",
    )


def fully_specified() -> OutcomePolicy:
    """A complete PRELIMINARY_CANDIDATE policy (NOT frozen)."""
    policy = OutcomePolicy()
    policy = policy.with_preliminary(
        SLOT_D1, EntrySpec(price_class=ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN)
    )
    policy = policy.with_preliminary(
        "d_2_fill_missed_entry_policy",
        FillPolicy(
            gap_through_policy=GAP_THROUGH_FILL_AT_OPEN,
            missed_entry_stat_policy=MISSED_STAT_ZERO_NET,
        ),
    )
    policy = policy.with_preliminary("d_3_cost_model", cost_model())
    policy = policy.with_preliminary(
        "d_4_entry_deadline", EntryDeadline(max_delay_ms=30 * 60 * 1000)
    )
    policy = policy.with_preliminary(
        "d_5_sl_tp_geometry", SlTpGeometryPolicy(mode=GEOMETRY_REBASED_AT_ENTRY)
    )
    policy = policy.with_preliminary(
        "d_6_exit_rule",
        ExitRule(
            exit_rule=EXIT_RULE_STOP_TARGET_OR_HORIZON,
            horizon_minutes=240,
            same_candle_tie_break=TIE_BREAK_STOP_FIRST,
        ),
    )
    policy = policy.with_preliminary(
        "d_7_baseline_definition", BaselineSpec(mode=BASELINE_ORIGINAL_SIGNAL)
    )
    policy = policy.with_preliminary(
        "d_8_net_er_denominator", DenominatorSpec(definition=DENOM_ALL_SOURCE)
    )
    policy = policy.with_preliminary(
        "d_9_mae_mfe_conventions",
        MaeMfeSpec(path_sampling=PATH_WICK, normalization=NORM_R_STOP_DISTANCE),
    )
    policy = policy.with_preliminary(
        "d_10_missed_winner_rule",
        MissedWinnerRule(
            definition=MISSED_WINNER_BASELINE_NET_R,
            min_net_r=0.0,
            reporting=MISSED_WINNER_REPORT_COUNT_AND_LIST,
        ),
    )
    policy = policy.with_preliminary(
        "d_11_statistical_acceptance_rule",
        StatisticalAcceptanceRule(
            min_paired_n=10,
            evaluation_cadence="WEEKLY_RESEARCH_REVIEW",
            decision_rule="PAIRED_MEAN_NET_R_DIFFERENCE_VS_ZERO",
            declared_before_outcomes_inspected=True,
        ),
    )
    return policy


class TestDecisionVocabulary:
    def test_every_slot_defaults_to_pending_owner_decision(self):
        policy = OutcomePolicy()
        assert tuple(policy.slots()) == ALL_SLOTS
        for name, decision in policy.slots().items():
            assert decision.status == DECISION_PENDING, name
            assert decision.value is None, name
        assert policy.policy_status == DECISION_PENDING
        assert len(policy.missing_slots()) == 12

    def test_three_states_are_exactly_the_documented_vocabulary(self):
        assert DECISION_STATUSES == (
            "PENDING_OWNER_DECISION",
            "PRELIMINARY_CANDIDATE_NOT_FROZEN",
            "FROZEN",
        )

    def test_pending_decision_may_not_carry_a_value(self):
        with pytest.raises(OutcomePolicyError):
            Decision(value=1, status=DECISION_PENDING)

    def test_specified_decision_must_carry_a_value(self):
        with pytest.raises(OutcomePolicyError):
            Decision(value=None, status=DECISION_PRELIMINARY)
        with pytest.raises(OutcomePolicyError):
            Decision(value=None, status=DECISION_FROZEN)

    def test_unknown_status_rejected(self):
        with pytest.raises(OutcomePolicyError):
            Decision(value=1, status="APPROVED")

    def test_policy_status_is_preliminary_until_frozen(self):
        policy = fully_specified()
        assert policy.policy_status == DECISION_PRELIMINARY
        assert not policy.is_ready_for_aggregates()


class TestFreezeRequiresApprovedFingerprint:
    def test_freeze_needs_an_explicit_approved_fingerprint(self):
        policy = fully_specified()
        with pytest.raises(OutcomePolicyError):
            policy.freeze("")
        with pytest.raises(OutcomePolicyError):
            policy.freeze("0" * 64)

    def test_freeze_refuses_pending_decisions(self):
        incomplete = OutcomePolicy()
        with pytest.raises(OutcomePolicyError):
            incomplete.freeze(incomplete.spec_fingerprint())

    def test_freeze_with_matching_fingerprint_is_the_only_path_to_frozen(self):
        policy = fully_specified()
        approved = policy.spec_fingerprint()
        frozen = policy.freeze(approved)
        assert frozen.policy_status == DECISION_FROZEN
        assert frozen.is_ready_for_aggregates()
        for name in ALL_SLOTS[:11]:
            assert frozen.slots()[name].status == DECISION_FROZEN, name
        assert frozen.slots()[SLOT_D12].status == DECISION_FROZEN
        record = frozen.slots()[SLOT_D12].value
        assert record.approved_fingerprint == approved
        assert record.spec_fingerprint == approved

    def test_frozen_policy_is_immutable(self):
        frozen = fully_specified().freeze(
            fully_specified().spec_fingerprint()
        )
        with pytest.raises(OutcomePolicyError):
            frozen.with_preliminary(
                "d_4_entry_deadline", EntryDeadline(max_delay_ms=1)
            )

    def test_with_decision_cannot_forge_frozen_status(self):
        policy = fully_specified()
        with pytest.raises(OutcomePolicyError):
            policy.with_decision(
                SLOT_D12,
                Decision(value={"approved_fingerprint": "x"},
                         status=DECISION_FROZEN),
            )


class TestFingerprints:
    def test_spec_fingerprint_is_deterministic(self):
        assert fully_specified().spec_fingerprint() == (
            fully_specified().spec_fingerprint()
        )

    def test_spec_fingerprint_changes_with_any_decision_value(self):
        base = fully_specified()
        changed = base.with_preliminary(
            "d_4_entry_deadline", EntryDeadline(max_delay_ms=31 * 60 * 1000)
        )
        assert base.spec_fingerprint() != changed.spec_fingerprint()

    def test_policy_fingerprint_covers_status_and_freeze_record(self):
        policy = fully_specified()
        frozen_a = policy.freeze(policy.spec_fingerprint())
        frozen_b = fully_specified().freeze(fully_specified().spec_fingerprint())
        # the freeze record and the statuses enter the policy fingerprint
        assert policy.policy_fingerprint() != frozen_a.policy_fingerprint()
        # identical specs + identical approval => identical fingerprint
        assert frozen_a.policy_fingerprint() == frozen_b.policy_fingerprint()

    def test_spec_fingerprint_excludes_the_freeze_record(self):
        policy = fully_specified()
        frozen = policy.freeze(policy.spec_fingerprint())
        assert policy.spec_fingerprint() == frozen.spec_fingerprint()

    def test_serialization_is_json_safe(self):
        payload = fully_specified().to_dict()
        assert payload["outcome_policy_version"].startswith("H2_OUTCOME_POLICY")
        assert set(payload["decisions"]) == set(ALL_SLOTS)


class TestRefusalVocabulary:
    def test_refusal_status_is_the_documented_string(self):
        assert RESULT_STATUS_NOT_READY_PENDING_DECISION == (
            "NOT_READY_PENDING_DECISION"
        )

    def test_missing_slots_lists_every_pending_decision(self):
        assert OutcomePolicy().missing_slots() == ALL_SLOTS


class TestValueObjects:
    def test_entry_spec_limit_class_requires_level(self):
        with pytest.raises(OutcomePolicyError):
            EntrySpec(price_class=ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL)

    def test_cost_model_requires_data_source_labels(self):
        with pytest.raises(CostModelError):
            CostModel(
                fee_model="FEE_FIXED_BPS_PER_SIDE",
                fee_bps_per_side=5.0,
                fee_data_source="",
                slippage_model="SLIPPAGE_FIXED_BPS_PER_SIDE",
                slippage_bps_per_side=2.0,
                slippage_data_source="TEST",
                funding_model="FUNDING_NONE_EXPLICIT_ZERO",
                funding_bps_per_interval=0.0,
                funding_interval_ms=28_800_000,
                funding_data_source="TEST",
            )

    def test_acceptance_rule_requires_declaration_before_outcomes(self):
        with pytest.raises(OutcomePolicyError):
            StatisticalAcceptanceRule(
                min_paired_n=10,
                evaluation_cadence="WEEKLY",
                decision_rule="RULE",
                declared_before_outcomes_inspected=False,
            )

    def test_geometry_re_derived_requires_explicit_distances(self):
        with pytest.raises(OutcomePolicyError):
            SlTpGeometryPolicy(mode=GEOMETRY_RE_DERIVED)
        with pytest.raises(OutcomePolicyError):
            SlTpGeometryPolicy(
                mode=GEOMETRY_RE_DERIVED, stop_distance=1.0
            )
        ok = SlTpGeometryPolicy(
            mode=GEOMETRY_RE_DERIVED, stop_distance=1.0, target_distance=2.0
        )
        assert ok.stop_distance == 1.0

    def test_exit_rule_requires_an_explicit_tie_break(self):
        with pytest.raises(OutcomePolicyError):
            ExitRule(
                exit_rule=EXIT_RULE_STOP_TARGET_OR_HORIZON,
                horizon_minutes=240,
                same_candle_tie_break="SOME_DEFAULT",
            )

    def test_rebased_geometry_forbids_inline_distances(self):
        with pytest.raises(OutcomePolicyError):
            SlTpGeometryPolicy(
                mode=GEOMETRY_REBASED_AT_ENTRY, stop_distance=1.0
            )
