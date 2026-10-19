"""#176 bridge audit unit tests."""
from tools.research.htf_baseline_short_first_seen_cluster_v1 import FirstSeenReactionClusters
from tools.research.htf_baseline_short_bridge_parity_v1 import inspect
from app.models import Candle

STEP=300000
BASE=1800000000000 // 3600000 * 3600000

def ev(eid, price):
    return {"event_id":eid,"level_id":eid,"episode_id":eid,
            "known_at_ms":STEP*10,"level_price_at_touch":price}

def test_bridge_records_two_existing_anchors_without_merging():
    s=FirstSeenReactionClusters("ETHUSDT")
    a=s.observe([ev("A",100.0)])[0]["reaction_cluster_id"]
    b=s.observe([ev("B",100.35)])[0]["reaction_cluster_id"]
    result=s.observe([ev("bridge",100.18)])[0]
    assert result["status"]=="AMBIGUOUS_BRIDGE"
    assert result["reaction_cluster_id"] is None
    assert len(s.ambiguous_bridges[-1]["candidate_cluster_ids"])==2
    assert {a,b}==set(s.ambiguous_bridges[-1]["candidate_cluster_ids"])

def test_flat_history_parity_and_no_bridge():
    bars=[Candle(BASE+i*STEP,100,101,99,100,10) for i in range(420)]
    r=inspect("BTCUSDT",bars,BASE+300*STEP,BASE+400*STEP)
    assert r["event_id_parity"]
    assert r["bridge_count"]==0
    assert r["selected_only_ids"]==[]
    assert r["full_only_ids"]==[]
