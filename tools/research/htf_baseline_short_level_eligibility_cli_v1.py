"""#176 offline eligible HTF level transitions; no lifecycle mutations."""
import argparse,json
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars,utc_ms,closed_htf,BAR_MS
from app.research.htf_baseline_short_causal_level_adapter_v1 import causal_resistance_universe
from tools.research.htf_baseline_short_level_eligibility_audit_v1 import LevelEligibilityAudit

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir",type=Path,required=True)
    p.add_argument("--symbols",nargs="+",required=True)
    p.add_argument("--start",required=True)
    p.add_argument("--end",required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args();start,end=utc_ms(a.start),utc_ms(a.end)
    results=[]
    for symbol in a.symbols:
        candles=load_bars(a.data_dir/f"{symbol}_5m.csv")
        state=LevelEligibilityAudit()
        for i,bar in enumerate(candles):
            asof=int(bar.timestamp)+BAR_MS
            if asof>=end:break
            if asof<start:continue
            prefix=candles[max(0,i-199):i+1]
            htf=closed_htf(candles[:i+1],asof)[-200:]
            current=causal_resistance_universe(htf,prefix,asof) if len(prefix)>=20 and len(htf)>=26 else ()
            state.advance(asof,current,float(bar.close))
        results.append({"symbol":symbol,**state.result()})
        print(symbol,"LEVELS",len(state.ever_seen),"TRANSITIONS",len(state.transitions),"COUNTS",dict(state.counts))
    a.out.write_text(json.dumps({"research_only":True,"issue":176,"results":results},indent=2))
    print("SAVED",a.out)
if __name__=="__main__":main()
