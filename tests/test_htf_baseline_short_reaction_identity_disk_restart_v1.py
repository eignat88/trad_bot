"""#176 candidate reaction groups and disk resume tests."""
import hashlib
import json
from pathlib import Path
from app.models import Candle
from tools.research.htf_baseline_short_reaction_cluster_identity_v1 import reaction_clusters
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
STEP=300000
BASE=1800000000000 // 3600000 * 3600000


def event(i, price, level):
    return {"event_id":f"event-{level}", "episode_id":f"episode-{level}",
            "level_id":level, "known_at_ms":i*STEP,
            "level_price_at_touch":price}


def test_transitive_cluster_preserves_three_original_events():
    e=[event(10,100,"A"),event(10,100.15,"B"),event(10,100.30,"C")]
    r=reaction_clusters(e)
    assert r["raw_events"]==3
    assert r["cluster_count"]==1
    assert r["events_in_multi_clusters"]==3
    assert len(r["clusters"][0]["member_episode_ids"])==3


def test_cluster_is_order_invariant():
    e=[event(10,100,"A"),event(10,100.1,"B"),event(20,105,"C")]
    assert reaction_clusters(e)==reaction_clusters(list(reversed(e)))


def test_exact_boundary_is_scale_invariant():
    for scale in (1.0, 10.0, 25.0):
        result = reaction_clusters([
            event(10, 100.0 * scale, "A"),
            event(10, 100.2 * scale, "B"),
        ])
        assert result["cluster_count"] == 1
        assert result["candidate_edges"] == 1


def test_just_outside_exact_boundary_stays_separate():
    result = reaction_clusters([
        event(10, 100.0, "A"),
        event(10, 100.201, "B"),
    ])
    assert result["cluster_count"] == 2
    assert result["candidate_edges"] == 0


def test_cluster_ids_follow_fixed_member_identity_at_boundary():
    e=[event(10,100.0,"A"),event(10,100.2,"B")]
    result=reaction_clusters(e)
    expected=hashlib.sha256(b"event-A|event-B").hexdigest()
    assert result["clusters"][0]["reaction_cluster_id"]==expected


def test_disk_checkpoint_resume_matches_uninterrupted(tmp_path):
    candles=[Candle(BASE+i*STEP,100,101,99,100,10) for i in range(420)]
    start,end=BASE+300*STEP,BASE+400*STEP
    baseline=replay("BTCUSDT",candles,start,end)
    checkpoint=tmp_path/"state.json"
    replay("BTCUSDT",candles,start,end,stop_at_ms=BASE+350*STEP,
           save_checkpoint=checkpoint)
    assert checkpoint.exists()
    restored=replay("BTCUSDT",candles,start,end,resume_checkpoint=checkpoint)
    for key in ("emissions","counters","total_episodes_including_warmup",
                "episode_state_counts_including_warmup","pending_at_end","integrity"):
        assert baseline[key]==restored[key]
