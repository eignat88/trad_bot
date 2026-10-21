"""Issue #176 Phase-2 mandatory test matrix (4+5): counter-free episode identity
stable across windows/restarts/warmup, and lifecycle parity with the audited
multi-level semantics (P2-06..P2-11 non-regression)."""
from __future__ import annotations

from hashlib import sha256

import pytest

from app.models import Candle
from app.research.htf_baseline_short_phase2_episode_v1 import (
    LegacyCheckpointError,
    Phase2Episodes,
    STATE_CONFIRMED,
    STATE_CONTACT,
    STATE_UNRESOLVED,
    episode_id_for,
    source_event_id_for,
)
from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes

EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
STEP = 300_000
T0 = 1_759_996_800_000  # aligned 5m open (multiple of 300000)
HOUR0 = 1_759_996_800_000  # aligned 1h open


def machine(symbol: str = "BTCUSDT") -> Phase2Episodes:
    return Phase2Episodes(experiment_id=EXP, symbol=symbol)


def contact(price: float = 110.0, available: int = 0, touched: bool = True):
    return (price, available, touched)


# --------------------------------------------------------------- identity (4)
def test_episode_identity_formula_is_exactly_the_frozen_rule():
    eid = episode_id_for(EXP, "BTCUSDT", "SHORT", "lvl-1", T0)
    expected = sha256(f"{EXP}|BTCUSDT|SHORT|lvl-1|{T0}".encode()).hexdigest()
    assert eid == expected
    assert source_event_id_for(eid) == f"{eid}:FIRST_CONFIRM"


def test_identity_has_no_mutable_counter_component():
    m1, m2 = machine(), machine()
    for m in (m1, m2):
        m.advance(open_ms=T0, level_contacts={"lvl-1": contact()}, warmup=False)
        m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    # equal causal facts -> equal identity, even across independent runs
    assert set(m1.episodes) == set(m2.episodes)


def test_identity_stable_across_warmup_length():
    short_warmup, long_warmup = machine(), machine()
    short_warmup.advance(open_ms=T0 - 2 * STEP, level_contacts={}, warmup=True)
    short_warmup.advance(open_ms=T0 - STEP, level_contacts={}, warmup=True)
    for offset in range(10, 0, -1):
        long_warmup.advance(open_ms=T0 - offset * STEP, level_contacts={}, warmup=True)
    short_warmup.advance(open_ms=T0, level_contacts={"lvl-1": contact()}, warmup=False)
    long_warmup.advance(open_ms=T0, level_contacts={"lvl-1": contact()}, warmup=False)
    assert set(short_warmup.episodes) == set(long_warmup.episodes)


def test_identity_stable_across_rolling_window_shift():
    early, late = machine(), machine()
    # `late` starts observing only from T0 (smaller effective window)
    for step in range(5):
        early.advance(open_ms=T0 + step * STEP, level_contacts={}, warmup=True)
    early.advance(open_ms=T0 + 5 * STEP, level_contacts={"lvl-1": contact()})
    late.advance(open_ms=T0 + 5 * STEP, level_contacts={"lvl-1": contact()})
    assert set(early.episodes) == set(late.episodes)


def test_identity_stable_across_restart():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    restored = Phase2Episodes.restore(m.checkpoint())
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    restored.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    assert m.checkpoint() == restored.checkpoint()


def test_recontact_identity_differs_only_by_first_touch_time():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact()})
    ids = set(m.episodes)
    assert len(ids) == 2
    expected1 = episode_id_for(EXP, "BTCUSDT", "SHORT", "lvl-1", T0)
    expected2 = episode_id_for(EXP, "BTCUSDT", "SHORT", "lvl-1", T0 + 2 * STEP)
    assert ids == {expected1, expected2}


def test_warmup_origin_flag_recorded_never_eligible_marker():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()}, warmup=True)
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    emitted = m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["lvl-1"])
    assert len(emitted) == 1
    assert emitted[0]["warmup_origin"] is True
    assert m.warmup_episode_ids == set(m.episodes)


