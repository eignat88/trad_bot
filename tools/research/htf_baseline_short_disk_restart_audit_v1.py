"""#176 true separate-invocation checkpoint audit. Research-only local files."""
import argparse
import json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars, utc_ms
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay

FIELDS = ("emissions", "counters", "total_episodes_including_warmup",
          "episode_state_counts_including_warmup", "pending_at_end",
          "unresolved_reason_counts", "integrity", "eligibility_loss_records")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--phase",choices=("baseline","stage1","stage2","compare"),required=True)
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--symbol",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--prefix",type=Path,required=True)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    mid=start+(end-start)//2
    name=str(a.prefix)
    paths={"checkpoint":Path(name+".checkpoint.json"),
           "baseline":Path(name+".baseline.json"),
           "resumed":Path(name+".resumed.json")}
    if a.phase=="compare":
        baseline=json.loads(paths["baseline"].read_text())
        resumed=json.loads(paths["resumed"].read_text())
        diff=[key for key in FIELDS if baseline[key]!=resumed[key]]
        if diff: raise AssertionError(f"SEPARATE_PROCESS_RESTART_FAIL: {diff}")
        print(a.symbol,"SEPARATE_PROCESS_RESTART_PASS","emissions",len(resumed["emissions"]))
        return
    bars=load_bars(a.data_dir/f"{a.symbol}_5m.csv")
    if a.phase=="baseline":
        result=replay(a.symbol,bars,start,end)
        paths["baseline"].write_text(json.dumps(result,sort_keys=True))
    elif a.phase=="stage1":
        replay(a.symbol,bars,start,end,stop_at_ms=mid,
               save_checkpoint=paths["checkpoint"])
        print("SAVED_CHECKPOINT",paths["checkpoint"])
        return
    else:
        if not paths["checkpoint"].exists():
            raise FileNotFoundError(paths["checkpoint"])
        result=replay(a.symbol,bars,start,end,
                      resume_checkpoint=paths["checkpoint"])
        paths["resumed"].write_text(json.dumps(result,sort_keys=True))
    print(a.phase.upper(),"SAVED",paths["baseline" if a.phase=="baseline" else "resumed"])


if __name__=="__main__":
    main()
