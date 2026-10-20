"""#176 stable causal identity and atomic same-ASOF assignment."""
import pytest
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters
STEP=300000

def event(k,when,price):
    return {"event_id":k,"episode_id":k,"known_at_ms":when,"level_price_at_touch":price}

def test_episode_id_independent_of_unrelated_prior_level_history():
    a=MultiLevelEpisodes("BTCUSDT")
    a.advance(open_ms=0,level_contacts={"L":(100,0,True)})
    aid=a.pending["L"]
    b=MultiLevelEpisodes("BTCUSDT")
    b.sequence["L"]=99
    b.advance(open_ms=0,level_contacts={"L":(100,0,True)})
    assert b.pending["L"]==aid

def test_same_level_recontact_has_distinct_first_touch_identity():
    s=MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0,level_contacts={"L":(100,0,True)})
    first=s.pending["L"]
    s.advance(open_ms=STEP,level_contacts={"L":(100,0,False)})
    s.advance(open_ms=2*STEP,level_contacts={"L":(100,0,True)})
    assert s.pending["L"]!=first
    assert s.episodes[first]["state"]=="UNRESOLVED"

def test_default_excludes_same_asof_prior_but_explicit_diagnostic_mode_allows_it():
    c = AtomicAsofClusters("BTCUSDT")
    c.observe_batch([event("a", STEP, 100.0), event("b", STEP, 100.2)])

    # A repeated ASOF must be rejected for atomic batch integrity.
    with pytest.raises(ValueError, match="nonmonotonic ASOF"):
        c.observe_batch([event("c", STEP, 100.1)])

    # Diagnostic flag must not bypass chronology protection.
    with pytest.raises(ValueError, match="nonmonotonic ASOF"):
        c.observe_batch(
            [event("c", STEP, 100.1)],
            include_same_asof_prior=True,
        )

    # A later ASOF is allowed.
    later = c.observe_batch([event("c", 2 * STEP, 100.1)])
    assert len(later) == 1



def test_batch_three_levels_one_atomic_cluster_and_order_invariant():
    batch=[event("a",STEP,2512.19),event("b",STEP,2516.8),event("c",STEP,2518)]
    x=AtomicAsofClusters("ETHUSDT")
    y=AtomicAsofClusters("ETHUSDT")
    left=x.observe(batch);right=y.observe(list(reversed(batch)))
    assert left==right
    assert len({r["reaction_cluster_id"] for r in left})==1
    assert len(x.clusters)==1
    assert all(r["status"]=="PROVISIONAL_NEW" for r in left)

def test_two_separate_prior_anchors_fail_closed_for_bridge():
    c=AtomicAsofClusters("ETHUSDT")
    c.observe_batch([event("a",STEP,100),event("b",STEP,100.35)])
    r=c.observe_batch([event("bridge",2*STEP,100.18)])
    assert r[0]["status"]=="AMBIGUOUS_PRIOR_ANCHORS"
    assert r[0]["reaction_cluster_id"] is None
    assert len(c.clusters)==2

def test_same_asof_groups_are_not_prior_anchors_by_default():
    c=AtomicAsofClusters("BTCUSDT")
    r=c.observe_batch([event("a",STEP,100),event("b",STEP,103)])
    assert len({x["reaction_cluster_id"] for x in r})==2

def test_exact_geometry_boundaries_are_not_clustered():
    c=AtomicAsofClusters("BTCUSDT")
    batch=[event("a",STEP,100.0),event("b",STEP,100.2)]
    left=c.observe_batch(batch)
    right=AtomicAsofClusters("BTCUSDT").observe_batch(list(reversed(batch)))
    assert left==right
    assert len({r["reaction_cluster_id"] for r in left})==2
    c.observe_batch([event("c",2*STEP,100.0)])
    assert len(c.clusters)==2
    result=c.observe_batch([event("d",3*STEP,100.2)])
    assert result[0]["status"]=="PROVISIONAL_NEW"
    assert len(c.clusters)==3


def test_checkpoint_preserves_events_for_sensitivity_reanalysis():
    c=AtomicAsofClusters("BTCUSDT")
    e=event("a",STEP,100.0)
    c.observe([e])
    restored=AtomicAsofClusters.restore(c.checkpoint())
    assert restored.events=={"a":e}
    with pytest.raises(ValueError,match="nonmonotonic"):
        restored.observe([event("b",STEP-1,100.0)])
    with pytest.raises(ValueError,match="duplicate"):
        restored.observe([event("a",2*STEP,100.0)])


def test_duplicate_and_nonmonotonic_batches_rejected():
    c=AtomicAsofClusters("BTCUSDT")
    c.observe_batch([event("a",STEP,100)])
    with pytest.raises(ValueError,match="nonmonotonic"):
        c.observe_batch([event("b",STEP-1,100)])
    with pytest.raises(ValueError,match="duplicate"):
        c.observe_batch([event("a",2*STEP,100)])
def test_second_batch_at_same_asof_is_rejected():
    c = AtomicAsofClusters("BTCUSDT")
    c.observe_batch([event("a", STEP, 100.0)])

    with pytest.raises(ValueError, match="nonmonotonic ASOF"):
        c.observe_batch([event("b", STEP, 100.1)])

    assert "b" not in c.assignments
    assert "b" not in c.events