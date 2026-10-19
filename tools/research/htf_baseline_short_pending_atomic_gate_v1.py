"""#176 combined research-only pending impact and single-file restart gate."""
import argparse
import json
import tempfile
from pathlib import Path
from tools.research.htf_baseline_short_atomic_checkpoint_v1 import write_checkpoint,read_checkpoint
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars,utc_ms

FIELDS=("emissions","counters","integrity","episode_state_counts_including_warmup",
        "pending_at_end","unresolved_reason_counts","pending_impact")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--phase",required=True,choices=("baseline","stage1","stage2","compare","fault"))
    p.add_argument("--data-dir",required=True,type=Path)
    p.add_argument("--symbol",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--prefix",required=True,type=Path)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    mid=start+(end-start)//2
    prefix=str(a.prefix)
    ck=Path(prefix+".atomic.json")
    base=Path(prefix+".baseline.json")
    final=Path(prefix+".resumed.json")
    if a.phase=="compare":
        x=json.loads(base.read_text());y=json.loads(final.read_text())
        different=[k for k in FIELDS if x["episodes"][k]!=y["episodes"][k]]
        if different or x["clusters"]!=y["clusters"]:
            raise AssertionError(f"COMBINED_RESTART_MISMATCH {different}")
        print(a.symbol,"ATOMIC_COMBINED_RESTART_PASS",len(y["episodes"]["emissions"]),
              "pending_impacts",len(y["episodes"]["pending_impact"]["eligibility_loss_with_pending"]))
        return
    if a.phase=="fault":
        old=read_checkpoint(ck)
        try:write_checkpoint(ck,{"invalid":"partial"},simulate_interrupt=True)
        except RuntimeError as ex:
            if str(ex)!="SIMULATED_BEFORE_ATOMIC_REPLACE":raise
        if read_checkpoint(ck)!=old:raise AssertionError("last valid checkpoint lost")
        print(a.symbol,"ATOMIC_INTERRUPTED_WRITE_RECOVERY_PASS")
        return
    bars=load_bars(a.data_dir/f"{a.symbol}_5m.csv")
    if a.phase=="baseline":
        episodes=replay(a.symbol,bars,start,end,full_universe=True,pending_impact=True)
        cluster=AtomicAsofClusters(a.symbol)
        cluster.observe(episodes["emissions"])
        base.write_text(json.dumps({"episodes":episodes,"clusters":cluster.checkpoint()},sort_keys=True))
        print(a.symbol,"BASELINE_PASS")
        return
    if a.phase=="stage1":
        with tempfile.TemporaryDirectory() as tmp:
            ep=Path(tmp)/"episode.json"
            result=replay(a.symbol,bars,start,end,full_universe=True,pending_impact=True,
                stop_at_ms=mid,save_checkpoint=ep)
            cluster=AtomicAsofClusters(a.symbol);cluster.observe(result["emissions"])
            write_checkpoint(ck,{"symbol":a.symbol,"start_ms":start,"end_ms":end,
                "episode":json.loads(ep.read_text()),"cluster":cluster.checkpoint()})
        print(a.symbol,"ATOMIC_STAGE1_PASS")
        return
    snapshot=read_checkpoint(ck)
    if (snapshot["symbol"],snapshot["start_ms"],snapshot["end_ms"])!=(a.symbol,start,end):
        raise ValueError("wrong checkpoint provenance")
    cluster=AtomicAsofClusters.restore(snapshot["cluster"])
    with tempfile.TemporaryDirectory() as tmp:
        ep=Path(tmp)/"episode.json"
        ep.write_text(json.dumps(snapshot["episode"]))
        result=replay(a.symbol,bars,start,end,full_universe=True,pending_impact=True,resume_checkpoint=ep)
    new_events=[e for e in result["emissions"] if e["event_id"] not in cluster.assignments]
    cluster.observe(new_events)
    final.write_text(json.dumps({"episodes":result,"clusters":cluster.checkpoint()},sort_keys=True))
    print(a.symbol,"ATOMIC_STAGE2_PASS")
if __name__=="__main__":main()
