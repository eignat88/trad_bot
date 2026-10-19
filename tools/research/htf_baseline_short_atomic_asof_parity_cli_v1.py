"""#176 stable-ID parity and atomic-ASOF historical audit; read-only."""
import argparse,json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars,utc_ms
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from tools.research.htf_baseline_short_episode_id_parity_root_cause_v1 import compare_events
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir",required=True,type=Path)
    p.add_argument("--symbols",nargs="+",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--out",required=True,type=Path)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    if end<=start:raise ValueError("end <= start")
    output=[]
    for symbol in a.symbols:
        bars=load_bars(a.data_dir/f"{symbol}_5m.csv")
        original=replay(symbol,bars,start,end)
        full=replay(symbol,bars,start,end,full_universe=True)
        parity=compare_events(original["emissions"],full["emissions"])
        state=AtomicAsofClusters(symbol)
        assignments=state.observe(full["emissions"])
        ambiguous=[x for x in assignments if x["reaction_cluster_id"] is None]
        report={"symbol":symbol,"parity":parity,
            "full_universe_integrity":full["integrity"],
            "atomic_clusters":len(state.clusters),
            "atomic_assigned":len(assignments)-len(ambiguous),
            "atomic_ambiguous":ambiguous,
            "atomic_assignments":assignments,
            "raw_confirmations":len(full["emissions"]),
            "verdict":"DIAGNOSTIC_ATOMIC_ASOF_NOT_OOS"}
        output.append(report)
        print(symbol,"COMMON_IDS",parity["common_ids"],"SELECTED_ONLY",
              parity["selected_only"],"FULL_ONLY",parity["full_only"],
              "ATOMIC_CLUSTERS",len(state.clusters),"AMBIGUOUS",len(ambiguous))
    a.out.write_text(json.dumps({"issue":176,"research_only":True,"results":output},sort_keys=True,indent=2))
    print("SAVED",a.out)

if __name__=="__main__":
    main()
