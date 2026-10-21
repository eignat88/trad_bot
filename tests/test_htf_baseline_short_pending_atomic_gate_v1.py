"""#176 unified checkpoint and real pending impact regression gates."""
import json
import pytest
from types import SimpleNamespace
from tools.research.htf_baseline_short_atomic_checkpoint_v1 import write_checkpoint,read_checkpoint
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay
from app.models import Candle

STEP=300000
BASE=1800000000000 // 3600000 * 3600000

def test_combined_checkpoint_roundtrip_and_old_valid_survives_interrupted_replace(tmp_path):
    path=tmp_path/"state.json"
    state={"episodes":{"pending":{"L":"e"}},"levels":{"L":{"price":100}},
           "clusters":{"assignments":{"e":"cluster"}},"cursor":900000}
    write_checkpoint(path,state)
    with pytest.raises(RuntimeError,match="SIMULATED_BEFORE_ATOMIC_REPLACE"):
        write_checkpoint(path,{"wrong":True},simulate_interrupt=True)
    assert read_checkpoint(path)==state

def test_combined_checkpoint_detects_corruption(tmp_path):
    path=tmp_path/"state.json"
    write_checkpoint(path,{"cursor":1})
    data=json.loads(path.read_text())
    data["payload"]["cursor"]=2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="integrity mismatch"):
        read_checkpoint(path)

def test_combined_checkpoint_rejects_wrong_schema(tmp_path):
    path=tmp_path/"state.json"
    write_checkpoint(path,{"cursor":1})
    data=json.loads(path.read_text());data["schema"]="OTHER"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="schema"):
        read_checkpoint(path)

def test_pending_impact_replay_checkpoint_equivalence(tmp_path):
    candles=[Candle(BASE+i*STEP,100,101,99,100,10) for i in range(420)]
    start,end=BASE+300*STEP,BASE+400*STEP
    baseline=replay("BTCUSDT",candles,start,end,full_universe=True,pending_impact=True)
    checkpoint=tmp_path/"replay.json"
    replay("BTCUSDT",candles,start,end,full_universe=True,pending_impact=True,
        stop_at_ms=BASE+350*STEP,save_checkpoint=checkpoint)
    resumed=replay("BTCUSDT",candles,start,end,full_universe=True,pending_impact=True,
        resume_checkpoint=checkpoint)
    assert baseline["pending_impact"]==resumed["pending_impact"]
    assert baseline["eligibility_loss_records"]==resumed["eligibility_loss_records"]
    assert baseline["emissions"]==resumed["emissions"]
