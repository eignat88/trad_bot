"""Issue #176 Phase-2 mandatory test matrix (1): protocol fingerprint/immutability."""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.research.htf_baseline_short_phase2_protocol_v1 import (
    APPROVED_FREEZE_TS,
    BASE_COMMIT,
    CANDIDATE_PROTOCOL_FINGERPRINT,
    CANDIDATE_PROTOCOL_STATUS,
    EXPERIMENT_ID,
    FROZEN_PROTOCOL_FINGERPRINT,
    OPERATOR_APPROVED,
    PROTOCOL_STATUS,
    PROTOCOL_V1,
    approved_freeze_ts,
    canonical_protocol_json,
    operator_decisions_pending,
    protocol_fingerprint,
    verify_frozen_fingerprint,
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


def test_protocol_status_is_frozen_but_authorizes_nothing():
    # Issue #197: the SPECIFICATION is frozen; this is not an activation.
    assert PROTOCOL_STATUS == "FROZEN"
    assert PROTOCOL_V1["status"] == "FROZEN"


def test_final_frozen_fingerprint_is_pinned_and_reproducible():
    # deterministic across processes/runs and equal to the reviewed pin
    assert protocol_fingerprint() == FROZEN_PROTOCOL_FINGERPRINT
    assert verify_frozen_fingerprint() is True
    assert len(FROZEN_PROTOCOL_FINGERPRINT) == 64


def test_candidate_fingerprint_reproducible_by_status_field_alone():
    # the ONLY semantic delta candidate -> frozen is the status field; this is
    # the independent re-verification of both published fingerprints
    restored = copy.deepcopy(PROTOCOL_V1)
    restored["status"] = CANDIDATE_PROTOCOL_STATUS
    assert protocol_fingerprint(restored) == CANDIDATE_PROTOCOL_FINGERPRINT
    assert CANDIDATE_PROTOCOL_FINGERPRINT != FROZEN_PROTOCOL_FINGERPRINT


def test_protocol_module_carries_no_freeze_timestamp():
    # timestamp authority V1: the freeze timestamp must never live in Git —
    # merge/CI/deploy latency cannot be allowed to decide it
    assert APPROVED_FREEZE_TS is None
    assert approved_freeze_ts() is None
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", canonical_protocol_json())


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
