"""#176 research-only prospective first-seen cluster IDs.

An immutable anchor is assigned when the first qualifying confirmation appears.
Subsequent events may attach to an existing anchor but never rename or merge it.
A bridge between two pre-existing anchors is flagged as ambiguous, not merged.
"""
from __future__ import annotations
from hashlib import sha256

from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import within_price_bps


class FirstSeenReactionClusters:
    def __init__(self, symbol):
        self.symbol = symbol
        self.clusters = {}
        self.assignments = {}
        self.ambiguous_bridges = []

    def observe(self, events, *, proximity_ms=300000, price_bps=20):
        assignments = []
        for e in sorted(events, key=lambda x: (x["known_at_ms"], x["event_id"])):
            eid = e["event_id"]
            if eid in self.assignments:
                raise ValueError("duplicate confirmation delivery")
            price = e["level_price_at_touch"]
            time = e["known_at_ms"]
            if price <= 0:
                raise ValueError("invalid confirmed resistance")
            candidates = [
                cid for cid, cluster in self.clusters.items()
                if 0 <= time - cluster["anchor_time_ms"] <= proximity_ms
                and within_price_bps(price, cluster["anchor_price"], price_bps)
            ]
            if len(candidates) > 1:
                self.ambiguous_bridges.append({"event_id":eid, "candidate_cluster_ids":sorted(candidates)})
                assignments.append({"event_id":eid, "reaction_cluster_id":None, "status":"AMBIGUOUS_BRIDGE"})
                self.assignments[eid] = None
                continue
            if candidates:
                cid = candidates[0]
                self.clusters[cid]["member_event_ids"].append(eid)
            else:
                cid = sha256(f"{self.symbol}|{eid}".encode()).hexdigest()
                self.clusters[cid] = {
                    "reaction_cluster_id":cid, "anchor_event_id":eid,
                    "anchor_time_ms":time, "anchor_price":price,
                    "member_event_ids":[eid],
                }
            self.assignments[eid] = cid
            assignments.append({"event_id":eid, "reaction_cluster_id":cid, "status":"PROVISIONAL"})
        return assignments

    def result(self):
        return {
            "cluster_count":len(self.clusters),
            "assigned_events":sum(cid is not None for cid in self.assignments.values()),
            "ambiguous_bridges":self.ambiguous_bridges,
            "clusters":sorted(self.clusters.values(),key=lambda c:(c["anchor_time_ms"],c["reaction_cluster_id"])),
            "verdict":"PROVISIONAL_ANCHOR_ONLY_NOT_VALIDATED_MARKET_INDEPENDENCE",
        }