# -------------------------------------------------------------- lifecycle (5)
def test_p2_06_confirmation_on_touching_continuation_bar_is_emitted():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    emitted = m.advance(
        open_ms=T0 + STEP, level_contacts={"lvl-1": contact()}, confirmations=["lvl-1"]
    )
    assert len(emitted) == 1
    assert emitted[0]["known_at_ms"] == T0 + 2 * STEP
    assert emitted[0]["source_event_id"] == source_event_id_for(emitted[0]["episode_id"])


def test_p2_09_recontact_same_step_rejects_confirmation():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    # re-contact AND confirmation claimed in the same step -> rejected
    emitted = m.advance(
        open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact()}, confirmations=["lvl-1"]
    )
    assert emitted == []
    # prior pending superseded, new episode pending
    reasons = sorted(r["terminal_reason"] for r in m.episodes.values() if r["terminal_reason"])
    assert reasons == ["RECONTACT_SUPERSEDED_PENDING"]
    assert m.pending_at_end == 1


def test_p2_10_same_step_supersession_never_orphans_pending():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact()}, confirmations=["lvl-1"])
    # first episode confirmed; new run opened on next touch
    m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact(touched=False)})
    m.advance(open_ms=T0 + 3 * STEP, level_contacts={"lvl-1": contact()})
    states = {eid: r["state"] for eid, r in m.episodes.items()}
    assert sorted(states.values()) == [STATE_CONFIRMED, STATE_CONTACT]


def test_p2_10_rejected_same_step_confirm_not_attributed_later():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact()}, confirmations=["lvl-1"])
    # the fresh episode may later confirm exactly once
    emitted = m.advance(
        open_ms=T0 + 3 * STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["lvl-1"]
    )
    assert len(emitted) == 1
    assert emitted[0]["first_touch_open_ms"] == T0 + 2 * STEP


def test_pending_survives_contact_run_end_and_confirms_later():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)})
    m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact(touched=False)})
    emitted = m.advance(
        open_ms=T0 + 3 * STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["lvl-1"]
    )
    assert len(emitted) == 1
    assert emitted[0]["known_at_ms"] == T0 + 4 * STEP  # never backdated


def test_unavailable_pending_evicted_as_source_window_evicted():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(
        open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)}, unavailable=["lvl-1"]
    )
    (record,) = m.episodes.values()
    assert record["state"] == STATE_UNRESOLVED
    assert record["terminal_reason"] == "SOURCE_WINDOW_EVICTED"


def test_eligibility_loss_is_non_terminal_and_preserves_pending():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)}, eligibility_loss=["lvl-1"])
    assert m.pending_at_end == 1  # pending NOT deleted
    assert all(r["terminal_reason"] is None for r in m.episodes.values())
    assert len(m.eligibility_loss_records) == 1  # diagnostic only


def test_unknown_eligibility_loss_fails_closed():
    m = machine()
    with pytest.raises(ValueError):
        m.advance(open_ms=T0, level_contacts={}, eligibility_loss=["ghost"])


def test_multi_level_same_bar_independent_emission():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"a": contact(110.0), "b": contact(120.0)})
    emitted = m.advance(
        open_ms=T0 + STEP,
        level_contacts={"a": contact(110.0, touched=False), "b": contact(120.0, touched=False)},
        confirmations=["a", "b"],
    )
    assert len(emitted) == 2
    assert [e["level_id"] for e in emitted] == ["a", "b"]  # deterministic order


def test_terminal_never_re_attributed_and_duplicate_confirm_raises():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["lvl-1"])
    with pytest.raises(ValueError):
        m.advance(
            open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["lvl-1"]
        )


def test_step_is_atomic_on_failure():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    before = m.checkpoint()
    with pytest.raises(ValueError):
        m.advance(open_ms=T0 + STEP, level_contacts={"lvl-1": contact(touched=False)}, confirmations=["ghost"])
    assert m.checkpoint() == before


