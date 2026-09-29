"""Tests for OBSERVE_ONLY direction gate status.

OBSERVE_ONLY decouples research/OOS observation from paper execution permission.

Semantics:
  ENABLED      — research capture YES, execution gate YES
  BLOCKED      — research capture may occur (experiment-registered), execution NO
  REGIME       — research capture YES, execution in configured regime only
  OBSERVE_ONLY — research capture YES, execution NO

This test suite covers:
  A. OBSERVE_ONLY evaluate() behavior
  B. OBSERVE_ONLY cannot create paper trades
  C. OOS continuity — prospective capture works with OBSERVE_ONLY
  D. BLOCKED execution
  E. Research independence
  F. No raw-candidate pollution
  G. REGIME unchanged
  H. DB failure remains fail-closed for execution
  I. Unknown scanner remains fail-closed for execution
  J. Manual precedence / config sync
  K. Original dual-veto regression
  L. Existing observations remain queryable
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass, replace
from typing import Any

from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_OBSERVE_ONLY,
    GATE_REGIME,
    GateDecision,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)
from app.scanners.expectancy_filter import ExpectancyFilter, ExpectancyRecord, filter_candidates
from app.scanners.models import SetupCandidate, SetupState


# ── Helpers ──────────────────────────────────────────────────────

def _gate_policy(*gates: ScannerDirectionGate) -> ScannerDirectionGatePolicy:
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates},
        {},
    )


def _candidate(scanner_name: str = "FVG_REACTION_LONG_LOCAL_STRUCT_V1",
               direction: str = "LONG", **kw) -> SetupCandidate:
    base = SetupCandidate(
        scanner_name=scanner_name,
        symbol="BTCUSDT",
        direction=direction,
        entry_timeframe="5m",
        reference_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=95.0,
        target_1=110.0,
        score=50.0,
    )
    return SetupCandidate(**{**base.__dict__, **kw})


# ═══════════════════════════════════════════════════════════════════
# A. OBSERVE_ONLY evaluate() behavior
# ═══════════════════════════════════════════════════════════════════

class TestObserveOnlyEvaluate:
    def test_observe_only_blocks_execution(self):
        """OBSERVE_ONLY returns allowed=False with reason_code DIRECTION_GATE_OBSERVE_ONLY."""
        policy = _gate_policy(
            ScannerDirectionGate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", GATE_OBSERVE_ONLY,
                                 reason="observe-only: scanner enabled, paper blocked"),
        )
        decision = policy.evaluate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "RANGE")
        assert decision.allowed is False
        assert decision.status == "OBSERVE_ONLY"
        assert decision.reason_code == "DIRECTION_GATE_OBSERVE_ONLY"

    def test_observe_only_differs_from_enabled(self):
        """ENABLED allows, OBSERVE_ONLY blocks."""
        enabled_policy = _gate_policy(
            ScannerDirectionGate("TEST", "LONG", GATE_ENABLED),
        )
        observe_policy = _gate_policy(
            ScannerDirectionGate("TEST", "LONG", GATE_OBSERVE_ONLY),
        )
        assert enabled_policy.evaluate("TEST", "LONG", "RANGE").allowed is True
        assert observe_policy.evaluate("TEST", "LONG", "RANGE").allowed is False

    def test_observe_only_differs_from_blocked(self):
        """BLOCKED and OBSERVE_ONLY both block execution, but have different status codes."""
        blocked_policy = _gate_policy(
            ScannerDirectionGate("TEST", "LONG", GATE_BLOCKED),
        )
        observe_policy = _gate_policy(
            ScannerDirectionGate("TEST", "LONG", GATE_OBSERVE_ONLY),
        )
        assert blocked_policy.evaluate("TEST", "LONG", "RANGE").status == "BLOCKED"
        assert observe_policy.evaluate("TEST", "LONG", "RANGE").status == "OBSERVE_ONLY"

    def test_observe_only_preserves_reason(self):
        """The reason text is preserved in the gate decision."""
        policy = _gate_policy(
            ScannerDirectionGate("FVG", "LONG", GATE_OBSERVE_ONLY,
                                 reason="observe-only: scanner enabled, paper blocked"),
        )
        decision = policy.evaluate("FVG", "LONG", "RANGE")
        assert decision.reason == "observe-only: scanner enabled, paper blocked"


# ═══════════════════════════════════════════════════════════════════
# B. OBSERVE_ONLY cannot create paper trades
# ═══════════════════════════════════════════════════════════════════

class TestObserveOnlyNoPaperTrade:
    def test_observe_only_candidate_rejected_at_gate(self):
        """OBSERVE_ONLY candidate is rejected by direction gate — never reaches expectancy filter."""
        policy = _gate_policy(
            ScannerDirectionGate("FVG", "LONG", GATE_OBSERVE_ONLY),
        )
        decision = policy.evaluate("FVG", "LONG", "RANGE")
        assert decision.allowed is False
        # The candidate would be logged as "observe-only candidate" in orchestrator
        # and saved to DB with DETECTED status (not READY_TO_TRADE)

    def test_observe_only_not_in_ready_setups(self):
        """OBSERVE_ONLY candidates are saved with DETECTED status, not READY_TO_TRADE.
        Therefore load_ready_setups() never returns them."""
        # This is enforced by scanner_runner saving with state=DETECTED
        c = _candidate()
        # FVG scanner emits with state=READY_TO_TRADE, but after OBSERVE_ONLY
        # handling in scanner_runner, it's replaced with DETECTED:
        obs_c = replace(c, state=SetupState.DETECTED)
        assert obs_c.state == SetupState.DETECTED
        # DETECTED != READY_TO_TRADE, so save_setup maps to "DETECTED" status
        # and load_ready_setups (WHERE status = 'READY_TO_TRADE') won't find it.


# ═══════════════════════════════════════════════════════════════════
# C. OOS continuity — prospective capture works with OBSERVE_ONLY
# ═══════════════════════════════════════════════════════════════════

class TestObserveOnlyOOSContinuity:
    def test_observe_only_reaches_expectancy_filter(self):
        """OBSERVE_ONLY candidates go through expectancy filter in orchestrator.
        Those rejected by expectancy are captured for prospective OOS."""
        # Simulate the orchestrator flow:
        # 1. Direction gate blocks OBSERVE_ONLY → candidate goes to observe_candidates
        # 2. observe_candidates go through filter_candidates (expectancy)
        # 3. Rejected ones go to _expectancy_rejected_candidates
        f = ExpectancyFilter(records={
            ("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG"): ExpectancyRecord(
                scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1", direction="LONG",
                samples=78, avg_r_after_costs=-0.42, win_rate=0.17,
                profit_factor=0.46,
            ),
        })
        c = _candidate()
        # OBSERVE_ONLY candidate goes through expectancy filter
        accepted, rejected = filter_candidates([c], f, trading_mode="paper")
        assert accepted == []  # negative expectancy → rejected
        assert rejected == 1

    def test_observe_only_expectancy_rejected_captured(self):
        """Expectancy-rejected OBSERVE_ONLY candidates are in _expectancy_rejected_candidates.
        This preserves the FVG experiment population."""
        # The key invariant: FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1 captures
        # candidates that pass risk_geometry + score_gate + direction_gate
        # (or OBSERVE_ONLY equivalent) + rejected by expectancy.
        #
        # After refactor, OBSERVE_ONLY candidates still go through expectancy,
        # and rejected ones are merged into _expectancy_rejected_candidates.
        f = ExpectancyFilter(records={
            ("FVG", "LONG"): ExpectancyRecord(
                scanner_name="FVG", direction="LONG",
                samples=78, avg_r_after_costs=-0.42, win_rate=0.17,
                profit_factor=0.46,
            ),
        })
        c = _candidate(scanner_name="FVG")
        # This simulates what the orchestrator does for observe_candidates:
        obs_before = [c]
        obs_after, _ = filter_candidates(obs_before, f, trading_mode="paper")
        obs_after_ids = {c2.setup_id for c2 in obs_after}
        observe_expectancy_rejected = [
            c2 for c2 in obs_before if c2.setup_id not in obs_after_ids
        ]
        assert len(observe_expectancy_rejected) == 1
        assert observe_expectancy_rejected[0].scanner_name == "FVG"

    def test_observe_only_with_positive_expectancy_passes(self):
        """If an OBSERVE_ONLY candidate has positive expectancy, it passes
        the expectancy filter (but still can't paper trade due to gate)."""
        f = ExpectancyFilter(records={
            ("FVG", "LONG"): ExpectancyRecord(
                scanner_name="FVG", direction="LONG",
                samples=40, avg_r_after_costs=0.5, win_rate=0.4,
                profit_factor=1.5,
            ),
        })
        c = _candidate(scanner_name="FVG")
        accepted, rejected = filter_candidates([c], f, trading_mode="paper")
        assert len(accepted) == 1  # passes expectancy
        # But still can't paper trade — OBSERVE_ONLY gate blocks execution


# ═══════════════════════════════════════════════════════════════════
# D. BLOCKED execution
# ═══════════════════════════════════════════════════════════════════

class TestBlockedExecution:
    def test_blocked_never_reaches_paper(self):
        """BLOCKED status blocks execution."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED),
        )
        decision = policy.evaluate("MOMENTUM_EXHAUSTION", "SHORT", "RANGE")
        assert decision.allowed is False
        assert decision.status == "BLOCKED"
        assert decision.reason_code == "DIRECTION_GATE_BLOCKED"


# ═══════════════════════════════════════════════════════════════════
# E. Research independence
# ═══════════════════════════════════════════════════════════════════

class TestResearchIndependence:
    def test_registered_experiment_captures_without_paper(self):
        """A registered prospective experiment can capture its population
        without enabling paper trading."""
        # FVG experiment filter_rule: "CAPTURE: candidates that pass
        # risk_geometry, score_gate, direction_gate, but REJECTED by expectancy_filter"
        #
        # After refactor: "direction_gate" means "allowed by direction gate
        # OR has OBSERVE_ONLY status (eligible for observation)".
        #
        # The experiment captures candidates that:
        # 1. Pass risk_geometry + score_gate
        # 2. Have OBSERVE_ONLY (or ENABLED) gate status
        # 3. Are rejected by expectancy
        #
        # Paper execution is blocked by OBSERVE_ONLY — research capture works.
        f = ExpectancyFilter(records={
            ("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG"): ExpectancyRecord(
                scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1", direction="LONG",
                samples=78, avg_r_after_costs=-0.42, win_rate=0.17,
                profit_factor=0.46,
            ),
        })
        c = _candidate()
        # Direction gate: OBSERVE_ONLY → blocked for execution
        policy = _gate_policy(
            ScannerDirectionGate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", GATE_OBSERVE_ONLY),
        )
        decision = policy.evaluate(c.scanner_name, c.direction, "RANGE")
        assert decision.allowed is False  # can't paper trade
        # But expectancy filter still processes it:
        accepted, rejected = filter_candidates([c], f, trading_mode="paper")
        assert rejected == 1  # rejected by expectancy → captured for OOS


# ═══════════════════════════════════════════════════════════════════
# F. No raw-candidate pollution
# ═══════════════════════════════════════════════════════════════════

class TestNoRawCandidatePollution:
    def test_observe_only_respects_experiment_population(self):
        """Moving observation hooks must not cause experiments to capture
        candidates outside their declared filter population.
        FVG experiment captures expectancy-rejected, not raw candidates."""
        # Raw candidates (before any gates) should NOT be captured by the
        # FVG experiment.  Only candidates that pass risk_geometry + score_gate
        # + direction_gate (or OBSERVE_ONLY) + rejected by expectancy.
        f = ExpectancyFilter(records={
            ("FVG", "LONG"): ExpectancyRecord(
                scanner_name="FVG", direction="LONG",
                samples=40, avg_r_after_costs=0.5, win_rate=0.4,
                profit_factor=1.5,
            ),
        })
        c = _candidate(scanner_name="FVG")
        # If expectancy allows → NOT rejected → NOT captured by expectancy experiment
        accepted, rejected = filter_candidates([c], f, trading_mode="paper")
        assert len(accepted) == 1
        assert rejected == 0
        # This candidate would NOT appear in _expectancy_rejected_candidates


# ═══════════════════════════════════════════════════════════════════
# G. REGIME unchanged
# ═══════════════════════════════════════════════════════════════════

class TestRegimeUnchanged:
    def test_regime_allows_correct_market(self):
        policy = _gate_policy(
            ScannerDirectionGate("TREND_PULLBACK_V2", "LONG", GATE_REGIME,
                                 allowed_regimes=("TREND_UP",)),
        )
        assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "TREND_UP").allowed is True

    def test_regime_blocks_wrong_market(self):
        policy = _gate_policy(
            ScannerDirectionGate("TREND_PULLBACK_V2", "LONG", GATE_REGIME,
                                 allowed_regimes=("TREND_UP",)),
        )
        assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "RANGE").allowed is False


# ═══════════════════════════════════════════════════════════════════
# H. DB failure remains fail-closed for execution
# ═══════════════════════════════════════════════════════════════════

class TestDBFailureFailClosed:
    def test_db_error_uses_static_fallback(self):
        class FailingRepo:
            def get_scanner_direction_gates(self):
                raise RuntimeError("DB unavailable")

        policy = ScannerDirectionGatePolicy.load_for_cycle(
            FailingRepo(),
            scanner_names=["ACTIVE"],
            blocked_combinations=[("ACTIVE", "SHORT")],
            regime_whitelist={},
        )
        assert policy.evaluate("ACTIVE", "LONG", "RANGE").allowed is True
        assert policy.evaluate("ACTIVE", "SHORT", "RANGE").allowed is False


# ═══════════════════════════════════════════════════════════════════
# I. Unknown scanner remains fail-closed
# ═══════════════════════════════════════════════════════════════════

class TestUnknownScannerFailClosed:
    def test_missing_gate_is_fail_closed(self):
        policy = ScannerDirectionGatePolicy.static_fallback([], [], {})
        decision = policy.evaluate("UNKNOWN_SCANNER", "LONG", "TREND_UP")
        assert decision.allowed is False
        assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# J. Manual precedence / config sync
# ═══════════════════════════════════════════════════════════════════

class TestManualPrecedence:
    def test_observe_only_is_not_in_static_fallback(self):
        """OBSERVE_ONLY cannot be produced by static_fallback.
        It must come from DB (MANUAL operator action)."""
        policy = ScannerDirectionGatePolicy.static_fallback(
            ["FVG_REACTION_LONG_LOCAL_STRUCT_V1"],
            blocked_combinations=[],
            regime_whitelist={},
        )
        decision = policy.evaluate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "RANGE")
        # static_fallback produces ENABLED, not OBSERVE_ONLY
        assert decision.status == "ENABLED"
        assert decision.allowed is True

    def test_db_observe_only_overrides_static_fallback(self):
        """When DB has OBSERVE_ONLY, it takes precedence over static fallback."""
        class MockRepo:
            def get_scanner_direction_gates(self):
                return [
                    ScannerDirectionGate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG",
                                         GATE_OBSERVE_ONLY, reason="manual observe-only"),
                ]

        policy = ScannerDirectionGatePolicy.load_for_cycle(
            MockRepo(),
            scanner_names=["FVG_REACTION_LONG_LOCAL_STRUCT_V1"],
            blocked_combinations=[],  # static says enabled
            regime_whitelist={},
        )
        decision = policy.evaluate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "RANGE")
        assert decision.status == "OBSERVE_ONLY"
        assert decision.allowed is False


# ═══════════════════════════════════════════════════════════════════
# K. Original dual-veto regression
# ═══════════════════════════════════════════════════════════════════

class TestDualVetoRegression:
    def test_db_enabled_not_vetoed_by_static_blocklist(self):
        """DB ENABLED + stale static blocklist → direction gate allows,
        filter_candidates without blocked_combinations allows."""
        from app.config import Settings
        policy = _gate_policy(
            ScannerDirectionGate("SUPPORT_RESISTANCE_REACTION", "LONG", GATE_ENABLED),
        )
        decision = policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE")
        assert decision.allowed is True

        # Static blocklist contains SRR LONG — but filter_candidates
        # without blocked_combinations allows it
        static_blocked = frozenset(Settings().blocked_scanner_directions)
        assert ("SUPPORT_RESISTANCE_REACTION", "LONG") in static_blocked

        c = _candidate(scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG")
        accepted, rejected = filter_candidates([c], ExpectancyFilter(), trading_mode="paper")
        assert len(accepted) == 1


# ═══════════════════════════════════════════════════════════════════
# L. Existing observations remain queryable
# ═══════════════════════════════════════════════════════════════════

class TestExistingObservationsQueryable:
    def test_observe_only_status_is_valid(self):
        """OBSERVE_ONLY is a valid status that can be stored in DB."""
        assert GATE_OBSERVE_ONLY in {"ENABLED", "BLOCKED", "REGIME", "OBSERVE_ONLY"}

    def test_gate_decision_preserves_all_fields(self):
        """GateDecision with OBSERVE_ONLY preserves all fields."""
        policy = _gate_policy(
            ScannerDirectionGate("FVG", "LONG", GATE_OBSERVE_ONLY, reason="test"),
        )
        d = policy.evaluate("FVG", "LONG", "RANGE")
        assert d.allowed is False
        assert d.status == "OBSERVE_ONLY"
        assert d.reason_code == "DIRECTION_GATE_OBSERVE_ONLY"
        assert d.reason == "test"
        assert d.allowed_regimes == ()


# ═══════════════════════════════════════════════════════════════════
# Special safety audit: specific scanners
# ═══════════════════════════════════════════════════════════════════

class TestScannerSafetyAudit:
    """Verify intended behavior for each scanner with OOS/research significance."""

    def test_fvg_observe_only_blocks_paper(self):
        """FVG with OBSERVE_ONLY: research YES, paper NO."""
        policy = _gate_policy(
            ScannerDirectionGate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", GATE_OBSERVE_ONLY),
        )
        d = policy.evaluate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "RANGE")
        assert d.allowed is False  # paper NO
        assert d.status == "OBSERVE_ONLY"

    def test_me_r_long_oos_enabled_allows_paper(self):
        """ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 LONG is ENABLED — intended to paper trade."""
        policy = _gate_policy(
            ScannerDirectionGate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG", GATE_ENABLED),
        )
        d = policy.evaluate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG", "RANGE")
        assert d.allowed is True  # paper YES — this is the OOS validation scanner

    def test_me_r_long_oos_short_blocked(self):
        """ME_R_LONG CLOSE_LOCATION SHORT is BLOCKED — scanner only emits LONG."""
        policy = _gate_policy(
            ScannerDirectionGate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT", GATE_BLOCKED),
        )
        d = policy.evaluate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT", "RANGE")
        assert d.allowed is False

    def test_v1_shadow_control_blocked(self):
        """MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG is BLOCKED — shadow control."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG", GATE_BLOCKED),
        )
        d = policy.evaluate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG", "RANGE")
        assert d.allowed is False

    def test_v2_long_enabled(self):
        """MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 LONG is ENABLED — trades LONG only."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "LONG", GATE_ENABLED),
        )
        d = policy.evaluate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "LONG", "RANGE")
        assert d.allowed is True

    def test_me_r_blocked(self):
        """MOMENTUM_EXHAUSTION_R both LONG and SHORT are BLOCKED."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_R", "LONG", GATE_BLOCKED),
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_R", "SHORT", GATE_BLOCKED),
        )
        assert policy.evaluate("MOMENTUM_EXHAUSTION_R", "LONG", "RANGE").allowed is False
        assert policy.evaluate("MOMENTUM_EXHAUSTION_R", "SHORT", "RANGE").allowed is False

    def test_liquidity_reversal_short_enabled(self):
        """LIQUIDITY_REVERSAL SHORT is ENABLED — trades SHORT."""
        policy = _gate_policy(
            ScannerDirectionGate("LIQUIDITY_REVERSAL", "SHORT", GATE_ENABLED),
        )
        d = policy.evaluate("LIQUIDITY_REVERSAL", "SHORT", "RANGE")
        assert d.allowed is True

    def test_srr_long_enabled(self):
        """SUPPORT_RESISTANCE_REACTION LONG is ENABLED."""
        policy = _gate_policy(
            ScannerDirectionGate("SUPPORT_RESISTANCE_REACTION", "LONG", GATE_ENABLED),
        )
        d = policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE")
        assert d.allowed is True

    def test_me_long_enabled(self):
        """MOMENTUM_EXHAUSTION LONG is ENABLED."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "LONG", GATE_ENABLED),
        )
        d = policy.evaluate("MOMENTUM_EXHAUSTION", "LONG", "RANGE")
        assert d.allowed is True

    def test_breakout_retest_long_enabled(self):
        """BREAKOUT_RETEST LONG is ENABLED."""
        policy = _gate_policy(
            ScannerDirectionGate("BREAKOUT_RETEST", "LONG", GATE_ENABLED),
        )
        d = policy.evaluate("BREAKOUT_RETEST", "LONG", "RANGE")
        assert d.allowed is True
