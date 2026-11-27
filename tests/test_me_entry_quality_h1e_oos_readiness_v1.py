"""Issue #324 / H1-E.6 — OOS readiness: pre-registration template only,
NO outcome computation, unchanged control, Phase B/C hold.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import inspect

import pytest

import app.research.me_entry_quality_h1e_v1.oos_readiness as oos
from app.research.me_entry_quality_h1e_v1.oos_readiness import (
    OUTCOME_COMPUTATION,
    REGIME_COMBINATIONS,
    TEMPLATE_STATUS,
    assert_proposal_only,
    build_preregistration_template,
)


def test_template_is_proposal_only_with_phase_b_c_hold():
    template = build_preregistration_template()
    assert template["status"] == TEMPLATE_STATUS == "PROPOSAL_ONLY_NOT_FROZEN"
    assert template["outcome_computation"] == OUTCOME_COMPUTATION == \
        "FORBIDDEN_IN_PHASE_A"
    hold = template["phase_bc_hold"]
    assert "SEPARATE OWNER GO" in hold["phase_b"]
    assert "SEPARATE OWNER GO" in hold["phase_c"]
    assert "NOT authorized" in hold["merge_or_deploy"]
    assert_proposal_only(template)


def test_three_cohorts_and_nine_combinations():
    template = build_preregistration_template()
    assert set(template["cohorts"]) == {
        "H1_BULLISH", "H1_BEARISH", "H1_NEUTRAL"}
    assert [c["combination"] for c in template["htf_combinations"]] == list(
        REGIME_COMBINATIONS)
    assert len(REGIME_COMBINATIONS) == 9
    for entry in template["htf_combinations"]:
        assert "never merged" in entry["definition"]


def test_control_is_unchanged_and_paired_on_identical_source_event_key():
    template = build_preregistration_template()
    control = template["control"]
    assert control["definition"] == "UNCHANGED_BASELINE_CONTROL"
    assert "source_event_key" in control["pairing"]
    assert "FORBIDDEN" in control["tuning"]
    for cohort in template["cohorts"].values():
        assert cohort["identity"] == "original source_event_key (verbatim)"


def test_exclusions_cover_freeze_unverified_and_soxsusdt_qa():
    template = build_preregistration_template()
    for cohort in template["cohorts"].values():
        exclusions = " ".join(cohort["exclusions"])
        assert "pre-freeze" in exclusions
        assert "UNVERIFIED" in exclusions
        assert "SOXSUSDT QA example" in exclusions


def test_thresholds_and_execution_model_are_pre_registered_placeholders():
    template = build_preregistration_template()
    thresholds = template["sample_thresholds"]
    assert thresholds["min_events_per_cohort"] == "TBD_FROZEN"
    assert thresholds["uncertainty_policy"].startswith("TBD_FROZEN")
    execution = template["execution_model"]
    assert execution["fees_bps"] == "TBD_FROZEN"
    assert execution["slippage_model"] == "TBD_FROZEN"
    assert execution["followup_windows"]
    assert set(template["metrics"]) >= {
        "MAE_R", "MFE_R", "MISSED_WINNERS", "TIME_TO_PEAK"}
    assert any("failure_strata" in s for s in template["strata"])


def test_no_outcome_computation_exists_in_the_module():
    # the module defines templates and guards ONLY — no evaluator, no math
    forbidden = (
        "evaluate", "compute_outcome", "calculate", "mean", "median",
        "profit", "win_rate", "sharpe",
    )
    for name, obj in vars(oos).items():
        if inspect.isfunction(obj):
            assert not any(token in name.lower() for token in forbidden), name
    source = inspect.getsource(oos)
    assert "import numpy" not in source
    assert "import statistics" not in source
    assert "import pandas" not in source


def test_assert_proposal_only_fails_closed():
    template = build_preregistration_template()
    mutated = dict(template, status="FROZEN")
    with pytest.raises(ValueError):
        assert_proposal_only(mutated)
    mutated2 = dict(template, outcome_computation="COMPUTED")
    with pytest.raises(ValueError):
        assert_proposal_only(mutated2)
    mutated3 = dict(template, phase_bc_hold={})
    with pytest.raises(ValueError):
        assert_proposal_only(mutated3)
