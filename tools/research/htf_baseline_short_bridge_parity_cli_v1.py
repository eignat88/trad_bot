"""#176 bridge and event-ID parity audit CLI (offline)."""
import argparse
import json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars, utc_ms
from tools.research.htf_baseline_short_bridge_parity_v1 import inspect

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--symbols",nargs="+",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    start,end=utc_ms(a.start),utc_ms(a.end)
    if end<=start: raise ValueError("invalid interval")
    results=[inspect(s,load_bars(a.data_dir/f"{s}_5m.csv"),start,end) for s in a.symbols]
    a.out.write_text(json.dumps({"issue":176,"research_only":True,"results":results},sort_keys=True,indent=2))
    for r in results:
        print(r["symbol"],"EVENT_ID_PARITY",r["event_id_parity"],
              "ONLY_SELECTED",len(r["selected_only_ids"]),
              "ONLY_FULL",len(r["full_only_ids"]),
              "BRIDGES",r["bridge_count"])
    print("SAVED",a.out)

if __name__=="__main__":
    main()
