"""Issue #253 follow-up — recovery cooldown vs normal idle (limiter core).

Module: app/exchange/global_rate_limiter.py (GLOBAL_BYBIT_RATE_LIMITER_V1).

Root cause under test
---------------------
``_refresh()`` classified ANY wall delta greater than ``clock_step_cap_s``
(30 s) as a clock jump. Production scanner cycles run every ~300 s, so
while the breaker was OPEN every cycle re-armed
``cooldown_until_wall = wall + cooldown_for(level)`` BEFORE the
OPEN→RECOVERY transition check could fire — the breaker never left OPEN
and no recovery probe was ever admitted (VPS evidence: cycle #178,
2026-11-03 12:44:16 UTC, level 3, epoch 14, cooldown slid
12:41:17 → 12:46:16 with no fresh Bybit 10006 in that cycle).

Fix under test
--------------
The step cap now classifies the wall-vs-monotonic DIVERGENCE
(``skew = Δwall − Δmono``) persisted as the ``(last_wall, last_mono)``
pair: a normal idle gap advances both clocks together (skew ≈ 0), is
credited only up to the burst ceiling, and NEVER touches an OPEN
cooldown; a genuine wall discontinuity (wall moved, monotonic source did
not) keeps the original fail-closed behavior — no credit, cooldown
re-armed, epoch fencing untouched, single recovery probe unchanged.

Coverage (deterministic fake clock — no real time, no network):
  1. OPEN (level 3) → scanner idle cycles → cooldown never extended →
     RECOVERY → exactly one probe → 20/3-style success streak → CLOSED.
  2. Real forward wall discontinuity: no credit, cooldown re-armed,
     breaker still surfaces BREAKER_OPEN.
  3. Real backward wall discontinuity: credit frozen, cooldown neither
     shortened nor extended, later forward restore reads as a step.
  4. Restart / shared-state persistence: ``last_mono`` travels in the
     state file; a fresh instance after 300 s downtime recovers without
     any cooldown extension.
  5. Legacy state (no ``last_mono``): one fail-closed adoption refresh,
     then the divergence rule takes over — no repeated extension.
  6. Token accounting: a 300 s normal idle refills only up to the burst
     ceiling; the exact identity tokens == B + refill_total −
     total_grants still holds.
"""
from __future__ import annotations

import json

import pytest

from app.exchange import global_rate_limiter as grl
from app.exchange.global_rate_limiter import (
    GrantResult,
    GlobalBybitRateLimiter,
    LimiterConfig,
)


class FakeClock:
    """Deterministic clock: sleep() advances wall AND mono together.

    ``jump()`` simulates a wall-clock SET (NTP step / clock set) with the
    monotonic source untouched — i.e. a real clock discontinuity, exactly
    the distinction the fix has to make.
    """

    def __init__(self, wall: float = 1_700_000_000.0, mono: float = 1000.0):
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        s = max(0.0, float(seconds))
        self._wall += s
        self._mono += s

    def advance(self, seconds: float) -> None:
        """Normal elapsed time (idle or active) — both clocks move."""
        self.sleep(seconds)

    def jump(self, delta: float) -> None:
        """Wall-clock set only — a genuine discontinuity."""
        self._wall += float(delta)


def _cfg(tmp_path, **over) -> LimiterConfig:
    base = dict(enabled=True, state_path=str(tmp_path / "state.json"),
                breaker_cooldowns_s=(5.0, 30.0, 120.0),
                recovery_success_target=3)
    base.update(over)
    return LimiterConfig(**base)


def _lim(tmp_path, clock, **over) -> GlobalBybitRateLimiter:
    return GlobalBybitRateLimiter(_cfg(tmp_path, **over), clock=clock)


def _raw(tmp_path) -> dict:
    with open(tmp_path / "state.json", encoding="utf-8") as fh:
        return json.load(fh)


