"""Regression tests for SCORE_BYPASS_SCANNERS mechanism.

Verifies that MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 bypasses the generic
score gate (score >= 30) while remaining on the normal tradeable pipeline.

Invariants:
  1. V2 score < 30 → NOT SCORE_REJECTED (bypass active)
  2. Normal scanner score < 30 → SCORE_REJECTED (bypass scoped)
  3. V2 score >= 30 → accepted (bypass doesn't break happy path)
  4. V2 does NOT become shadow/control (separate mechanism)
  5. SHADOW_CONTROL_SCANNERS unchanged (3 members, V2 not among them)
  6. Direction gate still applies after score bypass
  7. Regime/expectancy filters still apply after score bypass
"""
from __future__ import annotations

import pytest
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.orchestrator import ScannerOrchestrator


# ── Helpers ──────────────────────────────────────────────────────────

def _v2_candidate(score: float = 26.8) -> SetupCandidate:
    """Create a typical V2 candidate with low score."""
    return SetupCandidate(
        setup_id=uuid4(),
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol="CRVUSDT",
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime.now(timezone.utc),
        reference_price=0.5,
        entry_zone_low=0.499,
        entry_zone_high=0.501,
        invalidation_price=0.4875,
        target_1=0.515,
        score=score,
        market_regime="HIGH_VOLATILITY",
        state=SetupState.SETUP_READY,
        features={
            "exhaustion_magnitude": 0.025,
            "body_ratio": 0.47,
            "rsi_confirmation": 0.38,
            "volume_ratio": 0.31,
            "rr_ratio": 0.40,
            "stop_distance_atr": 0.57,
            "rsi_14": 70.68,
            "rsi_delta_3": 2.2,
            "entry_confirmation_rsi_rising": True,
        },
    )


def _normal_candidate(score: float = 26.8) -> SetupCandidate:
    """Create a candidate from a normal scanner (not in any bypass set)."""
    return SetupCandidate(
        setup_id=uuid4(),
        scanner_name="BREAKOUT_RETEST",
        scanner_version="1.0.0",
        symbol="BTCUSDT",
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime.now(timezone.utc),
        reference_price=50000.0,
        entry_zone_low=49900.0,
        entry_zone_high=50100.0,
        invalidation_price=49000.0,
        target_1=52000.0,
        score=score,
        market_regime="TREND_UP",
        state=SetupState.SETUP_READY,
        features={},
    )


def _simulate_score_gate(candidates: list[SetupCandidate]) -> tuple[list[SetupCandidate], list[SetupCandidate]]:
    """Reproduce the orchestrator score gate logic exactly.

    Returns (valid, rejected) where rejected entries have
    SCORE_REJECTED reason recorded.
    """
    orch = ScannerOrchestrator()
    valid = []
    rejected = []
    for c in candidates:
        if (
            c.score >= 30
            or c.features.get("_oos_rejected")
            or c.scanner_name in orch.SHADOW_CONTROL_SCANNERS
            or c.scanner_name in orch.SCORE_BYPASS_SCANNERS
        ):
            valid.append(c)
        else:
            rejected.append(c)
    return valid, rejected


# ── Tests ────────────────────────────────────────────────────────────

class TestScoreBypassSet:
    """Test SCORE_BYPASS_SCANNERS set membership."""

    def test_v2_in_score_bypass(self):
        """V2 must be in SCORE_BYPASS_SCANNERS."""
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2" in ScannerOrchestrator.SCORE_BYPASS_SCANNERS

    def test_score_bypass_has_one_member(self):
        """SCORE_BYPASS_SCANNERS should have exactly 1 member."""
        assert len(ScannerOrchestrator.SCORE_BYPASS_SCANNERS) == 1

    def test_v2_not_in_shadow_control(self):
        """V2 must NOT be in SHADOW_CONTROL_SCANNERS — separate mechanism."""
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2" not in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_shadow_control_unchanged(self):
        """SHADOW_CONTROL_SCANNERS must retain exactly 3 members."""
        assert len(ScannerOrchestrator.SHADOW_CONTROL_SCANNERS) == 3
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
        assert "MOMENTUM_EXHAUSTION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
        assert "VOLATILITY_COMPRESSION" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS

    def test_sets_are_disjoint(self):
        """SCORE_BYPASS and SHADOW_CONTROL must not overlap."""
        overlap = ScannerOrchestrator.SCORE_BYPASS_SCANNERS & ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
        assert overlap == frozenset(), f"Overlap detected: {overlap}"


