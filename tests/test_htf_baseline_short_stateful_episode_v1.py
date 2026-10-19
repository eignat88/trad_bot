"""Research-only contract tests for #176 first-stage stateful observer."""
import json
import pytest

from app.research.htf_baseline_short_stateful_episode_v1 import (
    EpisodeState, LevelSnapshot, STEP_MS,
)

LEVEL = LevelSnapshot("confirmed-resistance-1", 100.0, 0)


def advance(state, i, level=LEVEL, touch=False, confirm=False):
    return state.advance(open_ms=i*STEP_MS, high=101.0, low=99.0,
                         level=level, touch=touch, reaction_confirmed=confirm)


def test_single_episode_first_confirmation_after_touch_ends():
    s = EpisodeState("BTCUSDT")
    assert advance(s, 0, touch=True) == []
    assert advance(s, 1, touch=True) == []
    assert advance(s, 2, touch=False) == []
    events = advance(s, 3, touch=False, confirm=True)
    assert len(events) == 1
    assert events[0]["known_at_ms"] == 4*STEP_MS
    assert s.episodes[events[0]["episode_id"]]["first_touch_open_ms"] == 0


def test_disconnected_second_touch_creates_new_episode():
    s = EpisodeState("ETHUSDT")
    advance(s, 0, touch=True)
    advance(s, 1, touch=False, confirm=True)
    first_id = s.emitted[0]["episode_id"]
    advance(s, 2, touch=True)
    second = advance(s, 3, touch=True, confirm=True)
    assert second[0]["episode_id"] != first_id
    assert len(s.emitted) == 2


def test_restart_preserves_ids_and_prevents_replay():
    s = EpisodeState("BTCUSDT")
    advance(s, 0, touch=True)
    restored = EpisodeState.restore(json.loads(json.dumps(s.snapshot())))
    emitted = advance(restored, 1, touch=False, confirm=True)
    assert len(emitted) == 1
    with pytest.raises(ValueError, match="overlapping"):
        advance(restored, 1, touch=False, confirm=True)
    assert len(restored.emitted) == 1


def test_history_eviction_does_not_erase_emitted_identity():
    s = EpisodeState("BTCUSDT")
    advance(s, 0, touch=True)
    advance(s, 1, touch=False, confirm=True)
    event_id = s.emitted[0]["episode_id"]
    for i in range(2, 221):
        advance(s, i, level=None)
    assert event_id in s.episodes
    assert len(s.emitted) == 1


def test_missing_bar_halts():
    s = EpisodeState("BTCUSDT")
    advance(s, 0)
    with pytest.raises(ValueError, match="missing"):
        advance(s, 2)


def test_future_level_not_eligible():
    s = EpisodeState("BTCUSDT")
    with pytest.raises(ValueError, match="causally available"):
        advance(s, 0, level=LevelSnapshot("future", 100.0, 2*STEP_MS), touch=True)


def test_no_historical_confirmation_without_contact():
    s = EpisodeState("BTCUSDT")
    with pytest.raises(ValueError, match="previously observed"):
        advance(s, 0, confirm=True)


def test_level_replacement_ends_old_contact():
    s = EpisodeState("BTCUSDT")
    advance(s, 0, touch=True)
    next_level = LevelSnapshot("new-resistance", 105.0, 0)
    advance(s, 1, level=next_level, touch=True)
    assert len(s.episodes) == 2
    assert len(s.active) == 1
