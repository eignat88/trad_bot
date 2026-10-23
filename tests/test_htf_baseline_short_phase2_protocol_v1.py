"""Issue #176 Phase-2 mandatory test matrix (1): protocol fingerprint/immutability."""
from __future__ import annotations

import copy
import json

import pytest

from app.research.htf_baseline_short_phase2_protocol_v1 import (
    BASE_COMMIT,
    EXPERIMENT_ID,
    OPERATOR_APPROVED,
    PROTOCOL_STATUS,
    PROTOCOL_V1,
    canonical_protocol_json,
    operator_decisions_pending,
    protocol_fingerprint,
)

POINT_B_FREEZE = "2026-10-06T14:00:00Z"
POINT_B_IDS = {
    "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
    "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE",
}


def test_fingerprint_is_stable_and_deterministic():
    first = protocol_fingerprint()
    second = protocol_fingerprint(copy.deepcopy(PROTOCOL_V1))
    assert first == second
    assert len(first) == 64
    # canonical serialization must be key-sorted and stable across runs
    assert canonical_protocol_json() == json.dumps(
        PROTOCOL_V1, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def test_fingerprint_changes_on_any_semantic_change():
    mutated = copy.deepcopy(PROTOCOL_V1)
    mutated["geometry"]["proximity_ms"]["value"] = 300001
    assert protocol_fingerprint(mutated) != protocol_fingerprint()
    mutated = copy.deepcopy(PROTOCOL_V1)
    mutated["freeze_boundary"]["signal_time_rule"] = "eligible iff signal_time >= freeze_ts"
    assert protocol_fingerprint(mutated) != protocol_fingerprint()


def test_protocol_status_is_not_frozen_until_operator_approval():
    assert PROTOCOL_STATUS == "PROTOCOL_NOT_FROZEN"
    assert PROTOCOL_V1["status"] == "PROTOCOL_NOT_FROZEN"


def test_experiment_identity_is_distinct_from_point_b():
    assert EXPERIMENT_ID == "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
    assert EXPERIMENT_ID not in POINT_B_IDS
    assert POINT_B_FREEZE not in canonical_protocol_json()


def test_strict_freeze_semantics_declared():
    freeze = PROTOCOL_V1["freeze_boundary"]
    assert freeze["signal_time_rule"].endswith("> freeze_ts (strict)")
    assert freeze["first_touch_rule"].endswith("> freeze_ts (strict)")
    assert freeze["mismatch_or_missing_freeze"] == "REJECT_FAIL_CLOSED"
    assert freeze["historical_events_as_oos"] == "FORBIDDEN"


def test_identity_rule_forbids_mutable_counters():
    identity = PROTOCOL_V1["episode_identity"]
    assert "first_touch_open_ms" in identity["rule"]
    assert any("counter" in item for item in identity["forbidden_in_identity"])
    assert identity["stable_across"] == ["restart", "warmup length", "rolling-window shift"]


def test_terminal_retirement_policy_approved_by_operator():
    retirement = PROTOCOL_V1["lifecycle"]["level_retirement_policy"]
    assert retirement["status"] == OPERATOR_APPROVED
    assert retirement["rule"] == "LEVEL_ELIGIBILITY_LOSS_IS_NON_TERMINAL"


def test_operator_decisions_are_explicitly_tracked():
    pending = operator_decisions_pending()
    assert pending == []
    for group, key, expected in [("geometry", "proximity_ms", 300000), ("geometry", "price_bps", 20), ("maturity_gates", "minimum_confirmations", 30), ("maturity_gates", "minimum_symbols", 5), ("maturity_gates", "minimum_active_days", 14)]:
        assert PROTOCOL_V1[group][key] == {"value": expected, "status": OPERATOR_APPROVED}


def test_metrics_separation_declares_execution_out_of_scope():
    metrics = PROTOCOL_V1["metrics"]
    assert metrics["execution_expectancy"] == "OUT_OF_SCOPE_ISSUE_170"
    assert PROTOCOL_V1["parameter_optimization"]["retrospective_pnl_tuning"] == "PROHIBITED"
    assert PROTOCOL_V1["automation"]["phase2_start"] == "PROHIBITED_WITHOUT_OPERATOR_APPROVAL"
    assert PROTOCOL_V1["automation"]["observer_enabled_default"] is False


def test_base_commit_matches_audited_merge_commit():
    assert BASE_COMMIT == "4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba"
