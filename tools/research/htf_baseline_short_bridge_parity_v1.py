"""#176 read-only first-seen bridge diagnostic, not a resolution policy."""
from __future__ import annotations
from tools.research.htf_baseline_short_full_universe_cluster_audit_v1 import analyze
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars, utc_ms


def inspect(symbol, bars, start, end):
    from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
    from tools.research.htf_baseline_short_first_seen_cluster_v1 import FirstSeenReactionClusters
    old = replay(symbol,bars,start,end)
    full = replay(symbol,bars,start,end,full_universe=True)
    old_ids={e["event_id"] for e in old["emissions"]}
    full_ids={e["event_id"] for e in full["emissions"]}
    first=FirstSeenReactionClusters(symbol)
    event_lookup={e["event_id"]:e for e in full["emissions"]}
    chronological=sorted(full["emissions"],key=lambda e:(e["known_at_ms"],e["event_id"]))
    cases=[]
    for event in chronological:
        assignments=first.observe([event])
        if assignments[0]["status"]=="AMBIGUOUS_BRIDGE":
            ambiguity=first.ambiguous_bridges[-1]
            anchors=[first.clusters[cid] for cid in ambiguity["candidate_cluster_ids"]]
            cases.append({
                "event":event,
                "candidate_anchors":[{
                    "reaction_cluster_id":x["reaction_cluster_id"],
                    "anchor_event_id":x["anchor_event_id"],
                    "anchor_time_ms":x["anchor_time_ms"],
                    "anchor_price":x["anchor_price"],
                    "member_event_ids":list(x["member_event_ids"]),
                } for x in anchors],
                "candidate_count":len(anchors),
                "verdict":"UNRESOLVED_NO_AUTOMATIC_ASSIGNMENT",
            })
    return {
        "symbol":symbol,
        "selected_once_count":len(old_ids),"full_universe_count":len(full_ids),
        "event_id_parity":old_ids==full_ids,
        "selected_only_ids":sorted(old_ids-full_ids),
        "full_only_ids":sorted(full_ids-old_ids),
        "bridge_count":len(cases),
        "bridge_cases":cases,
        "confirmed_events":len(chronological),
        "assigned_count":sum(v is not None for v in first.assignments.values()),
        "verdict":"RESEARCH_ONLY_BRIDGE_RESOLUTION_NOT_APPROVED",
    }
