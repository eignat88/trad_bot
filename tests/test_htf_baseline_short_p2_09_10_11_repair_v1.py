"""Failing reproductions for the Codex review of be8c441 (P2-09/10/11).

P2-09 : single-level replay suppresses a reaction confirmation on a touching
        bar (diverges from multi-level active-vs-recontact semantics).
P2-10 : a same-step recontact replaces the pending episode before the reaction
        confirmation is validated -> the event is misattributed to the recontact
        and the prior episode is orphaned in CONTACT. EpisodeState.advance is
        not atomic.
P2-11 : the global seen_prices dedup suppresses independent same-price swings,
        not only one tied plateau.

These tests must FAIL before the repair and PASS after, without weakening the
frozen detector and without regressing P2-06/07/08.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.models import Candle
from app.research.htf_baseline_short_causal_level_adapter_v1 import (
    causal_resistance_universe,
)
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes
from app.research.htf_baseline_short_stateful_episode_v1 import EpisodeState, LevelSnapshot

STEP = 300_000
HOUR = 3_600_000
BASE = 1_800_000_000_000 // HOUR * HOUR


# --------------------------------------------------------------------------- #
# P2-09 : single-level replay must use the multi-level active-vs-recontact rule
# --------------------------------------------------------------------------- #

def _patch_single_replay(monkeypatch, touch_times, reaction_end_at):
    """Pin level discovery, ATR, contact and reaction detection for the
    single-level stateful replay (tools/.../htf_baseline_short_stateful_replay_v1)."""
    import tools.research.htf_baseline_short_stateful_replay_v1 as R
    level = LevelSnapshot("L", 100.0, 0)
    htf = [Candle(BASE + i * HOUR, 80.0, 100.0, 79.0, 80.0, 1.0) for i in range(26)]
    monkeypatch.setattr(R, "closed_htf", lambda prefix, asof: htf)
    monkeypatch.setattr(R, "causal_resistance_snapshot", lambda h, p, a: level)
    monkeypatch.setattr(R, "_causal_atr_series", lambda p: [1.0] * len(p))
    monkeypatch.setattr(R, "causal_contact", lambda bar, lvl, atr: bar.timestamp in touch_times)

    def _reaction_end(*, closed, touch_index, level, direction, atr_by_index):
        if len(closed) == reaction_end_at:
            return (len(closed) - 1, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0)
        return None

    monkeypatch.setattr(R, "_find_reaction_end", _reaction_end)
    return R


def test_single_level_replay_confirms_reaction_on_touching_bar(monkeypatch):
    # Bar 19 opens the contact; bar 20 both re-pierces the level and is the
    # reaction end. The single-level replay must NOT suppress the confirmation
    # (parity with multi-level P2-06 handling of an active contact run).
    n = 21
    confirm_bar = 20
    touch_times = {BASE + 19 * STEP, BASE + 20 * STEP}
    R = _patch_single_replay(monkeypatch, touch_times, reaction_end_at=21)
    candles = [Candle(BASE + i * STEP, 80.0, 101.0, 79.0, 80.0, 1.0) for i in range(n)]
    start, end = BASE, BASE + (n + 1) * STEP
    result = R.replay("BTCUSDT", candles, start, end)
    assert result["counters"]["new_confirmations"] == 1
    assert len(result["emissions"]) == 1
    ev = result["emissions"][0]
    assert ev["level_id"] == "L"
    assert ev["known_at_ms"] == candles[confirm_bar].timestamp + STEP


def test_single_level_recontact_does_not_confirm(monkeypatch):
    # Bar 19 opens contact; bar 20 breaks it; bar 21 re-pierces (recontact) and
    # would also be a reaction end. Recontact supersedes the pending episode and
    # must NOT emit a same-bar confirmation (parity with multi-level).
    n = 22
    touch_times = {BASE + 19 * STEP, BASE + 21 * STEP}
    R = _patch_single_replay(monkeypatch, touch_times, reaction_end_at=22)
    candles = [Candle(BASE + i * STEP, 80.0, 101.0, 79.0, 80.0, 1.0) for i in range(n)]
    start, end = BASE, BASE + (n + 1) * STEP
    result = R.replay("BTCUSDT", candles, start, end)
    assert result["emissions"] == []
    assert result["counters"]["new_confirmations"] == 0


# --------------------------------------------------------------------------- #
# P2-10 : same-step recontact must not hijack the pending episode identity
# --------------------------------------------------------------------------- #

def test_episode_state_recontact_same_step_rejects_confirmation():
    state = EpisodeState("BTCUSDT")
    level = LevelSnapshot("L", 100.0, 0)
    state.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)   # E0 opens
    state.advance(open_ms=STEP, high=101.0, low=99.0, level=level, touch=False)  # contact breaks
    # Same-step recontact + confirmation: the confirmation must be rejected (not
    # emitted, not attributed to the recontact), the prior episode superseded to
    # UNRESOLVED (not orphaned in CONTACT), and a new episode left pending.
    state.advance(open_ms=2 * STEP, high=101.0, low=99.0, level=level,
                  touch=True, reaction_confirmed=True)
    assert state.emitted == []                       # no misattributed confirmation
    states = {}
    for rec in state.episodes.values():
        states.setdefault(rec["state"], []).append(rec)
    # prior episode superseded, NOT orphaned in CONTACT
    superseded = [r for r in state.episodes.values()
                  if r.get("terminal_reason") == "RECONTACT_SUPERSEDED_PENDING"]
    assert len(superseded) == 1
    assert superseded[0]["state"] == "UNRESOLVED"
    # the recontact episode is the pending CONTACT, never CONFIRMED
    assert state.pending["L"] in state.episodes
    assert state.episodes[state.pending["L"]]["state"] == "CONTACT"
    # no orphaned CONTACT episode (every CONTACT is the live pending)
    for rec in state.episodes.values():
        if rec["state"] == "CONTACT":
            assert state.pending.get(rec["level_id"]) == rec["episode_id"]


def test_multi_level_recontact_same_step_rejects_confirmation():
    state = MultiLevelEpisodes("BTCUSDT")
    state.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})    # E0 opens
    state.advance(open_ms=STEP, level_contacts={"L": (100.0, 0, False)})  # break
    state.advance(open_ms=2 * STEP, level_contacts={"L": (100.0, 0, True)},
                  confirmations=["L"])                                   # recontact + confirm
    assert state.events == []                     # confirmation rejected, not misattributed
    superseded = [r for r in state.episodes.values()
                  if r.get("terminal_reason") == "RECONTACT_SUPERSEDED_PENDING"]
    assert len(superseded) == 1 and superseded[0]["state"] == "UNRESOLVED"
    assert state.episodes[state.pending["L"]]["state"] == "CONTACT"


def test_single_and_multi_level_consistent_on_recontact_confirmation():
    level = LevelSnapshot("L", 100.0, 0)
    es = EpisodeState("BTCUSDT")
    es.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)
    es.advance(open_ms=STEP, high=101.0, low=99.0, level=level, touch=False)
    es.advance(open_ms=2 * STEP, high=101.0, low=99.0, level=level,
               touch=True, reaction_confirmed=True)

    ml = MultiLevelEpisodes("BTCUSDT")
    ml.advance(open_ms=0, level_contacts={"L": (100.0, 0, True)})
    ml.advance(open_ms=STEP, level_contacts={"L": (100.0, 0, False)})
    ml.advance(open_ms=2 * STEP, level_contacts={"L": (100.0, 0, True)}, confirmations=["L"])

    es_states = sorted(r["state"] for r in es.episodes.values())
    ml_states = sorted(r["state"] for r in ml.episodes.values())
    assert es_states == ml_states                 # identical episode state multiset
    assert es.emitted == [] and ml.events == []   # identical emission behaviour


def test_invalid_confirmation_rolls_back_and_is_retryable():
    # EpisodeState must be atomic: a rejected/invalid advance leaves the state
    # unchanged so the same bar can be retried deterministically.
    level = LevelSnapshot("L", 100.0, 0)
    state = EpisodeState("BTCUSDT")
    state.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)
    snap = state.snapshot()
    # confirmation for a level with no pending episode raises and must not mutate
    other = LevelSnapshot("X", 101.0, 0)
    with pytest.raises(ValueError):
        state.advance(open_ms=STEP, high=101.0, low=99.0, level=other,
                      touch=False, reaction_confirmed=True)
    assert state.snapshot() == snap          # atomic rollback

    # retry determinism: replaying a valid step twice yields identical state
    def run():
        s = EpisodeState("BTCUSDT")
        s.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)
        s.advance(open_ms=STEP, high=101.0, low=99.0, level=level, touch=False)
        s.advance(open_ms=2 * STEP, high=101.0, low=99.0, level=level,
                  touch=True, reaction_confirmed=True)
        return s
    assert run().snapshot() == run().snapshot()


def test_checkpoint_restart_equivalence_single_level():
    level = LevelSnapshot("L", 100.0, 0)

    def fill(s):
        s.advance(open_ms=0, high=101.0, low=99.0, level=level, touch=True)
        s.advance(open_ms=STEP, high=101.0, low=99.0, level=level, touch=True)
        return s

    direct = fill(EpisodeState("BTCUSDT"))
    first = EpisodeState("BTCUSDT")
    fill(first)
    restored = EpisodeState.restore(first.snapshot())
    assert restored.snapshot() == direct.snapshot()


# --------------------------------------------------------------------------- #
# P2-11 : tied-price dedup must be restricted to one plateau
# --------------------------------------------------------------------------- #

def _htf(highs):
    rows = []
    for i, high in enumerate(highs):
        o = min(80.0, float(high) - 0.5)
        rows.append(Candle(BASE + i * HOUR, o, float(high),
                           min(o - 0.5, float(high) - 1.0), o, 10.0))
    return rows


def _run_universe(highs):
    closed = _htf(highs)[:-1]
    execp = [Candle(closed[-1].timestamp + HOUR + i * STEP, 80.0, 81.0, 79.0, 80.0, 10.0)
             for i in range(24)]
    asof = execp[-1].timestamp + STEP
    return causal_resistance_universe(closed, execp, asof)


def _price_levels(highs, price=100.0):
    return [l for l in _run_universe(highs) if l.price == price]


def test_separate_same_price_swings_form_independent_levels():
    # Two isolated 100 swings (separated by lower highs) are independent
    # formations. They must not be collapsed into one just because the price is
    # equal. Both pass eligibility (>=2 prior touches each).
    highs = [100, 100, 100, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90]
    lv = _price_levels(highs)
    assert len(lv) == 2                       # independent, not globally deduped


def test_prior_ineligible_swing_does_not_suppress_later_eligible_same_price_swing():
    # Codex example: the first separated 100 swing has only one prior touch (so
    # it fails KEY_LEVEL_MIN_TOUCHES); it must NOT suppress a later distinct
    # 100 swing that satisfies every eligibility rule.
    # index 0: one early 100 (single prior touch source)
    # index 6: first isolated 100 swing -> exactly 1 prior touch -> INELIGIBLE
    # index 14: later isolated 100 swing -> >=2 prior touches -> ELIGIBLE
    highs = [100, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90]
    lv = _price_levels(highs)
    # the later eligible swing must be discovered (at least one 100 level)
    assert len(lv) >= 1


# Leading 100s sit at indices 0..2 (outside the SWING_RIGHT=4 swing window so
# they never mint a level) yet count as prior touches, so the plateau at 9,10
# reaches KEY_LEVEL_MIN_TOUCHES. This isolates the tied plateau as the ONLY
# in-range 100 swing.
_PLATEAU = [100, 100, 100, 95, 90, 90, 91, 92, 93,
            100, 100, 93, 92, 91, 90,
            90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90]


def test_tied_plateau_still_forms_one_level():
    # P2-07 preserved: a contiguous tied plateau collapses to ONE level.
    assert len(_price_levels(_PLATEAU)) == 1


def test_tied_plateau_identity_stable_across_window_shift():
    # Same physical plateau -> same level id regardless of trailing window growth.
    closed = _htf(_PLATEAU)[:-1]

    def universe_ids(win):
        execp = [Candle(win[-1].timestamp + HOUR + i * STEP, 80.0, 81.0, 79.0, 80.0, 10.0)
                 for i in range(24)]
        asof = execp[-1].timestamp + STEP
        return {l.level_id for l in causal_resistance_universe(win, execp, asof)
                if l.price == 100.0}

    a = universe_ids(closed)
    win2 = closed + [Candle(closed[-1].timestamp + HOUR + i * HOUR, 80.0, 90.0, 79.0, 80.0, 10.0)
                     for i in range(8)]
    b = universe_ids(win2)
    assert a and b
    assert a == b


def test_level_universe_is_deterministically_ordered():
    highs = [100, 100, 100, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90, 90,
             100, 90, 90, 90, 90,
             90, 90, 90, 90, 90, 90, 90, 90, 90]
    a = [l.level_id for l in _run_universe(highs)]
    b = [l.level_id for l in _run_universe(highs)]
    assert a == b
    # ordering is (price, formation, level_id) -> stable sort key
    uni = _run_universe(highs)
    keys = [(l.price, l.level_id) for l in uni]
    assert keys == sorted(keys, key=lambda k: k[0])