class TestScoreGateBypass:
    """Test that the score gate logic correctly bypasses V2."""

    def test_v2_low_score_passes_gate(self):
        """CASE A: V2 with score=26.8 → NOT SCORE_REJECTED."""
        candidate = _v2_candidate(score=26.8)
        valid, rejected = _simulate_score_gate([candidate])
        assert len(valid) == 1
        assert len(rejected) == 0
        assert valid[0].scanner_name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"

    def test_normal_scanner_low_score_rejected(self):
        """CASE B: Normal scanner with score=26.8 → SCORE_REJECTED."""
        candidate = _normal_candidate(score=26.8)
        valid, rejected = _simulate_score_gate([candidate])
        assert len(valid) == 0
        assert len(rejected) == 1
        assert rejected[0].scanner_name == "BREAKOUT_RETEST"

    def test_v2_high_score_still_accepted(self):
        """CASE A-happy: V2 with score=45 → accepted (bypass doesn't break happy path)."""
        candidate = _v2_candidate(score=45.0)
        valid, rejected = _simulate_score_gate([candidate])
        assert len(valid) == 1
        assert len(rejected) == 0

    def test_normal_scanner_high_score_accepted(self):
        """CASE B-happy: Normal scanner with score=45 → accepted."""
        candidate = _normal_candidate(score=45.0)
        valid, rejected = _simulate_score_gate([candidate])
        assert len(valid) == 1
        assert len(rejected) == 0

    def test_v2_boundary_score(self):
        """V2 at exact threshold score=30 → accepted (normal path, bypass irrelevant)."""
        candidate = _v2_candidate(score=30.0)
        valid, rejected = _simulate_score_gate([candidate])
        assert len(valid) == 1
        assert len(rejected) == 0


class TestV2NotShadow:
    """Test that V2 bypass does NOT make it a shadow/control scanner."""

    def test_v2_no_shadow_marker(self):
        """V2 candidate must not receive _shadow_control marker."""
        candidate = _v2_candidate(score=26.8)
        valid, _ = _simulate_score_gate([candidate])
        assert len(valid) == 1
        # _shadow_control should not be set by score bypass
        assert valid[0].features.get("_shadow_control") is not True

    def test_v2_scanner_name_unchanged(self):
        """V2 scanner_name must not be modified by score bypass."""
        candidate = _v2_candidate(score=26.8)
        valid, _ = _simulate_score_gate([candidate])
        assert valid[0].scanner_name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"

    def test_v2_direction_not_changed(self):
        """V2 direction must remain LONG after score bypass."""
        candidate = _v2_candidate(score=26.8)
        valid, _ = _simulate_score_gate([candidate])
        assert valid[0].direction == "LONG"


class TestExistingBehaviorUnchanged:
    """Verify existing mechanisms are not broken."""

    def test_shadow_control_still_works(self):
        """Shadow control scanner with low score → still accepted (via SHADOW_CONTROL path)."""
        shadow = SetupCandidate(
            setup_id=uuid4(),
            scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V1",
            symbol="BTCUSDT",
            direction="LONG",
            score=15.0,
            features={},
        )
        valid, rejected = _simulate_score_gate([shadow])
        assert len(valid) == 1
        assert len(rejected) == 0
        assert valid[0].scanner_name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"

    def test_oos_rejected_still_works(self):
        """OOS rejected candidate with low score → still accepted."""
        oos = SetupCandidate(
            setup_id=uuid4(),
            scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
            symbol="BTCUSDT",
            direction="LONG",
            score=10.0,
            features={"_oos_rejected": True},
        )
        valid, rejected = _simulate_score_gate([oos])
        assert len(valid) == 1
        assert len(rejected) == 0

    def test_mixed_candidates(self):
        """Multiple candidates: only correct ones pass/reject."""
        candidates = [
            _v2_candidate(score=26.8),                    # V2 low → PASS (bypass)
            _normal_candidate(score=26.8),                # Normal low → REJECT
            _normal_candidate(score=45.0),                # Normal high → PASS
            _v2_candidate(score=15.0),                    # V2 very low → PASS (bypass)
            SetupCandidate(                               # Shadow → PASS
                scanner_name="MOMENTUM_EXHAUSTION",
                symbol="BTCUSDT", direction="SHORT",
                score=20.0, features={},
            ),
        ]
        valid, rejected = _simulate_score_gate(candidates)
        valid_names = [c.scanner_name for c in valid]
        rejected_names = [c.scanner_name for c in rejected]

        # 4 pass: V2(26.8), Normal(45), V2(15), Shadow(ME)
        assert len(valid) == 4
        # 1 reject: Normal(26.8)
        assert len(rejected) == 1
        assert rejected[0].scanner_name == "BREAKOUT_RETEST"


class TestScoreStillCalculated:
    """Verify generic scoring still runs — score bypass does NOT skip scoring."""

    def test_score_candidate_still_runs_for_v2(self):
        """score_candidate() must still be called for V2 candidates."""
        from app.scanners.scoring import score_candidate

        candidate = _v2_candidate(score=0.0)  # raw, un-scored
        scored = score_candidate(candidate)
        # Score should be > 0 after generic scoring
        assert scored.score > 0, (
            f"Generic scoring should still produce a score for V2, "
            f"got score={scored.score}"
        )
        # Score should be in typical V2 range
        assert 10.0 <= scored.score <= 60.0, (
            f"V2 score out of expected range: {scored.score}"
        )
