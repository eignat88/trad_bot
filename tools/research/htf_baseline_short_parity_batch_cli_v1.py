"""#176 read-only two-mode parity and ASOF batch diagnostics."""
import argparse,json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars,utc_ms
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from tools.research.htf_baseline_short_episode_id_parity_root_cause_v1 import compare_events
from tools.research.htf_baseline_short_asof_batch_cluster_audit_v1 import audit_batches

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--symbols",nargs="+",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    if end<=start:raise ValueError("invalid interval")
    results=[]
    for symbol in a.symbols:
        bars=load_bars(a.data_dir/f"{symbol}_5m.csv")
        old=replay(symbol,bars,start,end)
        full=replay(symbol,bars,start,end,full_universe=True)
        parity=compare_events(old["emissions"],full["emissions"])
        batch=audit_batches(symbol,full["emissions"])
        results.append({"symbol":symbol,"parity":parity,"batch":batch})
        print(symbol,"ID_MISMATCH",parity["selected_only"],
              "SAME_GEOMETRY",parity["matched_same_level_confirmation"],
              "UNMATCHED",len(parity["unmatched_selected"]),len(parity["unmatched_full"]),
              "MULTI_ASOF_BATCHES",batch["multi_event_batches"])
    a.out.write_text(json.dumps({"issue":176,"research_only":True,"results":results},indent=2,sort_keys=True))
    print("SAVED",a.out)

if __name__=="__main__":
    main()
