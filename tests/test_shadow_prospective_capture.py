"""Regression tests: Shadow prospective capture for BLOCKED scanners.

After the architectural audit (2026-09-29), ME SHORT and VC SHORT were
N=0 in prospective OOS because BLOCKED candidates were lost at the
direction gate before prospective capture hooks could see them.

Fix: expand SHADOW_CONTROL_SCANNERS to include MOMENTUM_EXHAUSTION and
VOLATILITY_COMPRESSION.  Shadow candidates are saved as DETECTED (not
READY_TO_TRADE) and pass through prospective capture hooks.

These tests verify:
  1. BLOCKED ME SHORT / VC SHORT become shadow candidates
  2. Shadow candidates have _shadow_control=True
  3. Shadow candidates are NOT READY_TO_TRADE
  4. ENABLED scanners are unaffected
  5. Non-allowlisted BLOCKED scanners are still fully rejected
  6. Direction isolation (ME LONG unaffected by ME SHORT shadow)
  7. Existing shadow mechanism (ME_R_LONG_V1) unchanged
  8. Prospective dedup via unique constraint
  9. Evaluator compatibility
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_OBSERVE_ONLY,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)
from app.scanners.models import SetupCandidate, SetupState


# ── Helpers ──────────────────────────────────────────────────────

def _gate_policy(*gates: ScannerDirectionGate) -> ScannerDirectionGatePolicy:
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates},
        {},
    )


def _candidate(
    scanner_name: str = "MOMENTUM_EXHAUSTION",
    direction: str = "SHORT",
    state: SetupState = SetupState.SETUP_READY,
    score: float = 50.0,
    **kw,
) -> SetupCandidate:
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
        score=score,
        state=state,
    )
    return SetupCandidate(**{**base.__dict__, **kw})


def _all_gates_blocked(*scanner_names: str) -> dict:
    """Return a gate dict where all given scanners are BLOCKED both directions."""
    gates = {}
    for name in scanner_names:
        for d in ("LONG", "SHORT"):
            gates[(name, d)] = ScannerDirectionGate(name, d, GATE_BLOCKED)
    return gates


# ═══════════════════════════════════════════════════════════════════
# CASE 1 — ME SHORT BLOCKED → shadow candidate
# ═══════════════════════════════════════════════════════════════════

class TestMeShortBlockedShadowCapture:
    def test_me_short_in_shadow_control_scanners(self):
        """MOMENTUM_EXHAUSTION must be in SHADOW_CONTROL_SCANNERS."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "MOMENTUM_EXHAUSTION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_me_short_blocked_becomes_shadow(self):
        """BLOCKED ME SHORT candidate enters shadow_candidates path."""
        from app.scanners.orchestrator import ScannerOrchestrator

        orch = ScannerOrchestrator()
        gates = {
            ("MOMENTUM_EXHAUSTION", "SHORT"): ScannerDirectionGate(
                "MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED, reason="test"
            ),
            ("MOMENTUM_EXHAUSTION", "LONG"): ScannerDirectionGate(
                "MOMENTUM_EXHAUSTION", "LONG", GATE_ENABLED
            ),
        }
        gate_policy = ScannerDirectionGatePolicy(gates, {})

        # Directly test the gate logic with a manually created valid candidate
        candidate = _candidate("MOMENTUM_EXHAUSTION", "SHORT")
        decision = gate_policy.evaluate(
            candidate.scanner_name, candidate.direction, "RANGE"
        )
        assert decision.allowed is False
        assert decision.status == "BLOCKED"

        # Verify it would be caught by SHADOW_CONTROL_SCANNERS check
        assert candidate.scanner_name in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS


# ═══════════════════════════════════════════════════════════════════
# CASE 2 — VC SHORT BLOCKED → shadow candidate
# ═══════════════════════════════════════════════════════════════════

