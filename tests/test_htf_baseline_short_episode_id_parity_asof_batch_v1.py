"""#176 parity matching and ASOF batch diagnostic tests."""
from tools.research.htf_baseline_short_episode_id_parity_root_cause_v1 import compare_events
from tools.research.htf_baseline_short_asof_batch_cluster_audit_v1 import audit_batches

def event(eid,level,touch,when,price):
    return {"event_id":eid,"episode_id":eid,"level_id":level,
            "first_touch_open_ms":touch,"known_at_ms":when,
            "level_price_at_touch":price}

def test_same_underlying_level_confirmation_different_episode_id():
    left=[event("a","L",0,900000,100)]
    right=[event("b","L",300000,900000,100)]
    r=compare_events(left,right)
    assert r["matched_same_level_confirmation"]==1
    assert r["matches"][0]["touch_shift_ms"]==300000
    assert not r["unmatched_selected"] and not r["unmatched_full"]

def test_different_level_remains_unmatched():
    r=compare_events([event("a","L",0,900000,100)],
                     [event("b","Q",0,900000,100)])
    assert r["matched_same_level_confirmation"]==0
    assert len(r["unmatched_selected"])==len(r["unmatched_full"])==1

def test_simultaneous_asof_batch_edges_are_reported_without_merge():
    events=[event("a","A",0,900000,2512.19),
            event("b","B",0,900000,2516.8),
            event("c","C",0,900000,2518.)]
    result=audit_batches("ETHUSDT",events)
    assert result["multi_event_batches"]==1
    assert result["same_asof_edges"]>=2
    assert len(result["batches"][0]["event_ids"])==3

def test_batch_inspection_order_invariance():
    events=[event("a","A",0,900000,2512.19),
            event("b","B",0,900000,2516.8)]
    assert audit_batches("ETHUSDT",events)==audit_batches("ETHUSDT",list(reversed(events)))
