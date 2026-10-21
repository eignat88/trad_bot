"""Adversarial reproductions for consolidated #176 P2 defects."""
import pytest

from app.models import Candle
from app.research.htf_baseline_short_causal_level_adapter_v1 import (
    causal_resistance_universe,
)
from app.research.htf_baseline_short_stateful_episode_v1 import EpisodeState, LevelSnapshot
from tools.research.htf_baseline_short_final_readiness_audit_v1 import inspect_pair
from tools.research.htf_baseline_short_level_eligibility_audit_v1 import (
    LevelEligibilityAudit,
)
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay

STEP = 300_000
HOUR = 3_600_000
BASE = 1_800_000_000_000 // HOUR * HOUR


def test_confirmation_may_use_candle_that_also_touches_level():
    state = EpisodeState("BTCUSDT")
    level = LevelSnapshot("L", 100.0, 0)
    # The confirmation bar itself still touches the level.
    state.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)
    state.advance(open_ms=STEP, high=101.0, low=99.0, level=level, touch=True)
    state.advance(open_ms=2*STEP, high=101.0, low=80.0, level=level,
                  touch=True, reaction_confirmed=True)

    assert state.emitted[0]["known_at_ms"] == 3 * STEP


def test_multi_level_confirmation_may_use_candle_that_also_touches_level():
    from app.research.htf_baseline_short_multi_level_lifecycle_v1 import (
        MultiLevelEpisodes,
    )
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    state.advance(open_ms=STEP, level_contacts={"L": (100.0, 0, True)},
                  confirmations=["L"])
    assert state.events[0]["known_at_ms"] == 2 * STEP


def _htf(highs):
    rows = []
    for index, high in enumerate(highs):
        open_ = min(80.0, float(high) - 0.5)
        rows.append(Candle(BASE+index*HOUR, open_, float(high),
                           min(open_-0.5, float(high)-1.0), open_, 10.0))
    return rows


def test_tied_htf_swing_highs_form_one_resistance_level():
    # A contiguous tied plateau must be represented once, not once per tied
    # swing candidate.
    highs = [100, 90, 100, 90, 100, 90, 91, 92, 93, 100, 100, 93, 92, 91, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90]
    htf = _htf(highs)
    closed_htf = htf[:-1]
    exec_prefix = [
        Candle(closed_htf[-1].timestamp+HOUR+i*STEP, 80.0, 81.0, 79.0,
               80.0, 10.0) for i in range(24)
    ]
    asof = exec_prefix[-1].timestamp + STEP
    levels = causal_resistance_universe(closed_htf, exec_prefix, asof)
    assert len([level for level in levels if level.price == 100.0]) == 1


def test_discovery_transition_is_serialized_with_identity_and_asof():
    audit = LevelEligibilityAudit()
    level = type("Level", (), {"level_id": "L", "price": 101.0})()
    audit.advance(300_000, [level], 100.0)

    assert audit.counts["DISCOVERED"] == 1
    assert audit.transitions == [{
        "asof_ms": 300_000, "level_id": "L", "state": "DISCOVERED",
        "pending": False,
    }]


def _readiness_sample():
    return {
        "episodes": {
            "emissions": [{"event_id": "e1"}],
            "integrity": {"violations": 0, "result": "PASS"},
            "pending_at_end": 0,
            "pending_impact": {
                "eligibility_changes": 0,
                "eligibility_loss_with_pending": [],
                "confirmations_after_eligibility_loss": [],
            },
        },
        "clusters": {
            "clusters": {
                "c1": {"reaction_cluster_id": "c1", "member_event_ids": ["e1"]},
                "c2": {"reaction_cluster_id": "c2", "member_event_ids": ["e2"]},
            },
            "assignments": {"e1": "c1", "e2": "missing"},
        },
    }


def test_readiness_audit_rejects_unknown_extra_cluster_member():
    sample = _readiness_sample()
    sample["episodes"]["emissions"].append({"event_id": "e2"})
    sample["clusters"]["clusters"]["c2"]["member_event_ids"] = ["e1", "e2"]

    with pytest.raises(ValueError, match="duplicate cluster member"):
        inspect_pair("BTCUSDT", sample)


