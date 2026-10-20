"""#176 causal ASOF-batch provisional cluster allocation (research only).

All confirmations at one close are inspected against only prior-ASOF anchors.
New same-ASOF connected components produce one new immutable anchor each,
unless component could attach to multiple old anchors (fail closed).
"""
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256

def within_price_bps(a, b, price_bps):
    """Exact decimal boundary using the existing min-price denominator."""
    pa, pb = Decimal(str(a)), Decimal(str(b))
    threshold = Decimal(str(price_bps))
    if not pa.is_finite() or not pb.is_finite() or not threshold.is_finite():
        raise ValueError("non-finite price geometry")
    if pa <= 0 or pb <= 0 or threshold < 0:
        raise ValueError("invalid price geometry")
    return Decimal(10000) * abs(pa - pb) <= threshold * min(pa, pb)


class AtomicAsofClusters:
    def __init__(self,symbol):
        self.symbol=symbol
        self.clusters={}
        self.assignments={}
        self.events={}
        self.last_asof=None

    def checkpoint(self):
        return {"symbol":self.symbol,"clusters":self.clusters,
                "assignments":self.assignments,"events":self.events,
                "last_asof":self.last_asof}

    @classmethod
    def restore(cls, data):
        state=cls(data["symbol"])
        state.clusters=data["clusters"]
        state.assignments=data["assignments"]
        state.events=data.get("events",{})
        state.last_asof=data["last_asof"]
        return state

    def observe_batch(self, events, *, proximity_ms=300000, price_bps=20,
                      include_same_asof_prior=False):
        if not events:return []
        times={e["known_at_ms"] for e in events}
        if len(times)!=1:raise ValueError("mixed ASOF batch")
        asof=next(iter(times))
        if self.last_asof is not None and asof<=self.last_asof:
            raise ValueError("nonmonotonic ASOF")
        ids=[e["event_id"] for e in events]
        if len(ids)!=len(set(ids)) or any(event_id in self.assignments for event_id in ids):
            raise ValueError("duplicate event")
        batch=sorted(events,key=lambda e:e["event_id"])
        for e in batch:
            if e["level_price_at_touch"]<=0:raise ValueError("invalid price")
        parent={e["event_id"]:e["event_id"] for e in batch}
        def root(x):
            while parent[x]!=x:x=parent[x]
            return x
        for i,a in enumerate(batch):
            for b in batch[i+1:]:
                pa,pb=a["level_price_at_touch"],b["level_price_at_touch"]
                if within_price_bps(pa,pb,price_bps):
                    x,y=root(a["event_id"]),root(b["event_id"])
                    if x!=y:parent[max(x,y)]=min(x,y)
        groups=defaultdict(list)
        for e in batch:groups[root(e["event_id"])].append(e)
        results=[]
        prior_clusters = {cid: dict(c) for cid,c in self.clusters.items()
                          if include_same_asof_prior or c["anchor_time_ms"]<asof}
        for members in groups.values():
            matches=set()
            for e in members:
                p=e["level_price_at_touch"]
                for cid,cluster in prior_clusters.items():
                    if 0<=asof-cluster["anchor_time_ms"]<=proximity_ms and within_price_bps(p,cluster["anchor_price"],price_bps):
                        matches.add(cid)
            if len(matches)>1:
                cid=None; status="AMBIGUOUS_PRIOR_ANCHORS"
            elif matches:
                cid=next(iter(matches));status="PROVISIONAL_ATTACH"
                self.clusters[cid]["member_event_ids"].extend(e["event_id"] for e in members)
            else:
                anchor=min(members,key=lambda e:e["event_id"])
                cid=sha256(f"{self.symbol}|{anchor['event_id']}".encode()).hexdigest()
                status="PROVISIONAL_NEW"
                self.clusters[cid]={"reaction_cluster_id":cid,
                    "anchor_event_id":anchor["event_id"],"anchor_time_ms":asof,
                    "anchor_price":anchor["level_price_at_touch"],
                    "member_event_ids":[e["event_id"] for e in members]}
            for e in members:
                self.assignments[e["event_id"]]=cid
                self.events[e["event_id"]]=dict(e)
                results.append({"event_id":e["event_id"],"reaction_cluster_id":cid,
                    "status":status,"candidate_prior_cluster_ids":sorted(matches)})
        self.last_asof=asof
        return sorted(results,key=lambda e:e["event_id"])

    def observe(self,events, **kwargs):
        grouped=defaultdict(list)
        for e in events:grouped[e["known_at_ms"]].append(e)
        return [item for k in sorted(grouped) for item in self.observe_batch(grouped[k], **kwargs)]
