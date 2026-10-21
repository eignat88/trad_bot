"""Issue #176 multi-level lifecycle research-only regressions."""
import json
import pytest
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes

STEP = 300000
A = ("level-A", (101., 0, True))
B = ("level-B", (105., 0, True))


def test_two_levels_have_independent_confirmation_after_unselection():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts=dict((A,B)))
    assert s.advance(open_ms=STEP, level_contacts={
        "level-B": (105., 0, False)
    }, confirmations=["level-A"]) [0]["level_id"] == "level-A"
    assert "level-B" in s.pending
    assert len(s.events) == 1


def test_nonselected_level_remains_pending_without_timeout():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={"level-A": (101., 0, True)})
    for i in range(1, 220):
        s.advance(open_ms=i*STEP, level_contacts={})
    assert "level-A" in s.pending
    assert not s.events


def test_window_evict_marks_unresolved_not_confirmed():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={"level-A": (101., 0, True)})
    eid = s.pending["level-A"]
    s.advance(open_ms=STEP, level_contacts={}, unavailable=["level-A"])
    assert s.episodes[eid]["state"] == "UNRESOLVED"
    assert s.episodes[eid]["terminal_reason"] == "SOURCE_WINDOW_EVICTED"
    assert not s.events


def test_checkpoint_prevents_duplicate():
    s = MultiLevelEpisodes("ETHUSDT")
    s.advance(open_ms=0, level_contacts={"level-A": (101., 0, True)})
    restored = MultiLevelEpisodes.restore(json.loads(json.dumps(s.checkpoint())))
    events = restored.advance(open_ms=STEP, level_contacts={},
                              confirmations=["level-A"])
    assert len(events) == 1
    with pytest.raises(ValueError, match="overlapping"):
        restored.advance(open_ms=STEP, level_contacts={},
                         confirmations=["level-A"])
    assert len(restored.events) == 1


def test_gap_rejected():
    s = MultiLevelEpisodes("BTCUSDT")
    s.advance(open_ms=0, level_contacts={})
    with pytest.raises(ValueError, match="gap"):
        s.advance(open_ms=2*STEP, level_contacts={})


def test_level_unavailable_rejected():
    s = MultiLevelEpisodes("BTCUSDT")
    with pytest.raises(ValueError, match="not available"):
        s.advance(open_ms=0, level_contacts={"future": (105., STEP, True)})