def test_unaligned_overlap_gap_steps_rejected():
    m = machine()
    m.advance(open_ms=T0, level_contacts={})
    with pytest.raises(ValueError):
        m.advance(open_ms=T0 + 1, level_contacts={})
    with pytest.raises(ValueError):
        m.advance(open_ms=T0 - STEP, level_contacts={})  # overlap
    with pytest.raises(ValueError):
        m.advance(open_ms=T0 + 2 * STEP, level_contacts={})  # gap


def test_legacy_checkpoint_schema_rejected_explicitly():
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore({"schema": "HTF_176_COMBINED_CHECKPOINT_V2"})
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore({"no_schema": True})


def test_tampered_identity_rejected_on_restore():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    payload = m.checkpoint()
    eid = next(iter(payload["episodes"]))
    payload["episodes"][eid]["first_touch_open_ms"] = T0 + 999 * STEP  # tamper
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore(payload)


def test_tampered_active_map_rejected_on_restore():
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    # forged active entry pointing at a non-existent episode
    payload = m.checkpoint()
    payload["active"]["lvl-1"] = "f" * 64
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore(payload)
    # forged active entry for an unknown level
    payload = m.checkpoint()
    payload["active"]["ghost-level"] = next(iter(payload["episodes"]))
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore(payload)
    # forged active entry pointing at an UNRESOLVED (terminal) episode
    payload = m.checkpoint()
    eid = next(iter(payload["episodes"]))
    payload["episodes"][eid]["state"] = "UNRESOLVED"
    payload["episodes"][eid]["terminal_reason"] = "X"
    payload["episodes"][eid]["terminal_at_ms"] = T0 + STEP
    with pytest.raises(LegacyCheckpointError):
        Phase2Episodes.restore(payload)


# ------------------------------------------- differential parity with audited
def _projection(state_like) -> dict:
    episodes = {
        eid: (
            r["state"],
            r["terminal_reason"],
            r["first_touch_open_ms"],
            r["level_available_ms"],
            r["level_price_at_touch"],
            r["first_confirmation_ms"],
        )
        for eid, r in state_like.episodes.items()
    }
    by_causal_key = {}
    for eid, values in episodes.items():
        by_causal_key[(values[2], values[3], values[4])] = values
    # pending/active reference episodes by id; project ids through the causal key
    def ref(eid):
        values = episodes[eid]
        return (values[2], values[3], values[4])

    return {
        "episodes": by_causal_key,
        "pending": {level: ref(eid) for level, eid in state_like.pending.items()},
        "active": {level: ref(eid) for level, eid in state_like.active.items()},
    }


def test_p2_07_tied_plateau_mints_exactly_one_level():
    from app.research.htf_baseline_short_phase2_level_universe_v1 import (
        build_resistance_universe,
    )

    highs = [100.0 + (i % 3) * 0.1 for i in range(48)]
    for i in (10, 20):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    for i in (30, 31, 32):  # tied plateau -> one representative
        highs[i] = 110.0
    htf = [Candle(HOUR0 + i * 3_600_000, 100.0, h, 99.0, 100.0, 1.0) for i, h in enumerate(highs)]
    asof = int(htf[36].timestamp) + 3_600_000
    execs = [Candle(asof - (200 - i) * 300_000, 100, 100.1, 99.9, 100.05, 1.0) for i in range(200)]
    universe, _ = build_resistance_universe(htf[:37], execs, asof, symbol="BTCUSDT")
    plateau_levels = [r for r in universe if abs(r.price - 110.0) < 1e-9]
    assert len(plateau_levels) == 1
    assert plateau_levels[0].formation_close_ms == int(htf[30].timestamp)  # first plateau bar


def test_p2_11_independent_same_price_swings_are_separate_levels():
    from app.research.htf_baseline_short_phase2_level_universe_v1 import (
        build_resistance_universe,
    )

    highs = [100.0 + (i % 3) * 0.1 for i in range(48)]
    for i in (3, 7):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    highs[15] = 110.0  # independent swing A
    highs[35] = 110.0  # independent swing B, same price
    htf = [Candle(HOUR0 + i * 3_600_000, 100.0, h, 99.0, 100.0, 1.0) for i, h in enumerate(highs)]
    asof = int(htf[40].timestamp) + 3_600_000
    execs = [Candle(asof - (200 - i) * 300_000, 100, 100.1, 99.9, 100.05, 1.0) for i in range(200)]
    universe, _ = build_resistance_universe(htf[:41], execs, asof, symbol="BTCUSDT")
    same_price = [r for r in universe if abs(r.price - 110.0) < 1e-9]
    assert len(same_price) == 2  # never merged, never suppressed
    assert len({r.level_id for r in same_price}) == 2
    assert {r.formation_close_ms for r in same_price} == {int(htf[15].timestamp), int(htf[35].timestamp)}


