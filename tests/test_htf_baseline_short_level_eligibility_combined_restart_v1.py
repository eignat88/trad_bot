"""#176 diagnostic level eligibility and combined checkpoint contracts."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.models import Candle
from app.research.htf_baseline_short_causal_level_adapter_v1 import causal_resistance_universe
from tools.research.htf_baseline_short_causal_coverage_v1 import closed_htf
from tools.research.htf_baseline_short_level_eligibility_audit_v1 import LevelEligibilityAudit
from tools.research.htf_baseline_short_asof_atomic_cluster_v1 import AtomicAsofClusters

def test_price_cross_is_eligibility_loss_not_terminal_retirement():
    s=LevelEligibilityAudit()
    level=SimpleNamespace(level_id="L",price=101)
    s.advance(300000,[level],100,pending_ids=("L",))
    s.advance(600000,[],102,pending_ids=("L",))
    assert s.transitions[-1]["state"]=="NOT_CURRENTLY_ELIGIBLE"
    assert s.transitions[-1]["reason"]=="BELOW_OR_AT_REFERENCE"
    assert s.transitions[-1]["pending"] is True
    assert "L" in s.ever_seen

def test_eligibility_reentry_is_recorded_without_deletion():
    s=LevelEligibilityAudit()
    level=SimpleNamespace(level_id="L",price=101)
    s.advance(300000,[level],100)
    s.advance(600000,[],102)
    s.advance(900000,[level],100)
    assert s.counts["RE_ELIGIBLE"]==1

def test_atomic_cluster_checkpoint_json_round_trip():
    c=AtomicAsofClusters("ETHUSDT")
    c.observe([{"event_id":"A","known_at_ms":300000,"level_price_at_touch":100}])
    restored=AtomicAsofClusters.restore(json.loads(json.dumps(c.checkpoint())))
    restored.observe([{"event_id":"B","known_at_ms":600000,"level_price_at_touch":100.1}])
    c.observe([{"event_id":"B","known_at_ms":600000,"level_price_at_touch":100.1}])
    assert restored.checkpoint()==c.checkpoint()
    assert len(c.clusters)==1


def test_warmup_excludes_transitions_but_preserves_reentry_state():
    warm=LevelEligibilityAudit(record_transitions=False)
    level=SimpleNamespace(level_id="L",price=101)
    warm.advance(300000,[level],100)
    warm.advance(600000,[],102)
    assert warm.transitions==[]
    assert warm.counts=={}
    report=LevelEligibilityAudit()
    report.ever_seen.update(warm.ever_seen)
    report.last_eligible=set(warm.last_eligible)
    report.advance(900000,[level],100)
    assert report.transitions==[{"asof_ms":900000,"level_id":"L","state":"RE_ELIGIBLE","pending":False}]
    assert report.counts["RE_ELIGIBLE"]==1


def write_htf_warmup_fixture(path: Path, start_ms: int):
    rows=[]
    prices=[100,101,102,103,104,103,102,101,100,99,100,101]
    for hour in range(20):
        base=start_ms+(hour-20)*3600000
        for i,price in enumerate(prices):
            high=110 if hour in (7,8,9,10,11) and i==4 else price+1
            rows.append(Candle(base+i*300000,price,high,price-1,price,10))
    with path.open("w",newline="",encoding="utf-8") as f:
        f.write("timestamp,open,high,low,close,volume\n")
        for c in rows:
            f.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
    return rows


def assert_warmup_fixture_has_eligible_level(rows, start_ms: int):
    pre_seen=set()
    for i in range(60,len(rows)):
        asof=int(rows[i].timestamp)+300000
        prefix=rows[max(0,i-199):i+1]
        levels=causal_resistance_universe(closed_htf(rows[:i+1],asof)[-200:],prefix,asof)
        ids={x.level_id for x in levels}
        if asof<start_ms:pre_seen.update(ids)
        elif pre_seen and ids:return sorted(pre_seen)[0]
    raise AssertionError("fixture did not create pre-start eligible level")


def run_eligibility_cli(root: Path, data_dir: Path, start: str, end: str, out: Path):
    env=dict(os.environ);env["PYTHONPATH"]=str(root)
    return subprocess.run([sys.executable,"-m","tools.research.htf_baseline_short_level_eligibility_cli_v1",
                           "--data-dir",str(data_dir),"--symbols","BTCUSDT","--start",start,"--end",end,
                           "--out",str(out)],cwd=root,env=env,capture_output=True,text=True,check=True)


def test_cli_warms_before_start_and_does_not_leak_warmup_transitions(tmp_path):
    root=Path(__file__).resolve().parents[1];data_dir=tmp_path/"data";data_dir.mkdir();start_ms=1800000000000
    write_htf_warmup_fixture(data_dir/"BTCUSDT_5m.csv",start_ms)
    start=(datetime.fromtimestamp(start_ms/1000,timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z")
    end=(datetime.fromtimestamp(start_ms/1000,timezone.utc)+timedelta(hours=2)).isoformat().replace("+00:00","Z")
    out=tmp_path/"report.json";run_eligibility_cli(root,data_dir,start,end,out)
    result=json.loads(out.read_text())["results"][0]
    assert "levels_ever_seen" in result
    assert all(start_ms+3600000<=t["asof_ms"]<start_ms+7200000 for t in result["transitions"])
    assert all(start_ms+3600000<=t["asof_ms"]<start_ms+7200000 for t in result["transitions"])


def test_cli_warmup_is_deterministic_across_restarts(tmp_path):
    root=Path(__file__).resolve().parents[1];data_dir=tmp_path/"data";data_dir.mkdir();start_ms=1800000000000
    write_htf_warmup_fixture(data_dir/"BTCUSDT_5m.csv",start_ms)
    start=(datetime.fromtimestamp(start_ms/1000,timezone.utc)+timedelta(hours=1)).isoformat().replace("+00:00","Z")
    end=(datetime.fromtimestamp(start_ms/1000,timezone.utc)+timedelta(hours=2)).isoformat().replace("+00:00","Z")
    first=tmp_path/"first.json";second=tmp_path/"second.json";run_eligibility_cli(root,data_dir,start,end,first)
    run_eligibility_cli(root,data_dir,start,end,second);assert first.read_bytes()==second.read_bytes()