def _trip(lim, clock, outcome: str = "RATE_LIMIT") -> tuple[str, int]:
    """One fresh-epoch grant recorded as ``outcome`` (breaker change)."""
    out = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert out.granted, out
    assert lim.record_result(out.grant_id, "P0", outcome,
                             epoch=out.epoch) == "APPLIED"
    return out.grant_id, out.epoch


def _drive_open_level3(lim, clock) -> tuple[float, int]:
    """OPEN breaker at level 3 (cooldown 120 s), as on the VPS.

    Returns (cooldown_until_wall, epoch).
    """
    _trip(lim, clock)                      # level 1 → cooldown 5 s
    assert lim.snapshot()["level"] == 1
    clock.advance(5.0)
    p = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert p.granted and p.probe, p         # OPEN → RECOVERY → 1 probe
    assert lim.record_result(p.grant_id, "P0", "RATE_LIMIT",
                             epoch=p.epoch) == "APPLIED"   # level 2, 30 s
    assert lim.snapshot()["level"] == 2
    clock.advance(30.0)
    p = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert p.granted and p.probe, p
    assert lim.record_result(p.grant_id, "P0", "RATE_LIMIT",
                             epoch=p.epoch) == "APPLIED"   # level 3, 120 s
    snap = lim.snapshot()
    assert snap["state"] == "OPEN" and snap["level"] == 3
    assert snap["cooldown_until_wall"] == pytest.approx(
        clock.wall() + 120.0, abs=1e-6)
    return snap["cooldown_until_wall"], int(snap["epoch"])


