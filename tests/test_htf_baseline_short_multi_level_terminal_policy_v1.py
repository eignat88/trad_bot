"""Issue #176 terminal-policy and causal event regression checks."""
import pytest
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes
STEP = 300000

def test_window_eviction_removes_pending_and_blocks_late_confirmation():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={"L": (100., 0, True)})
    eid = s.pending["L"]
    s.advance(open_ms=STEP, level_contacts={}, unavailable=["L"])
    assert s.episodes[eid]["state"] == "UNRESOLVED"
    assert "L" not in s.pending
    with pytest.raises(ValueError, match="without pending"):
        s.advance(open_ms=2*STEP, level_contacts={}, confirmations=["L"])
    assert not s.events

def test_recontact_supersession_unlinks_prior_pending():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={"L": (100., 0, True)})
    first = s.pending["L"]
    s.advance(open_ms=STEP, level_contacts={"L": (100., 0, False)})
    s.advance(open_ms=2*STEP, level_contacts={"L": (100., 0, True)})
    assert s.episodes[first]["state"] == "UNRESOLVED"
    assert s.pending["L"] != first
    event = s.advance(open_ms=3*STEP, level_contacts={}, confirmations=["L"])[0]
    assert event["episode_id"] != first
    assert event["level_available_ms"] <= event["first_touch_open_ms"] < event["known_at_ms"]

def test_confirmation_fails_on_perturbed_snapshot_causal_order():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={"L": (100., 0, True)})
    s.episodes[s.pending["L"]]["level_available_ms"] = 2*STEP
    with pytest.raises(ValueError, match="noncausal"):
        s.advance(open_ms=STEP, level_contacts={}, confirmations=["L"])
