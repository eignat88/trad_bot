"""#176 ASOF-batch ambiguity diagnostics, no prospective dedup decisions."""
from collections import defaultdict
from tools.research.htf_baseline_short_first_seen_cluster_v1 import FirstSeenReactionClusters

def audit_batches(symbol, events, *, price_bps=20):
    batches=defaultdict(list)
    for e in events:
        batches[e["known_at_ms"]].append(e)
    state=FirstSeenReactionClusters(symbol)
    cases=[]
    for when in sorted(batches):
        batch=sorted(batches[when],key=lambda e:e["event_id"])
        # Diagnose connected components *within this as-of batch* before any
        # same-asof event is allowed to create a cluster anchor.
        edges=[]
        for i,a in enumerate(batch):
            for b in batch[i+1:]:
                pa,pb=a["level_price_at_touch"],b["level_price_at_touch"]
                if pa<=0 or pb<=0:raise ValueError("invalid resistance price")
                if 10000*abs(pa-pb)/min(pa,pb)<=price_bps:
                    edges.append([a["event_id"],b["event_id"]])
        # For existing anchors, evaluate candidate sets against PRE-BATCH state.
        candidate_sets={}
        for e in batch:
            p=e["level_price_at_touch"]
            candidate_sets[e["event_id"]]=sorted(
                cid for cid,c in state.clusters.items()
                if 0<=when-c["anchor_time_ms"]<=300000
                and 10000*abs(p-c["anchor_price"])/min(p,c["anchor_price"])<=price_bps)
        cases.append({"asof_ms":when,"event_ids":[e["event_id"] for e in batch],
            "same_asof_candidate_edges":edges,
            "pre_batch_anchor_matches":candidate_sets,
            "events_matching_multiple_prior_anchors":sum(len(x)>1 for x in candidate_sets.values()),
            "diagnostic_only":True})
        # Existing single-event ingestion retained ONLY for comparison.
        state.observe(batch)
    return {"batches":cases,"multi_event_batches":sum(len(c["event_ids"])>1 for c in cases),
        "same_asof_edges":sum(len(c["same_asof_candidate_edges"]) for c in cases),
        "verdict":"BATCH_DIAGNOSTIC_NO_AUTOMATIC_CLUSTER_MERGE"}