def test_readiness_audit_fails_on_assignment_to_missing_cluster():
    sample = _readiness_sample()
    sample["episodes"]["emissions"].append({"event_id": "e2"})
    with pytest.raises(ValueError, match="missing cluster"):
        inspect_pair("BTCUSDT", sample)


def test_full_replay_checkpoint_emits_pending_impact_fields():
    # Ensure later audit consumers receive the same explicit schema whether
    # or not the replay is resumed.
    candles = [
        Candle(BASE+i*STEP, 100.0, 101.0, 99.0, 100.0, 10.0)
        for i in range(20)
    ]
    result = replay("BTCUSDT", candles, BASE, BASE + 10*STEP,
                    full_universe=True, pending_impact=True)
    assert result["pending_impact"] is not None
    assert "eligibility_changes" in result["pending_impact"]


# --- P2-06 replay-level reproductions -------------------------------------
# The two state-machine tests above prove EpisodeState / MultiLevelEpisodes
# accept a confirmation on a touching bar. The suppression defect lived one
# layer up, in tools.research.htf_baseline_short_multi_level_replay_v1.replay,
# whose detector was gated on `not touch`. These tests drive replay() with a
# controlled level / contact / reaction so the gate itself is exercised.

def _patch_replay(monkeypatch, touch_times, reaction_end_at):
    """Pin level discovery, ATR, contact and reaction detection.

    reaction_end_at: the prefix length at which _find_reaction_end reports the
    reaction ending on the current (last) bar; elsewhere it reports None.
    """
    import tools.research.htf_baseline_short_multi_level_replay_v1 as R
    level = LevelSnapshot("L", 100.0, 0)
    htf = [Candle(BASE + i*HOUR, 80.0, 100.0, 79.0, 80.0, 1.0) for i in range(26)]
    monkeypatch.setattr(R, "closed_htf", lambda prefix, asof: htf)
    monkeypatch.setattr(R, "causal_resistance_snapshot", lambda h, p, a: level)
    monkeypatch.setattr(R, "causal_resistance_universe", lambda h, p, a: (level,))
    monkeypatch.setattr(R, "_causal_atr_series", lambda p: [1.0]*len(p))
    monkeypatch.setattr(R, "causal_contact",
                        lambda bar, lvl, atr: bar.timestamp in touch_times)
    def _reaction_end(*, closed, touch_index, level, direction, atr_by_index):
        if len(closed) == reaction_end_at:
            return (len(closed)-1, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)
        return None
    monkeypatch.setattr(R, "_find_reaction_end", _reaction_end)
    return R


def test_replay_confirms_reaction_on_bar_that_also_touches_level(monkeypatch, tmp_path):
    # Bar 19 opens the contact; bar 20 both re-pierces the level and is the
    # reaction end. P2-06: the confirmation must NOT be suppressed. Also
    # verifies checkpoint/restart reproduces the same emission.
    n = 21
    confirm_bar = 20
    touch_times = {BASE + 19*STEP, BASE + 20*STEP}
    R = _patch_replay(monkeypatch, touch_times, reaction_end_at=21)
    candles = [Candle(BASE + i*STEP, 80.0, 101.0, 79.0, 80.0, 1.0) for i in range(n)]
    start, end = BASE, BASE + (n+1)*STEP
    base = R.replay("BTCUSDT", candles, start, end)
    assert base["counters"]["confirmations"] == 1
    assert len(base["emissions"]) == 1
    ev = base["emissions"][0]
    assert ev["level_id"] == "L"
    assert ev["known_at_ms"] == candles[confirm_bar].timestamp + STEP
    assert ev["first_touch_open_ms"] == candles[19].timestamp
    assert ev["level_available_ms"] <= ev["first_touch_open_ms"] < ev["known_at_ms"]
    # restart parity: stop before the confirming bar, resume, compare
    ck = tmp_path/"state.json"
    R.replay("BTCUSDT", candles, start, end,
             stop_at_ms=candles[confirm_bar].timestamp + STEP, save_checkpoint=ck)
    resumed = R.replay("BTCUSDT", candles, start, end, resume_checkpoint=ck)
    assert resumed["emissions"] == base["emissions"]
    assert resumed["counters"] == base["counters"]
    assert resumed["integrity"]["violations"] == 0


