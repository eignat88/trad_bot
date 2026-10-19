"""Issue #169: read-only reproducible paired OOS verdict.

Connects only to research tables; transactions use REPEATABLE READ READ ONLY.
Gross research results, NOT executable net returns or causal uplift for all touches.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from tools.research.htf_point_b_final_paired_oos_verdict_v1 import (
    BASELINE, POINT_B, FREEZE, EventRecord, match_frozen_events,
)

QUERY = """
SELECT p.experiment_id,p.observation_id,p.symbol,p.direction,p.signal_time,
       p.features->>'setup_event_id',p.features,
       p.reference_price::float8,p.invalidation_price::float8,
       p.target_1::float8,o.is_final,o.return_at_240m::float8,
       o.tp_before_sl,o.sl_before_tp,o.ambiguous_intrabar
FROM research.prospective_observation p
LEFT JOIN research.prospective_outcome o ON o.observation_id=p.observation_id
WHERE p.experiment_id IN (%s,%s)
ORDER BY p.experiment_id,p.signal_time,p.observation_id
"""

def frozen_gross_r(direction, entry, stop, target, ret, tp, sl, ambiguous, features):
    """Return (R,exit) strictly under frozen 2R/1R/240m/STOP_FIRST geometry."""
    if features.get("_frozen_intrabar_policy") != "STOP_FIRST" or features.get("_frozen_max_hold") != 240:
        return None,"non_frozen_policy"
    if direction not in ("LONG","SHORT") or None in (entry,stop,target):
        return None,"missing_geometry"
    entry,stop,target=map(float,(entry,stop,target))
    sign=1 if direction=="LONG" else -1
    if (not all(map(math.isfinite,(entry,stop,target))) or entry<=0
        or sign*(entry-stop)<=0 or sign*(target-entry)<=0):
        return None,"bad_directional_geometry"
    risk=abs(entry-stop)
    rr=abs(target-entry)/risk
    if not math.isclose(rr,2.0,rel_tol=1e-5,abs_tol=1e-5):
        return None,"target_not_2R"
    if tp is True and sl is True or ambiguous is True and sl is not True:
        return None,"ambiguous_first_hit"
    if sl is True: return -1.0,"SL_FIRST"
    if tp is True: return rr,"TP_FIRST"
    if ret is None or not math.isfinite(float(ret)): return None,"missing_timeout_return"
    # Evaluator return_at_240m is non-directional price return.
    return sign*float(ret)*entry/(100*risk),"TIMEOUT"

def _feature_map(raw):
    if isinstance(raw,dict):return raw
    if isinstance(raw,str):
        val=json.loads(raw)
        return val if isinstance(val,dict) else {}
    return {}

def cluster_ci(data,key,reps=4000,seed=169):
    groups=defaultdict(list)
    for r in data:groups[r[key]].append(r["delta_r"])
    groups=[groups[k] for k in sorted(groups)]
    if len(groups)<2:return {"clusters":len(groups),"ci95":None,"positive_fraction":None}
    n=len(groups);rng=random.Random(seed)
    sums=[sum(g) for g in groups];sizes=[len(g) for g in groups]
    out=[]
    for _ in range(reps):
        samples=[rng.randrange(n) for _ in range(n)]
        out.append(sum(sums[i] for i in samples)/sum(sizes[i] for i in samples))
    out.sort()
    return {"clusters":n,"ci95":[out[int((reps-1)*.025)],out[int((reps-1)*.975)]],
            "positive_fraction":sum(v>0 for v in out)/reps}

def _pf(values):
    loss=-sum(x for x in values if x<0)
    return sum(x for x in values if x>0)/loss if loss>0 else None

def analyze(rows,asof,reps=4000):
    recs=[];paths={};rejects=Counter()
    for (exp,oid,symbol,direction,signal_time,event_id,feat,entry,stop,target,
         finalized,ret,tp,sl,ambiguous) in rows:
        value,exit_label=frozen_gross_r(direction,entry,stop,target,ret,tp,sl,ambiguous,_feature_map(feat))
        if not finalized: value=None;exit_label="not_final"
        if finalized and value is None:rejects[f"{direction}:{exit_label}"]+=1
        recs.append(EventRecord(exp,event_id or "",oid,symbol,direction,signal_time,bool(finalized),value))
        paths[oid]=exit_label
    report=match_frozen_events(recs)
    details=[]
    for p in report.pairs:
        b,a=p.baseline,p.point_b
        details.append({"event_id":p.event_id,"symbol":p.symbol,"direction":p.direction,
            "point_b_observation_id":a.observation_id,"baseline_observation_id":b.observation_id,
            "day":a.signal_time.astimezone(timezone.utc).date().isoformat(),
            "point_b_signal_time":a.signal_time.isoformat(),"baseline_signal_time":b.signal_time.isoformat(),
            "delay_minutes":(a.signal_time-b.signal_time).total_seconds()/60,
            "point_b_r":a.gross_r,"baseline_r":b.gross_r,"delta_r":p.delta_r,
            "point_b_exit":paths[a.observation_id],"baseline_exit":paths[b.observation_id]})
    elapsed=asof>=FREEZE+timedelta(days=14)
    results={}
    for direction in ("LONG","SHORT"):
        data=[r for r in details if r["direction"]==direction]
        if not data:
            results[direction]={"n":0,"verdict":"INCONCLUSIVE"}
            continue
        delta=[r["delta_r"] for r in data];a=[r["point_b_r"] for r in data];b=[r["baseline_r"] for r in data]
        days=sorted({r["day"] for r in data})
        day_ci=cluster_ci(data,"day",reps)
        sym_ci=cluster_ci(data,"symbol",reps)
        leave_out={d:mean(r["delta_r"] for r in data if r["day"]!=d) for d in days}
        # Strictly incremental research verdict: not a trading recommendation.
        verdict="INCONCLUSIVE"
        if elapsed and day_ci["ci95"] and sym_ci["ci95"] and day_ci["ci95"][1]<0 and sym_ci["ci95"][1]<0:
            verdict="NO_EDGE_INCREMENTAL_POINT_B"
        results[direction]={"n":len(data),"symbols":len({r["symbol"] for r in data}),
            "calendar_days":len(days),"point_b_avg_r":mean(a),"baseline_avg_r":mean(b),
            "point_b_pf":_pf(a),"baseline_pf":_pf(b),"point_b_total_r":sum(a),
            "baseline_total_r":sum(b),"paired_delta_avg_r":mean(delta),
            "paired_delta_median_r":median(delta),"day_cluster":day_ci,
            "symbol_cluster":sym_ci,"leave_one_day_out":leave_out,
            "point_b_exits":dict(Counter(r["point_b_exit"] for r in data)),
            "baseline_exits":dict(Counter(r["baseline_exit"] for r in data)),
            "verdict":verdict}
    summary={"issue":169,"asof_utc":asof.isoformat(),"freeze_ts":FREEZE.isoformat(),
        "elapsed_14_days_met":elapsed,"point_b_source_rows":report.point_b_total,
        "baseline_source_rows":report.baseline_total,"matching_reasons":report.reasons,
        "outcome_rejections":dict(rejects),"directions":results,
        "limitations":["gross only; no fee/fill model","selection conditioned on Point-B observation",
        "is_final is not independent proof of contiguous candle coverage",
        "14 calendar dates may cover <14 elapsed days","few independent time clusters"]}
    return summary,details

def write_report(summary,details,prefix):
    prefix.parent.mkdir(parents=True,exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(summary,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    with prefix.with_suffix(".csv").open("w",newline="",encoding="utf-8") as fh:
        if details:
            writer=csv.DictWriter(fh,fieldnames=list(details[0]))
            writer.writeheader();writer.writerows(details)
    lines=["# Issue #169 — paired OOS report","",f"As of: {summary['asof_utc']}",
        f"14 elapsed days: {summary['elapsed_14_days_met']}","","|Direction|N|Delta R|Day CI|Symbol CI|Verdict|",
        "|---|---:|---:|---|---|---|"]
    for d,v in summary["directions"].items():
        if v["n"]:
            lines.append(f"|{d}|{v['n']}|{v['paired_delta_avg_r']:.6f}|{v['day_cluster']['ci95']}|{v['symbol_cluster']['ci95']}|{v['verdict']}|")
    lines+=["","Gross research only. NO paper/live or frozen protocol changes.",
            f"Matching exclusions: {summary['matching_reasons']}",
            f"Outcome exclusions: {summary['outcome_rejections']}"]
    prefix.with_suffix(".md").write_text("\n".join(lines)+"\n",encoding="utf-8")

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-prefix",required=True,type=Path)
    parser.add_argument("--reps",type=int,default=4000)
    opts=parser.parse_args(argv)
    if opts.reps<100:parser.error("--reps must be >=100")
    from app.config.settings import load_settings
    from app.db.repository import ScannerRepository
    root=Path("/opt/trad_bot")
    cfg=load_settings(path=root/"config.yaml",env_file=root/".env")
    repo=ScannerRepository(cfg.db_host,cfg.db_port,cfg.db_name,cfg.db_user,cfg.db_password,backend="postgres")
    conn=repo._conn
    try:
        cur=conn.cursor()
        try:
            cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SELECT transaction_timestamp()")
            asof=cur.fetchone()[0]
            cur.execute(QUERY,(POINT_B,BASELINE))
            summary,details=analyze(cur.fetchall(),asof,opts.reps)
            conn.rollback()
        finally:cur.close()
    finally:conn.close()
    write_report(summary,details,opts.output_prefix)
    print(json.dumps({"asof":summary["asof_utc"],
        "directions":{d:{"n":v["n"],"verdict":v["verdict"]} for d,v in summary["directions"].items()}},indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
