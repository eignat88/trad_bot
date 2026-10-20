"""#176 full universe and first-seen anchor contracts."""
import pytest
from app.models import Candle
from app.research.htf_baseline_short_causal_level_adapter_v1 import (
    causal_resistance_universe, causal_resistance_snapshot,
)
from tools.research.htf_baseline_short_first_seen_cluster_v1 import FirstSeenReactionClusters

HOUR = 3600000
FIVE = 300000
BASE = 1800000000000 // HOUR * HOUR


def candle(t, high=101, low=99, close=100):
    return Candle(t, 100., float(high), float(low), float(close), 10.)


def event(eid, when, price):
    return {"event_id":eid,"episode_id":eid,"known_at_ms":when,
            "level_price_at_touch":price}


def test_full_universe_retains_nearest_snapshot_compatibility():
    htf = [candle(BASE-(50-i)*HOUR, high=100+i%3) for i in range(44)]
    exe = [candle(BASE+i*FIVE) for i in range(200)]
    asof = BASE+200*FIVE
    universe = causal_resistance_universe(htf,exe,asof)
    nearest = causal_resistance_snapshot(htf,exe,asof)
    assert nearest == (universe[0] if universe else None)
    assert all(level.available_ms <= asof for level in universe)
    assert len({level.level_id for level in universe}) == len(universe)


def test_first_seen_cluster_anchor_is_unchanged_after_later_event():
    s = FirstSeenReactionClusters("BTCUSDT")
    a = s.observe([event("A", 3*FIVE, 100)])[0]["reaction_cluster_id"]
    b = s.observe([event("B", 4*FIVE, 100.1)])[0]["reaction_cluster_id"]
    assert a == b
    assert s.clusters[a]["anchor_event_id"] == "A"


def test_first_seen_prefix_replay_is_stable():
    events=[event("A", 3*FIVE, 100),event("B", 4*FIVE, 100.1),event("C", 10*FIVE, 103)]
    one=FirstSeenReactionClusters("ETHUSDT")
    per_bar=FirstSeenReactionClusters("ETHUSDT")
    assert one.observe(events) == sum((per_bar.observe([e]) for e in events), [])
    assert one.result() == per_bar.result()


def test_fail_closed_bridge_does_not_merge_old_anchor_ids():
    s=FirstSeenReactionClusters("ETHUSDT")
    a=s.observe([event("A", 10*FIVE, 100)])[0]["reaction_cluster_id"]
    b=s.observe([event("B", 10*FIVE, 100.35)])[0]["reaction_cluster_id"]
    assert a != b
    conflict=s.observe([event("bridge",10*FIVE,100.18)])[0]
    assert conflict["status"]=="AMBIGUOUS_BRIDGE"
    assert conflict["reaction_cluster_id"] is None
    assert len(s.clusters)==2


def test_first_seen_bps_geometry_is_scale_independent():
    scale_cases = ((100.0, 100.2), (1000.0, 1002.0), (2500.0, 2505.0))
    for scale, (anchor, attached) in enumerate(scale_cases, start=1):
        s = FirstSeenReactionClusters(f"SCALE{scale}USDT")
        anchor_id = s.observe([event("A", FIVE, anchor)])[0]["reaction_cluster_id"]
        attached_id = s.observe([event("B", 2 * FIVE, attached)])[0]["reaction_cluster_id"]
        assert attached_id == anchor_id
        assert s.result()["cluster_count"] == 1
        assert s.result()["assigned_events"] == 2


def test_first_seen_bps_geometry_rejects_values_outside_boundary():
    s = FirstSeenReactionClusters("BTCUSDT")
    anchor_id = s.observe([event("A", FIVE, 100.0)])[0]["reaction_cluster_id"]
    outside_id = s.observe([event("B", 2 * FIVE, 100.201)])[0]["reaction_cluster_id"]
    assert outside_id != anchor_id
    assert s.result()["cluster_count"] == 2
    assert s.result()["ambiguous_bridges"] == []


def test_repeated_delivery_is_rejected():
    s=FirstSeenReactionClusters("BTCUSDT")
    s.observe([event("A", FIVE, 100)])
    with pytest.raises(ValueError,match="duplicate"):
        s.observe([event("A", FIVE, 100)])