def test_replay_recontact_does_not_confirm_superseded_pending(monkeypatch):
    # Bar 19 opens contact; bar 20 breaks it; bar 21 re-pierces (a re-contact)
    # and would also be a reaction end. The re-contact supersedes the pending
    # episode (UNRESOLVED) and must NOT emit a same-bar confirmation.
    n = 22
    touch_times = {BASE + 19*STEP, BASE + 21*STEP}
    R = _patch_replay(monkeypatch, touch_times, reaction_end_at=22)
    candles = [Candle(BASE + i*STEP, 80.0, 101.0, 79.0, 80.0, 1.0) for i in range(n)]
    start, end = BASE, BASE + (n+1)*STEP
    result = R.replay("BTCUSDT", candles, start, end)
    assert result["emissions"] == []
    assert result["counters"]["confirmations"] == 0
    assert "RECONTACT_SUPERSEDED_PENDING" in result["unresolved_reason_counts"]


# --- P2-07 verification: HTF level-id stability under rolling-window shift ---

def _htf_series(highs, base):
    rows = []
    for index, high in enumerate(highs):
        open_ = min(80.0, float(high) - 0.5)
        rows.append(Candle(base + index*HOUR, open_, float(high),
                           min(open_ - 0.5, float(high) - 1.0), open_, 10.0))
    return rows


def _exec_prefix(after_ms, n=24):
    return [Candle(after_ms + HOUR + i*STEP, 80.0, 81.0, 79.0, 80.0, 10.0)
            for i in range(n)]


def test_level_id_is_stable_when_rolling_window_shifts_but_swing_remains():
    # The same physical swing-high must mint the SAME level_id regardless of how
    # the rolling HTF window is framed (ids are a pure function of absolute
    # formation/available/price). Window A is the base framing; window B appends
    # trailing bars (the window back-edge advances while the swing + its
    # ±SWING_RIGHT context remain fully inside). The peak id must not change and
    # no duplicate ids may appear.
    from app.research.htf_baseline_short_causal_level_adapter_v1 import (
        causal_resistance_universe,
    )
    highs = [100, 90, 100, 90, 100, 90, 91, 92, 93, 100, 100, 93, 92, 91, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90]
    base_series = _htf_series(highs, BASE)
    win_a = base_series[:-1]                      # 27 closed bars
    win_b = base_series[:-1] + _htf_series([90]*8, base_series[-1].timestamp + HOUR)
    exec_a = _exec_prefix(win_a[-1].timestamp)
    exec_b = _exec_prefix(win_b[-1].timestamp)
    asof_a = exec_a[-1].timestamp + STEP
    asof_b = exec_b[-1].timestamp + STEP
    uni_a = causal_resistance_universe(win_a, exec_a, asof_a)
    uni_b = causal_resistance_universe(win_b, exec_b, asof_b)
    ids_a = [l.level_id for l in uni_a]
    ids_b = [l.level_id for l in uni_b]
    assert len(ids_a) == len(set(ids_a))  # no duplicate ids
    assert len(ids_b) == len(set(ids_b))
    peak_a = {l.level_id for l in uni_a if l.price == 100.0}
    peak_b = {l.level_id for l in uni_b if l.price == 100.0}
    assert peak_a and peak_b          # both framings discover the peak (non-vacuous)
    assert peak_a == peak_b           # identical id across window framing


