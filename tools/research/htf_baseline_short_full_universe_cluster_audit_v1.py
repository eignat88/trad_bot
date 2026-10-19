"""#176 compare selected-once vs full-ASOF universe with provisional anchor IDs."""
import argparse
import json
from pathlib import Path

from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars, utc_ms
from tools.research.htf_baseline_short_first_seen_cluster_v1 import FirstSeenReactionClusters
from tools.research.htf_baseline_short_reaction_cluster_identity_v1 import reaction_clusters


def analyze(symbol, bars, start, end):
    old = replay(symbol,bars,start,end)
    full = replay(symbol,bars,start,end,full_universe=True)
    first = FirstSeenReactionClusters(symbol)
    assigned = first.observe(full["emissions"])
    if len(assigned) != len(full["emissions"]):
        raise AssertionError("missing first-seen assignments")
    if len({e["event_id"] for e in full["emissions"]}) != len(assigned):
        raise AssertionError("duplicate emissions")
    return {
        "symbol":symbol,
        "selected_once_events":len(old["emissions"]),
        "full_universe_events":len(full["emissions"]),
        "selected_once_levels_known":old["counters"].get("levels_known",0),
        "full_universe_levels_known":full["counters"].get("levels_known",0),
        "full_universe_integrity":full["integrity"],
        "anchor_clusters":first.result(),
        "retrospective_clusters":reaction_clusters(full["emissions"]),
        "provisional_event_assignments":assigned,
        "full_universe_unresolved_counts":full["unresolved_reason_counts"],
        "verdict":"HISTORICAL_CAUSAL_RESEARCH_ONLY_NO_OOS",
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--symbols",nargs="+",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    if end<=start:
        raise ValueError("end must be later than start")
    result={"issue":176,"research_only":True,
            "results":[analyze(s,load_bars(a.data_dir/f"{s}_5m.csv"),start,end)
                       for s in a.symbols]}
    a.out.write_text(json.dumps(result,indent=2,sort_keys=True))
    for item in result["results"]:
        print(item["symbol"],"old",item["selected_once_events"],
              "full",item["full_universe_events"],
              "anchor_clusters",item["anchor_clusters"]["cluster_count"],
              "bridge_ambiguities",len(item["anchor_clusters"]["ambiguous_bridges"]))
    print("SAVED",a.out)


if __name__=="__main__":
    main()
