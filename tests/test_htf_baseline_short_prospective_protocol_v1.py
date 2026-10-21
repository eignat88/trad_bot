"""#176 prospective protocol freeze regression checks."""
from tools.research.htf_baseline_short_prospective_protocol_v1 import PROTOCOL,checklist

def test_protocol_requires_operator_approval_and_prospective_only():
    assert PROTOCOL["status"]=="PROPOSED_NOT_OPERATOR_APPROVED"
    assert PROTOCOL["observation_boundary"]["historical_backfill_allowed"] is False
    assert PROTOCOL["eligibility"]["terminal_retirement"]=="NOT_APPROVED"
    assert PROTOCOL["atomic_asof_geometry"]["price_bps"]==20
    assert PROTOCOL["execution_edge"]=="NOT_ESTABLISHED_BY_PROTOCOL"
    assert PROTOCOL["observation_boundary"]["paper_or_live_execution"]=="NOT_PART_OF_PROTOCOL"
    assert "historical Point-B-conditioned baseline as fresh OOS" in PROTOCOL["source"]["forbidden"]
    assert PROTOCOL["automation"]["phase2_start"]=="PROHIBITED_WITHOUT_OPERATOR_APPROVAL"
    assert PROTOCOL["automation"]["merge_or_deploy"]=="PROHIBITED"
    assert PROTOCOL["automation"]["paper_or_live_execution"]=="PROHIBITED"
    assert PROTOCOL["atomic_asof_geometry"]["boundaries"].startswith("inclusive provisional baseline")

def test_freeze_checklist_does_not_automatic_go():
    result=checklist()
    assert result["operator_freeze_approval"]=="NOT_APPROVED"
    assert result["prospective_observation_start"]=="NOT_AUTHORIZED"
    assert result["terminal_level_retirement"]=="BLOCKED"
    assert result["exactly_once_external_delivery"]=="NOT_IMPLEMENTED"
    assert result["writer_stale_lock_recovery"]=="BLOCKED_OPERATOR_RECOVERY"
    assert result["asof_order_invariance"]=="LOCAL_PASS"
    assert result["asof_geometry_sensitivity"]=="VPS_VALIDATION_PENDING"
    assert result["phase2_gate"]=="NO_GO"
