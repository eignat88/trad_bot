"""#176 causal ASOF-batch provisional cluster allocation (research only).

All confirmations at one close are inspected against only prior-ASOF anchors.
New same-ASOF connected components produce one new immutable anchor each,
unless component could attach to multiple old anchors (fail closed).
"""
from collections import defaultdict
from hashlib import sha256

class AtomicAsofClusters:
    def __init__(self,symbol):
        self.symbol=symbol
        self.clusters={}
        self.assignments={}
        self.last_asof=None

    def checkpoint(self):
        return {"symbol":self.symbol,"clusters":self.clusters,
                "assignments":self.assignments,"last_asof":self.last_asof}

    @classmethod
    def restore(cls, data):
        state=cls(data["symbol"])
        state.clusters=data["clusters"]
        state.assignments=data["assignments"]
        state.last_asof=data["last_asof"]
        return state

    def observe_batch(self, events, *, proximity_ms=300000, price_bps=20):
        if not events:return []
        times={e["known_at_ms"] for e in events}
        if len(times)!=1:raise ValueError("mixed ASOF batch")
        asof=next(iter(times))
        if self.last_asof is not None and asof<=self.last_asof:
            raise ValueError("nonmonotonic ASOF")
        ids=[e["event_id"] for e in events]
        if len(ids)!=len(set(ids)) or set(ids)&self.assignments.keys():
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
                if 10000*abs(pa-pb)/min(pa,pb)<=price_bps:
                    x,y=root(a["event_id"]),root(b["event_id"])
                    if x!=y:parent[max(x,y)]=min(x,y)
        groups=defaultdict(list)
        for e in batch:groups[root(e["event_id"])].append(e)
        results=[]
        prior_clusters = {cid: dict(c) for cid,c in self.clusters.items()}
        for members in groups.values():
            matches=set()
            for e in members:
                p=e["level_price_at_touch"]
                for cid,cluster in prior_clusters.items():
                    if 0<=asof-cluster["anchor_time_ms"]<=proximity_ms and 10000*abs(p-cluster["anchor_price"])/min(p,cluster["anchor_price"])<=price_bps:
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
                results.append({"event_id":e["event_id"],"reaction_cluster_id":cid,
                    "status":status,"candidate_prior_cluster_ids":sorted(matches)})
        self.last_asof=asof
        return sorted(results,key=lambda e:e["event_id"])

    def observe(self,events):
        grouped=defaultdict(list)
        for e in events:grouped[e["known_at_ms"]].append(e)
        return [item for k in sorted(grouped) for item in self.observe_batch(grouped[k])]
