"""#176 overlap candidate and checkpoint restart audit tests."""
from app.models import Candle
from tools.research.htf_baseline_short_event_cluster_restart_audit_v1 import cluster_events, analyze
STEP=300000
BASE=1800000000000 // 3600000 * 3600000

def event(i, price, level):
    return {"event_id":f"e{i}","episode_id":f"ep{i}","level_id":level,
            "known_at_ms":i*STEP,"level_price_at_touch":price}

def test_nearby_levels_same_confirmation_flagged():
    r=cluster_events([event(10,100.0,"a"),event(10,100.1,"b")])
    assert r["candidate_pairs"]==1 and r["events_in_pairs"]==2
    assert not r["pairs"][0]["same_level"]

def test_separate_time_not_clustered():
    assert cluster_events([event(10,100,"a"),event(13,100.1,"b")])["candidate_pairs"]==0

def test_separate_price_not_clustered():
    assert cluster_events([event(10,100,"a"),event(10,105,"b")])["candidate_pairs"]==0

def test_snapshot_restart_matches_flat_historical_replay():
    bars=[Candle(BASE+i*STEP,100,101,99,100,10) for i in range(420)]
    result=analyze("BTCUSDT",bars,BASE+300*STEP,BASE+400*STEP)
    assert result["restart_equivalence"]=="PASS"
    assert result["confirmed_events"]==0
