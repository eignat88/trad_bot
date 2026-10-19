"""#176 combined multi-level replay and atomic ASOF cluster separate-process check."""
import argparse,json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars,utc_ms
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters

def main():
    p=argparse.ArgumentParser()
    for arg in ("phase","data-dir","symbol","start","end","prefix"):
        p.add_argument("--"+arg,required=True)
    a=p.parse_args()
    if a.phase not in ("baseline","stage1","stage2","compare"): raise ValueError("phase")
    start,end=utc_ms(a.start),utc_ms(a.end)
    mid=start+(end-start)//2
    base=Path(a.prefix+".baseline.json")
    snapshot=Path(a.prefix+".episode.checkpoint.json")
    clusters_path=Path(a.prefix+".cluster.checkpoint.json")
    completed=Path(a.prefix+".resumed.json")
    if a.phase=="compare":
        old=json.loads(base.read_text()); new=json.loads(completed.read_text())
        fields=("emissions","counters","integrity","episode_state_counts_including_warmup",
                "pending_at_end","unresolved_reason_counts")
        if any(old["episodes"][key]!=new["episodes"][key] for key in fields):
            raise AssertionError("episode restart mismatch")
        if old["clusters"]!=new["clusters"]:
            raise AssertionError("cluster restart mismatch")
        print(a.symbol,"COMBINED_PROCESS_RESTART_PASS",len(new["episodes"]["emissions"]),"events")
        return
    bars=load_bars(Path(a.data_dir)/f"{a.symbol}_5m.csv")
    if a.phase=="baseline":
        episodes=replay(a.symbol,bars,start,end,full_universe=True)
        clusters=AtomicAsofClusters(a.symbol)
        clusters.observe(episodes["emissions"])
        base.write_text(json.dumps({"episodes":episodes,"clusters":clusters.checkpoint()},sort_keys=True))
    elif a.phase=="stage1":
        episodes=replay(a.symbol,bars,start,end,full_universe=True,
                        stop_at_ms=mid,save_checkpoint=snapshot)
        clusters=AtomicAsofClusters(a.symbol)
        clusters.observe(episodes["emissions"])
        clusters_path.write_text(json.dumps(clusters.checkpoint(),sort_keys=True))
        print("COMBINED_CHECKPOINT_SAVED",snapshot,clusters_path)
        return
    else:
        state=json.loads(clusters_path.read_text())
        clusters=AtomicAsofClusters.restore(state)
        if clusters.symbol!=a.symbol:raise ValueError("symbol mismatch")
        episodes=replay(a.symbol,bars,start,end,full_universe=True,resume_checkpoint=snapshot)
        new_events=[e for e in episodes["emissions"] if e["event_id"] not in clusters.assignments]
        clusters.observe(new_events)
        completed.write_text(json.dumps({"episodes":episodes,"clusters":clusters.checkpoint()},sort_keys=True))
    print(a.symbol,a.phase.upper(),"PASS")
if __name__=="__main__":
    main()
