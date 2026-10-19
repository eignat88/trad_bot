"""Offline identity hardening unit tests for Issue #174."""
from types import SimpleNamespace
from tools.research.htf_baseline_short_episode_identity_hardening_v1 import (
    audit_episode_identity, sliding_window_audit,
)


def event(n, episode, level="L", *, censored=False, start=100):
    return {
        "event_id": f"event-{n}", "episode_id": episode, "level_id": level,
        "symbol": "BTCUSDT", "first_seen_ms": n*300000,
        "known_at_ms": n*300000, "episode_start_ms": start,
        "episode_left_censored": censored, "touch_open_ms": start,
    }


def test_first_confirmation_per_episode_suppresses_later_duplicates():
    r = audit_episode_identity([event(3, "A"), event(4, "A"), event(5, "B")])
    assert r["candidate_confirmations"] == 3
    assert r["first_confirmations_per_episode"] == 2
    assert r["suppressed_same_episode"] == 1
    assert r["first_confirmation_events"][0]["event_id"] == "event-3"


def test_separate_levels_not_coalesced():
    r = audit_episode_identity([event(3, "A", "L1"), event(4, "B", "L2")])
    assert r["first_confirmations_per_episode"] == 2


def test_left_censored_blocks_first_confirmation():
    r = audit_episode_identity([event(3, "A", censored=True)])
    assert r["first_confirmations_per_episode"] == 0
    assert r["unresolved_identity_records"] == 1


def test_episode_start_disagreement_blocks_approval():
    r = audit_episode_identity([event(3, "A", start=100),
                                event(4, "A", start=200)])
    assert r["unresolved_identity_records"] == 2


def test_collision_fails_closed():
    import pytest
    with pytest.raises(ValueError, match="collision"):
        audit_episode_identity([event(3, "A", "L1"), event(4, "A", "L2")])


def test_repeated_event_fails_closed():
    import pytest
    with pytest.raises(ValueError, match="duplicate"):
        audit_episode_identity([event(3, "A"), event(3, "A")])


def test_cross_level_confirmation_is_flagged():
    a = event(3, "A", "L1")
    b = event(4, "B", "L2")
    b["known_at_ms"] = a["known_at_ms"]
    r = audit_episode_identity([a, b])
    assert len(r["cross_level_same_confirmation"]) == 1


def test_episode_start_eviction_detected_at_rolling_boundary():
    start = 300_000*10
    bars = [SimpleNamespace(timestamp=i*300_000) for i in range(225)]
    e = event(11, "A", start=start)
    e["first_seen_ms"] = 20*300_000
    r = sliding_window_audit(bars, e, window=200)
    assert r["episode_start_exit_count"] == 1
    assert r["identity_not_guaranteed_if_start_evicted"]


def test_episode_no_eviction_in_bounded_followup():
    bars = [SimpleNamespace(timestamp=i*300_000) for i in range(100)]
    e = event(11, "A", start=10*300_000)
    e["first_seen_ms"] = 20*300_000
    r = sliding_window_audit(bars, e, window=200)
    assert r["episode_start_exit_count"] == 0