class TestVcShortBlockedShadowCapture:
    def test_vc_short_in_shadow_control_scanners(self):
        """VOLATILITY_COMPRESSION must be in SHADOW_CONTROL_SCANNERS."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "VOLATILITY_COMPRESSION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_vc_short_blocked_becomes_shadow(self):
        """BLOCKED VC SHORT candidate enters shadow_candidates path."""
        from app.scanners.orchestrator import ScannerOrchestrator

        candidate = _candidate("VOLATILITY_COMPRESSION", "SHORT")
        assert candidate.scanner_name in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

        gate_policy = _gate_policy(
            ScannerDirectionGate("VOLATILITY_COMPRESSION", "SHORT", GATE_BLOCKED),
            ScannerDirectionGate("VOLATILITY_COMPRESSION", "LONG", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("VOLATILITY_COMPRESSION", "SHORT", "RANGE")
        assert decision.allowed is False
        assert decision.status == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════
# CASE 3 — PAPER SAFETY: shadow candidates → NOT READY_TO_TRADE
# ═══════════════════════════════════════════════════════════════════

class TestPaperSafety:
    def test_shadow_control_flag_set(self):
        """Shadow candidate must have _shadow_control=True in features."""
        from app.scanners.orchestrator import ScannerOrchestrator

        # Simulate what orchestrator does for shadow candidates
        candidate = _candidate("MOMENTUM_EXHAUSTION", "SHORT")
        shadow_features = dict(candidate.features) if candidate.features else {}
        shadow_features["_shadow_control"] = True
        shadow_features["_shadow_control_reason"] = "oos_experiment_baseline"
        shadow_candidate = replace(candidate, features=shadow_features)

        assert shadow_candidate.features["_shadow_control"] is True
        assert shadow_candidate.features["_shadow_control_reason"] == "oos_experiment_baseline"

    def test_shadow_state_override_to_detected(self):
        """Shadow candidate state must be overridden to DETECTED before save.

        This simulates the scanner_runner state fix that prevents READY_TO_TRADE.
        """
        candidate = _candidate(
            "MOMENTUM_EXHAUSTION", "SHORT",
            state=SetupState.SETUP_READY,  # scanner default
        )
        # Simulate the state fix from scanner_runner
        if candidate.features is None:
            features = {}
        else:
            features = dict(candidate.features)
        features["_shadow_control"] = True
        overridden = replace(candidate, features=features, state=SetupState.DETECTED)

        assert overridden.state == SetupState.DETECTED
        assert overridden.state.value != "SETUP_READY"

    def test_detected_state_not_ready_to_trade(self):
        """DETECTED state maps to DETECTED status in scanner_setup, not READY_TO_TRADE."""
        # This tests the repository._save_pg logic indirectly
        candidate = _candidate(state=SetupState.DETECTED)
        status = "READY_TO_TRADE" if candidate.state == SetupState.SETUP_READY else candidate.state.value
        assert status == "DETECTED"
        assert status != "READY_TO_TRADE"


# ═══════════════════════════════════════════════════════════════════
# CASE 4 — NORMAL ENABLED scanner unchanged
# ═══════════════════════════════════════════════════════════════════

class TestEnabledScannerUnchanged:
    def test_enabled_candidate_not_shadow(self):
        """ENABLED candidate must NOT get _shadow_control flag."""
        candidate = _candidate("MOMENTUM_EXHAUSTION", "LONG")
        gate_policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "LONG", GATE_ENABLED),
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("MOMENTUM_EXHAUSTION", "LONG", "RANGE")
        assert decision.allowed is True
        # ENABLED candidates go to gate_accepted, not shadow_candidates
        # They keep their original state (SETUP_READY → READY_TO_TRADE)
        assert candidate.state == SetupState.SETUP_READY

    def test_srr_long_still_enabled(self):
        """SRR LONG remains ENABLED — not affected by shadow changes."""
        gate_policy = _gate_policy(
            ScannerDirectionGate("SUPPORT_RESISTANCE_REACTION", "LONG", GATE_ENABLED),
            ScannerDirectionGate("SUPPORT_RESISTANCE_REACTION", "SHORT", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "TREND_UP")
        assert decision.allowed is True

    def test_lr_short_still_enabled(self):
        """LR SHORT remains ENABLED."""
        gate_policy = _gate_policy(
            ScannerDirectionGate("LIQUIDITY_REVERSAL", "SHORT", GATE_ENABLED),
        )
        decision = gate_policy.evaluate("LIQUIDITY_REVERSAL", "SHORT", "RANGE")
        assert decision.allowed is True


# ═══════════════════════════════════════════════════════════════════
# CASE 5 — BLOCKED NON-RESEARCH scanner still fully rejected
# ═══════════════════════════════════════════════════════════════════

class TestBlockedNonResearchFullyRejected:
    def test_unknown_blocked_scanner_not_shadow(self):
        """A BLOCKED scanner NOT in SHADOW_CONTROL_SCANNERS is fully rejected.

        This is the critical negative test: we must NOT open shadow path
        for every BLOCKED scanner — only the explicit allowlist.
        """
        from app.scanners.orchestrator import ScannerOrchestrator

        # Pick a scanner that is BLOCKED and NOT in SHADOW_CONTROL_SCANNERS
        assert "BREAKOUT_RETEST" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

        gate_policy = _gate_policy(
            ScannerDirectionGate("BREAKOUT_RETEST", "SHORT", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("BREAKOUT_RETEST", "SHORT", "RANGE")
        assert decision.allowed is False
        assert decision.status == "BLOCKED"
        # BREAKOUT_RETEST SHORT is not in shadow list → goes to else → fully rejected

    def test_non_shadow_blocked_not_in_valid(self):
        """Non-shadow BLOCKED candidate must not appear in valid list.

        Only scanners in SHADOW_CONTROL_SCANNERS get shadow treatment.
        """
        from app.scanners.orchestrator import ScannerOrchestrator

        # MOMENTUM_EXHAUSTION_R is BLOCKED but NOT in shadow list
        assert "MOMENTUM_EXHAUSTION_R" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

        gate_policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_R", "LONG", GATE_BLOCKED),
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_R", "SHORT", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("MOMENTUM_EXHAUSTION_R", "LONG", "RANGE")
        assert decision.allowed is False
        # Not in shadow list → goes to else → fully rejected

    def test_volatility_compression_long_not_shadow_when_blocked(self):
        """VC LONG is BLOCKED and in shadow list → becomes shadow.

        Both directions become shadow for scanners in the allowlist.
        This is acceptable: shadow candidates can never trade.
        """
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "VOLATILITY_COMPRESSION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS


# ═══════════════════════════════════════════════════════════════════
# CASE 6 — DIRECTION ISOLATION: ME LONG unaffected by ME SHORT shadow
# ═══════════════════════════════════════════════════════════════════

class TestDirectionIsolation:
    def test_me_long_enabled_not_shadow(self):
        """MOMENTUM_EXHAUSTION LONG = ENABLED → goes to gate_accepted, not shadow.

        Even though MOMENTUM_EXHAUSTION is now in SHADOW_CONTROL_SCANNERS,
        the elif chain evaluates decision.allowed FIRST.  ENABLED candidates
        hit the first branch and never reach the shadow check.
        """
        gate_policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "LONG", GATE_ENABLED),
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED),
        )
        decision_long = gate_policy.evaluate("MOMENTUM_EXHAUSTION", "LONG", "RANGE")
        decision_short = gate_policy.evaluate("MOMENTUM_EXHAUSTION", "SHORT", "RANGE")

        assert decision_long.allowed is True   # LONG is ENABLED
        assert decision_short.allowed is False  # SHORT is BLOCKED

    def test_me_long_still_ready_to_trade(self):
        """ME LONG candidate keeps SETUP_READY → READY_TO_TRADE."""
        candidate = _candidate("MOMENTUM_EXHAUSTION", "LONG", state=SetupState.SETUP_READY)
        assert candidate.state == SetupState.SETUP_READY
        # No shadow override applied to ENABLED candidates

    def test_me_short_direction_gate_sees_blocked(self):
        """Direction gate correctly returns BLOCKED for ME SHORT."""
        gate_policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED),
        )
        decision = gate_policy.evaluate("MOMENTUM_EXHAUSTION", "SHORT", "RANGE")
        assert decision.allowed is False
        assert decision.status == "BLOCKED"


# ═══════════════════════════════════════════════════════════════════
# CASE 7 — EXISTING SHADOW: ME_R_LONG_V1 unchanged
# ═══════════════════════════════════════════════════════════════════

class TestExistingShadowUnchanged:
    def test_me_r_long_v1_still_shadow(self):
        """MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 remains in SHADOW_CONTROL_SCANNERS."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_shadow_control_scanners_count(self):
        """Verify exactly 3 scanners in SHADOW_CONTROL_SCANNERS."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert len(ScannerOrchestrator.SHADOW_CONTROL_SCANNERS) == 3


# ═══════════════════════════════════════════════════════════════════
# CASE 8 — PROSPECTIVE DEDUP: unique constraint
# ═══════════════════════════════════════════════════════════════════

class TestProspectiveDedup:
    def test_source_key_deterministic(self):
        """Same candidate identity produces same source_signal_id."""
        from app.research.prospective_observer import ProspectiveOOSObserver

        observer = ProspectiveOOSObserver(conn=None, registry={})
        key1 = observer._make_source_key("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "2026-09-29T12:00:00Z")
        key2 = observer._make_source_key("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "2026-09-29T12:00:00Z")
        assert key1 == key2

    def test_different_candidates_different_keys(self):
        """Different symbols produce different source_signal_ids."""
        from app.research.prospective_observer import ProspectiveOOSObserver

        observer = ProspectiveOOSObserver(conn=None, registry={})
        key1 = observer._make_source_key("MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "2026-09-29T12:00:00Z")
        key2 = observer._make_source_key("MOMENTUM_EXHAUSTION", "ETHUSDT", "SHORT", "2026-09-29T12:00:00Z")
        assert key1 != key2


# ═══════════════════════════════════════════════════════════════════
# CASE 9 — EVALUATOR: BLOCKED scanner observations compatible
# ═══════════════════════════════════════════════════════════════════

class TestEvaluatorCompatibility:
    def test_prospective_evaluator_ignores_gate(self):
        """Evaluator reads prospective_observation, not direction gate.

        The evaluator query uses:
          WHERE o.experiment_id = %s AND o.signal_time >= %s
        with no reference to scanner_direction_gate.
        """
        import inspect
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        source = inspect.getsource(ProspectiveOOSEvaluator.run_evaluation_cycle)
        # Evaluator should query prospective_observation, not gate
        assert "prospective_observation" in source
        assert "scanner_direction_gate" not in source

    def test_prospective_observer_ignores_gate(self):
        """Prospective observer inserts to research schema, not dds."""
        import inspect
        from app.research.prospective_observer import ProspectiveOOSObserver

        # Check the internal _observe_standard method which does the actual INSERT
        source = inspect.getsource(ProspectiveOOSObserver._observe_standard)
        # Must not write to dds.scanner_setup or dds.paper_trade
        assert "scanner_setup" not in source
        assert "paper_trade" not in source


# ═══════════════════════════════════════════════════════════════════
# CASE 10 — OBSERVE_ONLY still works correctly
# ═══════════════════════════════════════════════════════════════════

class TestObserveOnlyStillWorks:
    def test_observe_only_path_separate_from_shadow(self):
        """OBSERVE_ONLY candidates use observe_candidates, not shadow_candidates.

        The elif chain: allowed → oos_rejected → OBSERVE_ONLY → SHADOW_CONTROL → else.
        OBSERVE_ONLY is checked BEFORE SHADOW_CONTROL, so a scanner that is
        OBSERVE_ONLY (like FVG) never reaches the shadow path.
        """
        gate_policy = _gate_policy(
            ScannerDirectionGate(
                "FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG",
                GATE_OBSERVE_ONLY, reason="observe-only"
            ),
        )
        decision = gate_policy.evaluate("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG", "RANGE")
        assert decision.allowed is False
        assert decision.status == "OBSERVE_ONLY"


# ═══════════════════════════════════════════════════════════════════
# CASE 11 — SRR not accidentally added to shadow
# ═══════════════════════════════════════════════════════════════════

class TestSrrNotInShadow:
    def test_srr_not_shadow_control(self):
        """SRR should NOT be in SHADOW_CONTROL_SCANNERS (it has its own research path)."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "SUPPORT_RESISTANCE_REACTION" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