# =====================================================================
# 1. Production replica: OPEN (level 3) + ~300 s scanner idle cycles
#    must NEVER extend the cooldown; RECOVERY must fire; exactly one
#    probe; successful probes close the breaker.
# =====================================================================
def test_scanner_idle_cycles_never_extend_open_cooldown(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    cooldown0, epoch0 = _drive_open_level3(lim, clock)

    # --- scanner cycle while the cooldown is still running (60 s gap,
    #     above the 30 s step cap — the OLD code re-armed here).
    clock.advance(60.0)
    out = lim.acquire("P0", deadline=clock.mono() + 0.3)
    assert out.result is GrantResult.BREAKER_OPEN
    assert out.reason == "breaker_cooldown_exceeds_deadline"
    snap = lim.snapshot()
    assert snap["state"] == "OPEN"
    assert snap["cooldown_until_wall"] == pytest.approx(cooldown0, abs=1e-9)
    assert snap["level"] == 3 and snap["epoch"] == epoch0   # no fencing bump

    # --- production cycle: 300 s of wall-clock idle since the trip.
    #     The cooldown (120 s) expired inside the gap → RECOVERY, with
    #     the cooldown timestamp left exactly where the trip set it.
    clock.advance(240.0)
    snap = lim.refresh()
    assert snap["state"] == "RECOVERY", snap["state"]  # OLD code: stuck OPEN
    assert snap["cooldown_until_wall"] == pytest.approx(cooldown0, abs=1e-9)
    assert snap["level"] == 3 and snap["epoch"] == epoch0
    assert snap["probe"] is None

    # --- exactly ONE recovery probe (§3.1); a second attempt is refused
    #     while the first holder is live.
    p1 = lim.acquire("P0", deadline=clock.mono() + 5.0)
    assert p1.granted and p1.probe, p1
    assert lim.prepare_send(p1.grant_id) is True
    second = lim.acquire("P0", deadline=clock.mono() + 0.2)
    assert second.result is GrantResult.SKIPPED      # single probe enforced
    assert lim.snapshot()["probe"]["holder"] == p1.grant_id

    # --- successful probes walk the streak to CLOSED (target = 3).
    assert lim.record_result(p1.grant_id, "P0", "OK",
                             epoch=p1.epoch) == "APPLIED"
    for _ in range(2):
        px = lim.acquire("P0", deadline=clock.mono() + 5.0)
        assert px.granted and px.probe, px
        assert lim.prepare_send(px.grant_id) is True
        assert lim.record_result(px.grant_id, "P0", "OK",
                                 epoch=px.epoch) == "APPLIED"
    snap = lim.snapshot()
    assert snap["state"] == "CLOSED"
    assert snap["level"] == 0 and snap["success_streak"] == 0
    # cooldown was never moved by ANY of the idle cycles
    assert snap["cooldown_until_wall"] == pytest.approx(cooldown0, abs=1e-9)


# =====================================================================
# 2. Real FORWARD wall discontinuity: no credit, cooldown re-armed,
#    breaker still fail-closed (unchanged contract, new classification).
# =====================================================================
def test_forward_wall_step_rearms_cooldown_and_credits_nothing(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    _trip(lim, clock)                            # OPEN level 1 at T, cd 5 s
    before = lim.snapshot()
    tokens_before = float(before["tokens"])
    epoch_before = int(before["epoch"])
    assert before["cooldown_until_wall"] == pytest.approx(
        clock.wall() + 5.0, abs=1e-6)

    clock.jump(3600)                             # wall set +1 h, mono fixed
    snap = lim.refresh()
    assert snap["state"] == "OPEN"               # not tricked into RECOVERY
    # exact re-arm: full cooldown measured from the jumped wall
    assert snap["cooldown_until_wall"] == pytest.approx(
        clock.wall() + 5.0, abs=1e-6)
    assert float(snap["tokens"]) == pytest.approx(tokens_before)  # no mint
    assert int(snap["epoch"]) == epoch_before    # extension ≠ a trip

    out = lim.acquire("P0", deadline=clock.mono() + 0.3)
    assert out.result is GrantResult.BREAKER_OPEN # still fail-closed
    st = grl._validate(_raw(tmp_path))
    assert abs(float(st["tokens"]) -
               (12.0 + float(st["refill_total"]) -
                int(st["total_grants"]))) < 1e-6  # exact token identity


# =====================================================================
# 3. Real BACKWARD wall discontinuity: credit frozen, cooldown neither
#    shortened nor re-armed; the later forward restore reads as a step.
# =====================================================================
def test_backward_wall_step_freezes_credit_and_keeps_cooldown(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    _trip(lim, clock)                            # OPEN level 1 at T, cd 5 s
    before = lim.snapshot()
    tokens_before = float(before["tokens"])
    cooldown0 = float(before["cooldown_until_wall"])
    epoch0 = int(before["epoch"])

    clock.jump(-3600)                            # wall set −1 h, mono fixed
    snap = lim.refresh()
    assert snap["state"] == "OPEN"
    # never shortened by the backward set, never extended either
    assert float(snap["cooldown_until_wall"]) == pytest.approx(
        cooldown0, abs=1e-9)
    assert float(snap["tokens"]) == pytest.approx(tokens_before)  # frozen
    assert int(snap["epoch"]) == epoch0

    clock.jump(+4000)                            # restore, overshooting T
    snap = lim.refresh()
    assert snap["state"] == "OPEN"               # re-arm beats the restore:
    assert float(snap["cooldown_until_wall"]) == pytest.approx(
        clock.wall() + 5.0, abs=1e-6)            # without it, wall+0 would
                                                 # read as expired cooldown
    assert float(snap["tokens"]) == pytest.approx(tokens_before)  # no mint


# =====================================================================
# 4. Restart / shared-state persistence: a NEW instance after 300 s of
#    downtime recovers with the persisted baseline — no extension.
# =====================================================================
def test_restart_after_idle_recovers_without_cooldown_extension(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    _trip(lim, clock)                            # OPEN level 1 at T, cd 5 s
    cooldown0 = float(lim.snapshot()["cooldown_until_wall"])

    # --- service restart (fresh object, same shared state file) ...
    lim2 = GlobalBybitRateLimiter(_cfg(tmp_path), clock=clock)
    # ... stayed DOWN for 300 s of normal elapsed time
    clock.advance(300.0)
    snap = lim2.refresh()
    assert snap["state"] == "RECOVERY"
    assert float(snap["cooldown_until_wall"]) == pytest.approx(
        cooldown0, abs=1e-9)                     # persisted pair, no re-arm

    st = grl._validate(_raw(tmp_path))
    assert isinstance(st.get("last_mono"), (int, float))  # traveled across

    # and the restarted instance can complete recovery normally
    p = lim2.acquire("P0", deadline=clock.mono() + 5.0)
    assert p.granted and p.probe, p
    assert lim2.record_result(p.grant_id, "P0", "OK",
                              epoch=p.epoch) == "APPLIED"
    for _ in range(2):
        px = lim2.acquire("P0", deadline=clock.mono() + 5.0)
        assert px.granted and px.probe, px
        assert lim2.record_result(px.grant_id, "P0", "OK",
                                  epoch=px.epoch) == "APPLIED"
    assert lim2.snapshot()["state"] == "CLOSED"


# =====================================================================
# 5. Legacy state (written before this fix): ONE fail-closed adoption
#    refresh keeps the original wall-only heuristic, then the
#    divergence rule takes over — extension never repeats.
# =====================================================================
def test_legacy_state_adopts_mono_baseline_then_recovers(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock)
    _trip(lim, clock)                            # OPEN level 1 at T, cd 5 s
    cooldown0 = float(lim.snapshot()["cooldown_until_wall"])

    # --- downgrade the state to pre-fix shape (no last_mono), keeping
    #     the checksum valid exactly like an old build would write it.
    doc = _raw(tmp_path)
    assert "last_mono" in doc
    doc.pop("last_mono")
    doc["checksum"] = grl._checksum(doc)
    (tmp_path / "state.json").write_text(
        json.dumps(doc, sort_keys=True, separators=(",", ":")),
        encoding="utf-8")

    # 300 s idle against a legacy baseline: the wall-only heuristic can't
    # tell this from a clock step, so it keeps the fail-closed extension
    # ONCE (never weakened at the upgrade boundary).
    clock.advance(300.0)
    snap = lim.refresh()
    assert snap["state"] == "OPEN"
    assert float(snap["cooldown_until_wall"]) == pytest.approx(
        clock.wall() + 5.0, abs=1e-6)            # extended exactly once
    st = grl._validate(_raw(tmp_path))
    assert isinstance(st.get("last_mono"), (int, float))  # baseline adopted

    # From here the divergence rule governs: no second extension, and the
    # now-expired cooldown transitions to RECOVERY on the next cycle.
    cooldown1 = float(snap["cooldown_until_wall"])
    clock.advance(300.0)
    snap = lim.refresh()
    assert snap["state"] == "RECOVERY"
    assert float(snap["cooldown_until_wall"]) == pytest.approx(
        cooldown1, abs=1e-9)                     # untouched after adoption


# =====================================================================
# 6. Token accounting: a 300 s NORMAL idle gap refills only up to the
#    burst ceiling (never mints), and the exact identity holds.
# =====================================================================
def test_normal_idle_gap_refills_only_up_to_burst(tmp_path):
    clock = FakeClock()
    lim = _lim(tmp_path, clock, g_rps=8.0, burst=12.0)
    for _ in range(12):                           # drain while CLOSED
        out = lim.acquire("P1", deadline=clock.mono() + 5.0)
        assert out.granted, out
        # terminal results keep the registry on its normal production
        # lifecycle (and make the post-idle refresh's write observable)
        assert lim.record_result(out.grant_id, "P1", "OK",
                                 epoch=out.epoch) == "APPLIED"
    assert float(lim.snapshot()["tokens"]) < 1.0

    clock.advance(300.0)                          # scanner-style idle gap
    snap = lim.refresh()
    # 8 r/s × 300 s = 2400 requests of credit requested, capped at B=12
    assert float(snap["tokens"]) == pytest.approx(12.0, abs=1e-9)
    assert float(snap["refill_total"]) == pytest.approx(12.0, abs=1e-9)
    assert float(snap["tokens"]) == pytest.approx(
        12.0 + float(snap["refill_total"]) - int(snap["total_grants"]),
        abs=1e-6)                                 # exact identity
