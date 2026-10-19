"""#176 diagnostic level eligibility and combined checkpoint contracts."""
import json
from types import SimpleNamespace
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
