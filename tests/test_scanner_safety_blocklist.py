"""Regression tests for the production scanner safety blocklist.

The static blocklist in Settings().blocked_scanner_directions serves as
BOOTSTRAP / FALLBACK safety configuration.  It is used by:

  1. ScannerDirectionGatePolicy.static_fallback() — when DB is unavailable
  2. scanner_runner sync_scanner_direction_gate() — initial DB seeding

In normal production runtime, ScannerDirectionGatePolicy (DB-aware) is the
SINGLE source of truth for direction gating.  The static blocklist should
NOT be passed as blocked_combinations to filter_candidates() in production
code paths.  This prevents the dual-veto bug where a DB-ENABLED direction
was incorrectly rejected by the stale static list.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates
from app.scanners.models import SetupCandidate


EXPECTED_BLOCKED_COMBINATIONS = frozenset({
    ("VOLATILITY_COMPRESSION", "LONG"),
    ("VOLATILITY_COMPRESSION", "SHORT"),
    ("SUPPORT_RESISTANCE_REACTION", "LONG"),
    ("SUPPORT_RESISTANCE_REACTION", "SHORT"),
    ("LIQUIDITY_REVERSAL", "SHORT"),
    ("BREAKOUT_RETEST", "LONG"),
    ("BREAKOUT_RETEST", "SHORT"),
    ("MOMENTUM_EXHAUSTION", "LONG"),
    ("MOMENTUM_EXHAUSTION", "SHORT"),  # Blocked for reverse long experiment
    ("TREND_PULLBACK_V2", "SHORT"),
    ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"),  # Blocked for OOS validation
    ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT"),  # Reverse long only trades LONG
    ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),  # Reverse long V2 only trades LONG
    ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"),  # OOS scanner only trades LONG
})


@pytest.fixture
def candidate() -> SetupCandidate:
    return SetupCandidate(
        scanner_name="TREND_PULLBACK_V2",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        reference_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=95.0,
        target_1=110.0,
        score=50.0,
    )


# ── Configuration integrity ──────────────────────────────────────

def test_default_safety_blocklist_matches_approved_matrix():
    """Settings() default blocklist is the approved safety matrix."""
    assert frozenset(Settings().blocked_scanner_directions) == EXPECTED_BLOCKED_COMBINATIONS


def test_production_config_safety_blocklist_matches_approved_matrix():
    """Production config.yaml blocklist matches the approved matrix."""
    root = Path(__file__).resolve().parent.parent
    settings = load_settings(path=root / "config.yaml", env_file=root / ".env.missing")

    assert settings.expectancy_filter_enabled is True
    assert frozenset(settings.blocked_scanner_directions) == EXPECTED_BLOCKED_COMBINATIONS


# ── Static fallback behavior ─────────────────────────────────────

@pytest.mark.parametrize("scanner_name,direction", sorted(EXPECTED_BLOCKED_COMBINATIONS))
def test_static_fallback_blocks_listed_combinations(scanner_name, direction):
    """static_fallback blocks all combinations in the approved matrix."""
    policy = ScannerDirectionGatePolicy.static_fallback(
        [scanner_name],
        blocked_combinations=EXPECTED_BLOCKED_COMBINATIONS,
        regime_whitelist={},
    )
    decision = policy.evaluate(scanner_name, direction, "RANGE")
    assert decision.allowed is False
    assert decision.status == GATE_BLOCKED


def test_static_fallback_allows_unlisted_combinations():
    """Combinations NOT in the static blocklist are allowed."""
    policy = ScannerDirectionGatePolicy.static_fallback(
        ["ACTIVE_SCANNER"],
        blocked_combinations=EXPECTED_BLOCKED_COMBINATIONS,
        regime_whitelist={},
    )
    decision = policy.evaluate("ACTIVE_SCANNER", "LONG", "RANGE")
    assert decision.allowed is True
    assert decision.status == GATE_ENABLED


# ── Deprecated API backward compatibility ─────────────────────────

@pytest.mark.parametrize("scanner_name,direction", sorted(EXPECTED_BLOCKED_COMBINATIONS))
def test_deprecated_blocked_combinations_still_works(candidate, scanner_name, direction):
    """The deprecated blocked_combinations param in filter_candidates still
    blocks for backward-compatible test infrastructure."""
    blocked_candidate = SetupCandidate(**{
        **candidate.__dict__,
        "scanner_name": scanner_name,
        "direction": direction,
    })

    accepted, rejected = filter_candidates(
        [blocked_candidate],
        ExpectancyFilter(),
        blocked_combinations=EXPECTED_BLOCKED_COMBINATIONS,
        trading_mode="paper",
    )

    assert accepted == []
    assert rejected == 1


# ── DB-aware gate overrides static blocklist ──────────────────────

def test_db_enabled_overrides_static_blocklist():
    """DB ENABLED direction is NOT blocked even if in static blocklist.
    This is the core fix for the dual-veto bug.
    """
    policy = ScannerDirectionGatePolicy(
        gates={
            ("SUPPORT_RESISTANCE_REACTION", "LONG"): ScannerDirectionGate(
                "SUPPORT_RESISTANCE_REACTION", "LONG", GATE_ENABLED,
                reason="operator enable",
            ),
        },
        fallback_gates={},  # no fallback needed — DB gate is authority
    )
    decision = policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "RANGE")
    assert decision.allowed is True
    assert decision.status == GATE_ENABLED

    # filter_candidates without blocked_combinations (correct new path)
    c = SetupCandidate(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        reference_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=95.0,
        target_1=110.0,
        score=50.0,
    )
    accepted, rejected = filter_candidates([c], ExpectancyFilter(), trading_mode="paper")
    assert len(accepted) == 1
    assert rejected == 0
