"""#176 Phase-2 prospective protocol freeze checklist (research only)."""
PROTOCOL = {
    "issue": 176,
    "status": "PROPOSED_NOT_OPERATOR_APPROVED",
    "source": {
        "level_timeframe": "1H",
        "decision_timeframe": "5M",
        "bar_rule": "consume only fully closed candles; level available_at must be <= decision open",
        "forbidden": ["future candles", "backdating", "first-seen substitute",
                      "historical Point-B-conditioned baseline as fresh OOS"],
    },
    "episode_identity": "sha256(symbol|SHORT|level_id|first_touch_open_ms)",
    "atomic_asof_geometry": {
        "proximity_ms": 300000,
        "price_bps": 20,
        "boundaries": "inclusive provisional baseline <= 300000ms and <=20bps; strict boundary sensitivity remains VPS pending",
        "ambiguity": "multiple prior anchors fail closed without merge",
        "sensitivity_required": True,
    },
    "execution_edge": "NOT_ESTABLISHED_BY_PROTOCOL",
    "automation": {
        "phase2_start": "PROHIBITED_WITHOUT_OPERATOR_APPROVAL",
        "merge_or_deploy": "PROHIBITED",
        "paper_or_live_execution": "PROHIBITED",
    },
    "eligibility": {
        "states": ["DISCOVERED", "NOT_CURRENTLY_ELIGIBLE", "RE_ELIGIBLE"],
        "terminal_retirement": "NOT_APPROVED",
        "loss_must_delete_pending": False,
    },
    "observation_boundary": {
        "prospective_only": True,
        "historical_backfill_allowed": False,
        "requires_separate_operator_approval": True,
        "paper_or_live_execution": "NOT_PART_OF_PROTOCOL",
    },
}

def checklist():
    return {
        "closed_1h_and_5m_only": "IMPLEMENTED_RESEARCH",
        "episode_identity_stability": "IMPLEMENTED_RESEARCH",
        "asof_order_invariance": "LOCAL_PASS",
        "asof_geometry_sensitivity": "VPS_VALIDATION_PENDING",
        "terminal_level_retirement": "BLOCKED",
        "exactly_once_external_delivery": "NOT_IMPLEMENTED",
        "writer_stale_lock_recovery": "BLOCKED_OPERATOR_RECOVERY",
        "prospective_observation_start": "NOT_AUTHORIZED",
        "operator_freeze_approval": "NOT_APPROVED",
        "phase2_gate": "NO_GO",
    }
