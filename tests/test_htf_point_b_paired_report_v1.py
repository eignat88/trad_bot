"""Issue #169 frozen R and cluster analysis regression tests (offline/pure)."""
from datetime import datetime, timedelta, timezone

import pytest

from tools.research.htf_point_b_paired_report_v1 import frozen_gross_r, cluster_ci, analyze
from tools.research.htf_point_b_final_paired_oos_verdict_v1 import BASELINE, POINT_B

FEATURES={"_frozen_intrabar_policy":"STOP_FIRST","_frozen_max_hold":240}
T=datetime(2026,10,7,tzinfo=timezone.utc)

def calc(direction="SHORT",entry=100,stop=110,target=80,ret=1,tp=False,sl=False,ambiguous=False,features=None):
    return frozen_gross_r(direction,entry,stop,target,ret,tp,sl,ambiguous,
                          FEATURES if features is None else features)

def test_short_timeout_raw_price_return_has_negative_sign():
    value,path=calc(ret=1)
    assert value == pytest.approx(-0.1)
    assert path == "TIMEOUT"

def test_short_takeprofit_stop_and_collision():
    assert calc(tp=True)==(2.0,"TP_FIRST")
    assert calc(sl=True)==(-1.0,"SL_FIRST")
    assert calc(tp=False,sl=True,ambiguous=True)==(-1.0,"AMBIGUOUS_STOP_FIRST")
    assert calc(tp=True,sl=True,ambiguous=True)==(-1.0,"AMBIGUOUS_STOP_FIRST")

def test_long_timeout_and_directional_geometry():
    value,path=calc(direction="LONG",entry=100,stop=90,target=120,ret=2)
    assert value==pytest.approx(0.2)
    assert path=="TIMEOUT"
    assert calc(direction="LONG")[1]=="bad_directional_geometry"

def test_frozen_policy_mismatch_fails_closed():
    assert calc(features={"_frozen_intrabar_policy":"TP_FIRST","_frozen_max_hold":240})[1]=="non_frozen_policy"
    assert calc(features={"_frozen_intrabar_policy":"STOP_FIRST","_frozen_max_hold":120})[1]=="non_frozen_policy"

def test_invalid_target_and_flags():
    assert calc(target=85)[1]=="target_not_2R"
    assert calc(tp=True,sl=True)[1]=="inconsistent_first_hit"
    assert calc(ambiguous=True)==(-1.0,"AMBIGUOUS_STOP_FIRST")

def test_day_clusters_reflect_unequal_day_sizes():
    data=[{"day":"a","symbol":"S1","delta_r":-1.0} for _ in range(10)]
    data.append({"day":"b","symbol":"S2","delta_r":2.0})
    result=cluster_ci(data,"day",reps=300,seed=169)
    assert result["clusters"]==2
    assert result["ci95"][0] < 0 < result["ci95"][1]

def test_pairing_and_timestamp_gate_with_no_vps():
    def rec(exp,oid,time,ret):
        return (exp,oid,"BTCUSDT","SHORT",time,"touch","{\"_frozen_intrabar_policy\":\"STOP_FIRST\",\"_frozen_max_hold\":240}",
                100.0,110.0,80.0,True,ret,False,False,False)
    summary,details=analyze([rec(BASELINE,1,T,2),rec(POINT_B,2,T+timedelta(minutes=30),1)],
                             asof=datetime(2026,10,21,tzinfo=timezone.utc),reps=100)
    assert len(details)==1
    assert details[0]["delta_r"]==pytest.approx(0.1)
    assert summary["directions"]["SHORT"]["n"]==1
    assert summary["directions"]["SHORT"]["verdict"]=="INCONCLUSIVE"
    assert summary["directions"]["SHORT"]["leave_one_day_out"] == {"2026-10-07": None}