def test_documented_r1_deviation_same_step_recontact_beats_stale_unavailable():
    """Deliberate, documented deviation from the audited research driver:
    a same-step re-contact episode is fresh and is never evicted as stale."""
    m = machine()
    m.advance(open_ms=T0, level_contacts={"lvl-1": contact()})
    emitted = m.advance(
        open_ms=T0 + STEP,
        level_contacts={"lvl-1": contact()},  # recontact (run broke? no -> continuation)
    )
    m.advance(open_ms=T0 + 2 * STEP, level_contacts={"lvl-1": contact(touched=False)})
    # recontact AND stale-unavailable in the same step
    m.advance(
        open_ms=T0 + 3 * STEP,
        level_contacts={"lvl-1": contact()},
        unavailable=["lvl-1"],
    )
    assert m.pending_at_end == 1  # fresh episode preserved
    fresh = m.episodes[m.pending["lvl-1"]]
    assert fresh["terminal_reason"] is None


def test_unavailable_without_pending_fails_closed_in_phase2_machine():
    m = machine()
    with pytest.raises(ValueError):
        m.advance(open_ms=T0, level_contacts={}, unavailable=["ghost"])


def test_lifecycle_parity_with_audited_multi_level_machine():
    """Same inputs -> same observable lifecycle semantics as the audited machine."""
    old = MultiLevelEpisodes(symbol="BTCUSDT")
    new = machine()
    steps = [
        ({"a": contact(110.0), "b": contact(120.0)}, (), (), ()),
        ({"a": contact(110.0), "b": contact(120.0, touched=False)}, ("a",), (), ()),
        ({"a": contact(110.0, touched=False), "b": contact(120.0, touched=False)}, (), (), ("a",)),
        ({"a": contact(110.0, touched=False), "b": contact(120.0, touched=False)}, ("b",), (), ()),
        ({"a": contact(110.0), "b": contact(120.0)}, (), (), ()),  # a: recontact; b: new episode
        ({"a": contact(110.0, touched=False), "b": contact(120.0)}, (), (), ()),
        ({"a": contact(110.0, touched=False), "b": contact(120.0)}, ("a",), (), ()),  # a: late FIRST_CONFIRM
        ({"a": contact(110.0, touched=False), "b": contact(120.0, touched=False)}, ("b",), (), ()),
        ({"c": contact(130.0)}, (), (), ()),
        ({"c": contact(130.0, touched=False)}, (), ("c",), ()),  # c: SOURCE_WINDOW_EVICTED
    ]
    for index, (contacts, confirms, unavailable, loss) in enumerate(steps):
        open_ms = T0 + index * STEP
        old_emitted = old.advance(
            open_ms=open_ms,
            level_contacts=dict(contacts),
            confirmations=list(confirms),
            unavailable=list(unavailable),
            eligibility_loss=list(loss),
        )
        new_emitted = new.advance(
            open_ms=open_ms,
            level_contacts=dict(contacts),
            confirmations=list(confirms),
            unavailable=list(unavailable),
            eligibility_loss=list(loss),
        )
        assert _projection(old) == _projection(new), f"divergence at step {index}"
        old_semantics = [
            (e["level_id"], e["known_at_ms"], e["first_touch_open_ms"], e["level_price_at_touch"])
            for e in old_emitted
        ]
        new_semantics = [
            (e["level_id"], e["known_at_ms"], e["first_touch_open_ms"], e["level_price_at_touch"])
            for e in new_emitted
        ]
        assert old_semantics == new_semantics, f"emission divergence at step {index}"
