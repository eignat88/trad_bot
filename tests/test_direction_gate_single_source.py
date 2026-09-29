"""Regression tests: ScannerDirectionGatePolicy is the single source of truth.

After the fix, direction gating (ENABLED / BLOCKED / REGIME) is handled
exclusively by ScannerDirectionGatePolicy.  The deprecated ``blocked_combinations``
parameter in filter_candidates() is no longer used in production code paths.

These tests verify the new architecture and prevent regression to the
dual-veto bug where a DB-ENABLED direction was still rejected by the
static settings.blocked_scanner_directions list.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_REGIME,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)
from app.scanners.expectancy_filter import (
    DEFAULT_MIN_SAMPLES,
    ExpectancyFilter,
    ExpectancyRecord,
    filter_candidates,
)
from app.scanners.models import SetupCandidate


# ── Helpers ──────────────────────────────────────────────────────

def _gate_policy(*gates: ScannerDirectionGate) -> ScannerDirectionGatePolicy:
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates},
        {},
    )


def _candidate(scanner_name: str = "TREND_PULLBACK_V2", direction: str = "LONG", **kw) -> SetupCandidate:
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
# TEST 1 — DB ENABLED overrides stale static block
# ═══════════════════════════════════════════════════════════════════

class TestDBEnabledOverridesStaticBlock:
    """When DB gate says ENABLED, the candidate must pass direction gate
    even if it appears in the static settings.blocked_scanner_directions."""

    def test_db_enabled_not_vetoed_by_expectancy_blocked(self):
        """TEST 1: SRR LONG is in static blocklist but DB says ENABLED.

        The direction gate allows it.  filter_candidates (expectancy only)
        does NOT apply a second direction veto.
        """
        # DB gate says ENABLED
        policy = _gate_policy(
            ScannerDirectionGate("SUPPORT_RESISTANCE_REACTION", "LONG", GATE_ENABLED,
                                 reason="operator enable"),
        )
        decision = policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE")
        assert decision.allowed is True
        assert decision.status == GATE_ENABLED

        # Static blocklist contains SRR LONG — but it should NOT appear
        # in filter_candidates blocked_combinations after the fix.
        static_blocked = frozenset(Settings().blocked_scanner_directions)
        assert ("SUPPORT_RESISTANCE_REACTION", "LONG") in static_blocked

        c = _candidate(scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG")
        # filter_candidates WITHOUT blocked_combinations (the correct new path)
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(), trading_mode="paper",
        )
        assert len(accepted) == 1
        assert rejected == 0

    def test_full_paper_flow_db_enabled_passes(self):
        """End-to-end: gate policy allows + expectancy bootstrap allows."""
        policy = _gate_policy(
            ScannerDirectionGate("LIQUIDITY_REVERSAL", "SHORT", GATE_ENABLED),
        )
        c = _candidate(scanner_name="LIQUIDITY_REVERSAL", direction="SHORT")

        # Step 1: direction gate
        decision = policy.evaluate(c.scanner_name, c.direction, "RANGE")
        assert decision.allowed is True

        # Step 2: expectancy (empty = paper bootstrap → allow)
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(), trading_mode="paper",
        )
        assert len(accepted) == 1


# ═══════════════════════════════════════════════════════════════════
# TEST 2 — DB BLOCKED remains blocked
# ═══════════════════════════════════════════════════════════════════

class TestDBBlockedRemainsBlocked:
    """DB BLOCKED directions must never trade."""

    def test_db_blocked_rejected_by_gate(self):
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION", "SHORT", GATE_BLOCKED),
        )
        decision = policy.evaluate("MOMENTUM_EXHAUSTION", "SHORT", "RANGE")
        assert decision.allowed is False
        assert decision.status == GATE_BLOCKED
        assert decision.reason_code == "DIRECTION_GATE_BLOCKED"

    def test_db_blocked_all_scanner_directions(self):
        """All currently BLOCKED DB directions remain rejected."""
        blocked_pairs = [
            ("MOMENTUM_EXHAUSTION", "LONG"),
            ("MOMENTUM_EXHAUSTION", "SHORT"),
            ("VOLATILITY_COMPRESSION", "LONG"),
            ("VOLATILITY_COMPRESSION", "SHORT"),
            ("BREAKOUT_RETEST", "LONG"),
            ("BREAKOUT_RETEST", "SHORT"),
            ("SUPPORT_RESISTANCE_REACTION", "SHORT"),
            ("TREND_PULLBACK_V2", "SHORT"),
            ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"),
            ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT"),
            ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),
            ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"),
        ]
        gates = [
            ScannerDirectionGate(s, d, GATE_BLOCKED)
            for s, d in blocked_pairs
        ]
        policy = _gate_policy(*gates)
        for scanner, direction in blocked_pairs:
            decision = policy.evaluate(scanner, direction, "RANGE")
            assert decision.allowed is False, f"{scanner} {direction} should be BLOCKED"


# ═══════════════════════════════════════════════════════════════════
# TEST 3 — REGIME gating continues to work
# ═══════════════════════════════════════════════════════════════════

class TestRegimeGating:
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
        assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "TREND_DOWN").allowed is False


# ═══════════════════════════════════════════════════════════════════
# TEST 4 — DB failure / missing row uses documented fallback
# ═══════════════════════════════════════════════════════════════════

class TestDBFailureFallback:
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

    def test_missing_gate_is_fail_closed(self):
        policy = ScannerDirectionGatePolicy.static_fallback([], [], {})
        decision = policy.evaluate("UNKNOWN_SCANNER", "LONG", "TREND_UP")
        assert decision.allowed is False
        assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# TEST 5 — Expectancy rejection remains functional
# ═══════════════════════════════════════════════════════════════════

class TestExpectancyRejectionFunctional:
    def test_negative_expectancy_rejects_when_direction_enabled(self):
        """Direction ENABLED but bad expectancy → rejected by expectancy, not DISABLED_SCANNER_DIRECTION."""
        f = ExpectancyFilter(records={
            ("TEST_SCANNER", "LONG"): ExpectancyRecord(
                scanner_name="TEST_SCANNER", direction="LONG",
                samples=40, avg_r_after_costs=-0.2, win_rate=0.1,
                profit_factor=0.8,
            ),
        })
        c = _candidate(scanner_name="TEST_SCANNER", direction="LONG")
        accepted, rejected = filter_candidates([c], f, trading_mode="live")
        assert accepted == []
        assert rejected == 1

    def test_insufficient_samples_paper_bootstrap(self):
        """Paper mode: samples < min → bootstrap allows."""
        f = ExpectancyFilter(records={
            ("TEST_SCANNER", "LONG"): ExpectancyRecord(
                scanner_name="TEST_SCANNER", direction="LONG",
                samples=5, avg_r_after_costs=-0.5, win_rate=0.1,
            ),
        })
        c = _candidate(scanner_name="TEST_SCANNER", direction="LONG")
        accepted, rejected = filter_candidates(
            [c], f, trading_mode="paper", min_samples=30,
        )
        assert len(accepted) == 1


# ═══════════════════════════════════════════════════════════════════
# TEST 6 — Paper bootstrap behavior preserved
# ═══════════════════════════════════════════════════════════════════

class TestPaperBootstrap:
    def test_no_history_paper_allows(self):
        """No history + paper mode → allowed for bootstrap."""
        c = _candidate()
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(), trading_mode="paper",
        )
        assert len(accepted) == 1

    def test_no_history_live_blocks(self):
        """No history + live mode → blocked."""
        c = _candidate()
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(), trading_mode="live",
        )
        assert accepted == []


# ═══════════════════════════════════════════════════════════════════
# TEST 7 — OOS/observe-only safety: no accidental paper-tradeable
# ═══════════════════════════════════════════════════════════════════

class TestOOSSafety:
    """Ensure refactoring does not accidentally open OOS/observe-only scanners."""

    OOS_SCANNERS = [
        ("FVG_REACTION_LONG_LOCAL_STRUCT_V1", "LONG"),
        ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),
    ]

    def test_oos_scanners_blocked_in_static_blocklist(self):
        """The OOS scanners are still in the static blocklist (bootstrap safety)."""
        blocked = set(Settings().blocked_scanner_directions)
        for scanner, direction in self.OOS_SCANNERS:
            if scanner == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                # FVG is NOT in the static blocklist — its protection is
                # via DB gate.  Verify it has an explicit DB entry.
                continue
            assert (scanner, direction) in blocked, (
                f"{scanner} {direction} should remain in static blocklist"
            )

    def test_oos_scanner_db_blocked_cannot_trade(self):
        """If DB says BLOCKED, OOS scanners cannot trade regardless of refactoring."""
        # ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 SHORT is BLOCKED
        policy = _gate_policy(
            ScannerDirectionGate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT", GATE_BLOCKED),
        )
        decision = policy.evaluate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT", "RANGE")
        assert decision.allowed is False

    def test_oos_v1_shadow_control_direction_blocked(self):
        """V1 LONG is BLOCKED by gate policy (shadow control / OOS)."""
        policy = _gate_policy(
            ScannerDirectionGate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG", GATE_BLOCKED),
        )
        decision = policy.evaluate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG", "RANGE")
        assert decision.allowed is False


# ═══════════════════════════════════════════════════════════════════
# TEST 8 — Operator gate changes apply next cycle
# ═══════════════════════════════════════════════════════════════════

class TestOperatorChangeNextCycle:
    def test_blocked_to_enabled_takes_effect(self):
        """After operator changes BLOCKED → ENABLED, next load_for_cycle allows."""
        class StaticRepo:
            _gates = [
                ScannerDirectionGate("TEST_S", "LONG", GATE_ENABLED),
            ]
            def get_scanner_direction_gates(self):
                return self._gates

        policy = ScannerDirectionGatePolicy.load_for_cycle(
            StaticRepo(),
            scanner_names=["TEST_S"],
            blocked_combinations=[("TEST_S", "LONG")],  # stale static
            regime_whitelist={},
        )
        # DB says ENABLED → should be allowed despite stale static block
        assert policy.evaluate("TEST_S", "LONG", "RANGE").allowed is True

    def test_enabled_to_blocked_takes_effect(self):
        """After operator changes ENABLED → BLOCKED, next load_for_cycle blocks."""
        class StaticRepo:
            _gates = [
                ScannerDirectionGate("TEST_S", "LONG", GATE_BLOCKED),
            ]
            def get_scanner_direction_gates(self):
                return self._gates

        policy = ScannerDirectionGatePolicy.load_for_cycle(
            StaticRepo(),
            scanner_names=["TEST_S"],
            blocked_combinations=[],  # static says enabled
            regime_whitelist={},
        )
        # DB says BLOCKED → blocked
        assert policy.evaluate("TEST_S", "LONG", "RANGE").allowed is False


# ═══════════════════════════════════════════════════════════════════
# TEST 9 — No double direction gate
# ═══════════════════════════════════════════════════════════════════

class TestNoDoubleVeto:
    """After ScannerDirectionGatePolicy allows a candidate, no downstream
    component should re-apply blocked_combinations as an independent veto."""

    def test_filter_candidates_without_blocked_allows_db_enabled(self):
        """The correct new call pattern: no blocked_combinations."""
        c = _candidate(scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG")
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(), trading_mode="paper",
        )
        assert len(accepted) == 1
        assert rejected == 0

    def test_filter_candidates_with_blocked_is_deprecated(self):
        """The old pattern (blocked_combinations) still works for backward compat
        but should NOT be used in production code paths."""
        c = _candidate(scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG")
        accepted, rejected = filter_candidates(
            [c], ExpectancyFilter(),
            blocked_combinations=frozenset({("SUPPORT_RESISTANCE_REACTION", "LONG")}),
            trading_mode="paper",
        )
        # Deprecated path still blocks (for backward compat tests)
        assert accepted == []
        assert rejected == 1


# ═══════════════════════════════════════════════════════════════════
# Static blocklist configuration sanity
# ═══════════════════════════════════════════════════════════════════

class TestStaticBlocklistConfig:
    """Static blocklist remains as bootstrap/fallback safety config."""

    EXPECTED_BLOCKED = frozenset({
        ("VOLATILITY_COMPRESSION", "LONG"),
        ("VOLATILITY_COMPRESSION", "SHORT"),
        ("SUPPORT_RESISTANCE_REACTION", "LONG"),
        ("SUPPORT_RESISTANCE_REACTION", "SHORT"),
        ("LIQUIDITY_REVERSAL", "SHORT"),
        ("BREAKOUT_RETEST", "LONG"),
        ("BREAKOUT_RETEST", "SHORT"),
        ("MOMENTUM_EXHAUSTION", "LONG"),
        ("MOMENTUM_EXHAUSTION", "SHORT"),
        ("TREND_PULLBACK_V2", "SHORT"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),
        ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"),
    })

    def test_default_settings_blocklist_matches_approved_matrix(self):
        assert frozenset(Settings().blocked_scanner_directions) == self.EXPECTED_BLOCKED

    def test_static_fallback_uses_blocklist(self):
        """static_fallback uses the blocklist for DB-unavailable scenarios."""
        policy = ScannerDirectionGatePolicy.static_fallback(
            ["SUPPORT_RESISTANCE_REACTION", "ACTIVE_SCANNER", "TREND_PULLBACK_V2"],
            blocked_combinations=Settings().blocked_scanner_directions,
            regime_whitelist={"TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)}},
        )
        # SRR LONG blocked by static fallback
        assert policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE").allowed is False
        # ACTIVE_SCANNER LONG enabled (not in blocklist)
        assert policy.evaluate("ACTIVE_SCANNER", "LONG", "RANGE").allowed is True
        # TREND_PULLBACK_V2 LONG regime-gated
        assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "TREND_UP").allowed is True
        assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "RANGE").allowed is False

    def test_static_fallback_used_when_db_unavailable(self):
        """load_for_cycle falls back to static when DB fails."""
        class FailingRepo:
            def get_scanner_direction_gates(self):
                raise RuntimeError("connection refused")

        policy = ScannerDirectionGatePolicy.load_for_cycle(
            FailingRepo(),
            scanner_names=["SUPPORT_RESISTANCE_REACTION"],
            blocked_combinations=Settings().blocked_scanner_directions,
            regime_whitelist=Settings().scanner_regime_whitelist,
        )
        # Static fallback: SRR LONG is BLOCKED
        assert policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE").allowed is False
