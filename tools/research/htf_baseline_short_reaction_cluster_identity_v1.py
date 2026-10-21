"""#176 diagnostic causal reaction-cluster identity; no event deletion.

Clusters are connected components of candidate overlaps, not proven independent
setups. First confirmation and all member IDs retained for audit.
"""
from __future__ import annotations
from hashlib import sha256

from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import within_price_bps


def reaction_clusters(events, *, proximity_ms=300000, price_bps=20):
    ordered = sorted(events, key=lambda e: (e["known_at_ms"], e["event_id"]))
    ids = [e["event_id"] for e in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event ID")
    parent = {eid: eid for eid in ids}

    def root(eid):
        while parent[eid] != eid:
            eid = parent[eid]
        return eid

    edges = []
    for i, a in enumerate(ordered):
        for b in ordered[i+1:]:
            dt = b["known_at_ms"] - a["known_at_ms"]
            if dt > proximity_ms:
                break
            pa = a.get("level_price_at_touch")
            pb = b.get("level_price_at_touch")
            if pa is None or pb is None:
                raise ValueError("missing/invalid level price")
            if within_price_bps(pa, pb, price_bps):
                edges.append((a["event_id"], b["event_id"]))
                x, y = root(a["event_id"]), root(b["event_id"])
                if x != y:
                    parent[max(x,y)] = min(x,y)

    groups = {}
    for e in ordered:
        groups.setdefault(root(e["event_id"]), []).append(e)
    result = []
    for members in groups.values():
        first = min(members, key=lambda e: (e["known_at_ms"], e["event_id"]))
        member_ids = sorted(e["event_id"] for e in members)
        cluster_id = sha256(("|".join(member_ids)).encode()).hexdigest()
        result.append({
            "reaction_cluster_id": cluster_id,
            "first_confirmation_event_id": first["event_id"],
            "first_confirmation_ms": first["known_at_ms"],
            "member_event_ids": member_ids,
            "member_episode_ids": sorted(e["episode_id"] for e in members),
            "level_ids": sorted({e["level_id"] for e in members}),
            "size": len(members),
            "diagnostic_only": True,
        })
    result.sort(key=lambda x: (x["first_confirmation_ms"], x["reaction_cluster_id"]))
    return {
        "raw_events": len(ordered),
        "candidate_edges": len(edges),
        "cluster_count": len(result),
        "multi_event_clusters": sum(c["size"] > 1 for c in result),
        "events_in_multi_clusters": sum(c["size"] for c in result if c["size"] > 1),
        "clusters": result,
        "rule": "TRANSITIVE_COMPONENT_5M_20BPS_DIAGNOSTIC_NOT_EXECUTION",
    }
