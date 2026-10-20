"""#176 final read-only readiness fail-closed gate."""
import pytest
from tools.research.htf_baseline_short_final_readiness_audit_v1 import inspect_pair,report

def sample():
    return {"episodes":{"emissions":[{"event_id":"e1"}],
        "integrity":{"violations":0,"result":"PASS"},
        "pending_at_end":1,
        "pending_impact":{"eligibility_changes":2,
            "eligibility_loss_with_pending":[{"episode_id":"e1","level_id":"L"},
                                             {"episode_id":"e1","level_id":"L"}],
            "confirmations_after_eligibility_loss":[{"event_id":"e1","level_id":"L"}]}},
        "clusters":{"clusters":{"c1":{"reaction_cluster_id":"c1",
                                     "member_event_ids":["e1"]}},
            "assignments":{"e1":"c1"}}}

def test_unique_episode_counts_not_misrepresented_as_records():
    r=inspect_pair("BTCUSDT",sample())
    assert r["loss_with_pending_records"]==2
    assert r["unique_impacted_episode_ids"]==1
    assert r["post_loss_unique_event_ids"]==1

def test_duplicate_emissions_fail_closed():
    x=sample()
    x["episodes"]["emissions"].append({"event_id":"e1"})
    with pytest.raises(ValueError,match="duplicate"):inspect_pair("BTCUSDT",x)

def test_orphan_confirmation_impact_fails_closed():
    x=sample()
    x["episodes"]["pending_impact"]["confirmations_after_eligibility_loss"][0]["level_id"]="Q"
    with pytest.raises(ValueError,match="without recorded loss"):inspect_pair("BTCUSDT",x)

def test_missing_cluster_assignment_fails_closed():
    x=sample()
    x["clusters"]["assignments"]={}
    with pytest.raises(ValueError,match="assignment"):inspect_pair("BTCUSDT",x)

def test_event_integrity_fail_closed():
    x=sample()
    x["episodes"]["integrity"]["violations"]=1
    with pytest.raises(ValueError,match="integrity"):inspect_pair("BTCUSDT",x)

def test_assignment_to_missing_cluster_fails_closed():
    x=sample()
    x["clusters"]["assignments"]["e1"]="missing"
    with pytest.raises(ValueError,match="missing cluster"):inspect_pair("BTCUSDT",x)

def test_event_absent_from_assigned_cluster_fails_closed():
    x=sample()
    x["episodes"]["emissions"].append({"event_id":"e2"})
    x["clusters"]["assignments"]["e2"]="c1"
    x["clusters"]["clusters"]["c1"]["member_event_ids"]=["e1"]
    with pytest.raises(ValueError,match="membership/emission"):inspect_pair("BTCUSDT",x)

def test_cluster_member_without_assignment_fails_closed():
    x=sample()
    x["episodes"]["emissions"].append({"event_id":"e2"})
    x["clusters"]["clusters"]["c1"]["member_event_ids"]=["e1","e2"]
    with pytest.raises(ValueError,match="assignment/event"):inspect_pair("BTCUSDT",x)

def test_member_assigned_to_other_cluster_fails_closed():
    x=sample()
    x["episodes"]["emissions"].append({"event_id":"e2"})
    x["clusters"]["clusters"]["c1"]["member_event_ids"]=["e1","e2"]
    x["clusters"]["clusters"]["c2"]={"reaction_cluster_id":"c2","member_event_ids":["e2"]}
    x["clusters"]["assignments"]["e2"]="c1"
    with pytest.raises(ValueError,match="duplicate cluster member"):inspect_pair("BTCUSDT",x)

def test_duplicate_member_id_fails_closed():
    x=sample()
    x["clusters"]["clusters"]["c1"]["member_event_ids"]=["e1","e1"]
    with pytest.raises(ValueError,match="duplicate cluster member"):inspect_pair("BTCUSDT",x)

def test_orphan_cluster_fails_closed():
    x=sample()
    x["clusters"]["clusters"]["c2"]={"reaction_cluster_id":"c2","member_event_ids":["e1"]}
    with pytest.raises(ValueError,match="duplicate cluster member"):inspect_pair("BTCUSDT",x)

def test_ambiguous_none_assignment_is_not_accepted_for_atomic_artifact():
    x=sample()
    x["clusters"]["assignments"]["e1"]=None
    with pytest.raises(ValueError,match="invalid cluster assignment"):inspect_pair("BTCUSDT",x)

def test_malformed_artifact_fails_closed():
    x=sample()
    x["clusters"]["assignments"]=None
    with pytest.raises(ValueError,match="assignments must be an object"):inspect_pair("BTCUSDT",x)