def test_tied_plateau_mints_single_id_even_after_original_swing_eviction():
    # After the P2-07 dedup, a contiguous tied plateau collapses to ONE level.
    # Re-running with the plateau's first swing trimmed (simulating its eviction
    # from the rolling window) must NOT resurrect multiple ids for the plateau.
    from app.research.htf_baseline_short_causal_level_adapter_v1 import (
        causal_resistance_universe,
    )
    highs = [100, 90, 100, 90, 100, 90, 91, 92, 93, 100, 100, 93, 92, 91, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90]
    full = _htf_series(highs, BASE)
    closed = full[:-1]
    execp = _exec_prefix(closed[-1].timestamp)
    asof = execp[-1].timestamp + STEP
    uni = causal_resistance_universe(closed, execp, asof)
    at_100 = [l.level_id for l in uni if l.price == 100.0]
    assert len(at_100) == 1
    # Evict the original leading swing-high (index 0) from the window: the
    # plateau must still collapse to a single id, never two.
    trimmed = closed[1:]
    execp2 = _exec_prefix(trimmed[-1].timestamp)
    asof2 = execp2[-1].timestamp + STEP
    uni2 = causal_resistance_universe(trimmed, execp2, asof2)
    at_100_2 = [l.level_id for l in uni2 if l.price == 100.0]
    assert len(at_100_2) == 1


# --- P2-08 verification: counters / transitions consistency ----------------

def _audit_consistency(audit):
    from collections import Counter
    states = Counter(t["state"] for t in audit.transitions)
    reasons = Counter(t.get("reason") for t in audit.transitions
                      if t["state"] == "NOT_CURRENTLY_ELIGIBLE")
    assert audit.counts.get("DISCOVERED", 0) == states.get("DISCOVERED", 0)
    assert audit.counts.get("RE_ELIGIBLE", 0) == states.get("RE_ELIGIBLE", 0)
    assert audit.counts.get("BELOW_OR_AT_REFERENCE", 0) == reasons.get("BELOW_OR_AT_REFERENCE", 0)
    assert audit.counts.get("NOT_IN_CURRENT_ELIGIBLE_SET", 0) == reasons.get("NOT_IN_CURRENT_ELIGIBLE_SET", 0)


def test_p2_08_counters_transitions_consistent_across_warmup_window_restart():
    from types import SimpleNamespace
    from tools.research.htf_baseline_short_level_eligibility_audit_v1 import (
        LevelEligibilityAudit,
    )
    def lvl(lid, price):
        return SimpleNamespace(level_id=lid, price=price)

    # warm-up: no transitions/counts recorded, but re-entry state is preserved
    warm = LevelEligibilityAudit(record_transitions=False)
    warm.advance(300_000, [lvl("L", 101)], 100)
    warm.advance(600_000, [], 102)
    assert warm.transitions == [] and warm.counts == {}
    _audit_consistency(warm)

    # report window inheriting warm-up state, exercising all transition kinds
    report = LevelEligibilityAudit()
    report.ever_seen.update(warm.ever_seen)
    report.last_eligible = set(warm.last_eligible)
    report.advance(900_000, [lvl("L", 101)], 100)     # RE_ELIGIBLE
    report.advance(1_200_000, [lvl("M", 105)], 100)   # DISCOVERED
    report.advance(1_500_000, [], 102)                # both NOT_CURRENTLY_ELIGIBLE
    report.advance(1_800_000, [lvl("L", 101)], 100)   # RE_ELIGIBLE again
    _audit_consistency(report)
    assert report.counts.get("DISCOVERED", 0) == 1
    assert report.counts.get("RE_ELIGIBLE", 0) == 2

    # shortened window: fewer eligible levels must still keep counts==transitions
    short = LevelEligibilityAudit()
    short.advance(300_000, [lvl("X", 101)], 100)
    short.advance(600_000, [], 102)
    _audit_consistency(short)

    # restart determinism: an identical re-run reproduces counts and transitions
    def run():
        a = LevelEligibilityAudit()
        a.advance(900_000, [lvl("L", 101)], 100)
        a.advance(1_200_000, [lvl("M", 105)], 100)
        a.advance(1_500_000, [], 102)
        return a
    r1, r2 = run(), run()
    assert r1.counts == r2.counts
    assert r1.transitions == r2.transitions
