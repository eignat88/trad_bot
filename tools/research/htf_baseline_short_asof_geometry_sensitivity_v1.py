"""#176 read-only ASOF geometry sensitivity and order-invariance audit."""
import argparse
import json
from pathlib import Path
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters

def summarize(events, proximity_ms, price_bps):
    clusters=AtomicAsofClusters("SENSITIVITY")
    results=clusters.observe(events,proximity_ms=proximity_ms,price_bps=price_bps)
    statuses={}
    for item in results:statuses[item["status"]]=statuses.get(item["status"],0)+1
    return {"proximity_ms":proximity_ms,"price_bps":price_bps,
        "include_same_asof_prior":False,
        "cluster_count":len(clusters.clusters),"result_count":len(results),
        "statuses":statuses,
        "assignments":{e["event_id"]:next(r["reaction_cluster_id"] for r in results
            if r["event_id"]==e["event_id"]) for e in events}}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--artifacts",nargs="+",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    report=[]
    for path in a.artifacts:
        item=json.loads(path.read_text())
        symbol=item["episodes"]["symbol"]
        events=item["episodes"]["emissions"]
        variants=[]
        for price_bps in (0,10,20,30,40):
            variants.append(summarize(events,300000,price_bps))
        reverse=AtomicAsofClusters("SENSITIVITY")
        reverse.observe(list(reversed(events)),proximity_ms=300000,price_bps=20)
        forward=AtomicAsofClusters("SENSITIVITY")
        forward.observe(events,proximity_ms=300000,price_bps=20)
        if forward.checkpoint()!=reverse.checkpoint():
            raise AssertionError(f"{symbol}: ASOF result order dependent")
        report.append({"symbol":symbol,"event_count":len(events),
            "forward_reverse_order_invariant":True,"variants":variants})
    result={"issue":176,"read_only":True,"results":report,
        "verdict":"ASOF_GEOMETRY_SENSITIVITY_DIAGNOSTIC_NOT_APPROVED"}
    a.out.write_text(json.dumps(result,indent=2,sort_keys=True))
    print("ASOF_SENSITIVITY_PASS","SAVED",a.out)

if __name__=="__main__":main()
