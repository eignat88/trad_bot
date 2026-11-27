"""Issue #324 / H1-E.1 — source readiness: UNKNOWN→HOLD (never PASS),
BLOCK propagation, the permanent Phase A freeze hold.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

from app.research.me_entry_quality_h1e_v1.source_readiness import (
    CHECK_H1E_FREEZE_APPROVAL,
    CHECK_SRC305_COVERAGE_PROOF,
    STATUS_BLOCK,
    STATUS_HOLD,
    STATUS_PASS,
    STATUS_UNKNOWN,
    VERDICT_BLOCK,
    VERDICT_HOLD,
    build_readiness_report,
)


def _check(report, check_id):
    return next(c for c in report.checks if c.check_id == check_id)


def test_empty_evidence_is_all_unknown_and_never_pass():
    report = build_readiness_report({})
    assert report.verdict == VERDICT_HOLD
    assert report.phase_a_hold
    for check in report.checks:
        assert check.status in (STATUS_UNKNOWN, STATUS_HOLD)
        assert check.status != STATUS_PASS


def test_unsupported_checks_remain_unknown_hold():
    report = build_readiness_report({"source_schema_contract_ok": True})
    assert _check(report, "SRC305_SCHEMA_CONTRACT").status == STATUS_PASS
    # an unsupported check is UNKNOWN and keeps the verdict on HOLD
    assert _check(report, "HTF_SOURCE_AVAILABILITY").status == STATUS_UNKNOWN
    assert report.verdict == VERDICT_HOLD


def test_block_evidence_propagates_to_block_verdict():
    report = build_readiness_report({
        "source_schema_contract_ok": True,
        "source_freeze_immutable_ok": False,   # BLOCK
        "source296_lineage_obligations_ok": True,
        "source_fingerprint_stable_ok": True,
        "blind_intervals_documented": True,
        "blind_intervals": [],
        "sampled_identity_check": "PASS",
        "htf_source_available_ok": True,
        "limiter_available_ok": True,
        "h1e_freeze_approved": False,
    })
    assert _check(report, "SRC305_FREEZE_IMMUTABLE").status == STATUS_BLOCK
    assert report.verdict == VERDICT_BLOCK


def test_identity_mismatch_is_block():
    report = build_readiness_report({"sampled_identity_check": "FAIL"})
    assert _check(report, "CAPTURED_EVENT_IDENTITY").status == STATUS_BLOCK
    assert report.verdict == VERDICT_BLOCK


def test_upstream_coverage_unproven_is_hold_not_pass():
    report = build_readiness_report({"source_coverage_proof": False})
    check = _check(report, CHECK_SRC305_COVERAGE_PROOF)
    assert check.status == STATUS_HOLD
    assert "UPSTREAM_SOURCE_COVERAGE_UNPROVEN" in check.detail
    # and absent evidence stays UNKNOWN
    report2 = build_readiness_report({})
    assert _check(report2, CHECK_SRC305_COVERAGE_PROOF).status == STATUS_UNKNOWN


def test_phase_a_freeze_hold_survives_all_other_passes():
    report = build_readiness_report({
        "source_schema_contract_ok": True,
        "source_freeze_immutable_ok": True,
        "source_coverage_proof": True,
        "source296_lineage_obligations_ok": True,
        "source_fingerprint_stable_ok": True,
        "blind_intervals_documented": True,
        "blind_intervals": [
            {"from": "2026-11-24T13:31:53.365952+00:00",
             "to": "2026-11-25T07:14:41.230516+00:00"},
        ],
        "sampled_identity_check": "PASS",
        "htf_source_available_ok": True,
        "limiter_available_ok": True,
        "h1e_freeze_approved": True,
    })
    freeze_check = _check(report, CHECK_H1E_FREEZE_APPROVAL)
    # even a claimed approval stays HOLD — Phase A never reaches PASS
    assert freeze_check.status == STATUS_HOLD
    assert report.verdict == VERDICT_HOLD
    assert report.phase_a_hold


def test_undocumented_blind_intervals_are_hold():
    report = build_readiness_report({"blind_intervals_documented": False})
    assert _check(report, "BLIND_INTERVALS").status == STATUS_HOLD
    assert report.verdict == VERDICT_HOLD


def test_report_serializes_evidence_for_audit():
    report = build_readiness_report({"source_freeze_immutable_ok": True})
    document = report.to_dict()
    assert document["verdict"] == VERDICT_HOLD
    assert document["phase_a_hold"] is True
    assert {c["check_id"] for c in document["checks"]} >= {
        "SRC305_SCHEMA_CONTRACT", "SRC305_FREEZE_IMMUTABLE",
        "SRC296_LINEAGE_OBLIGATIONS", "SOURCE_FINGERPRINT", "BLIND_INTERVALS",
        "CAPTURED_EVENT_IDENTITY", "HTF_SOURCE_AVAILABILITY",
        "LIMITER_AVAILABILITY", "H1E_FREEZE_APPROVAL",
    }
