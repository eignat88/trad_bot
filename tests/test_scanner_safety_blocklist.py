"""Regression tests for the production scanner safety blocklist."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, load_settings
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
    ("TREND_PULLBACK", "LONG"),
    ("TREND_PULLBACK", "SHORT"),
})


@pytest.fixture
def candidate() -> SetupCandidate:
    return SetupCandidate(
        scanner_name="TREND_PULLBACK",
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


def test_default_safety_blocklist_matches_approved_matrix():
    assert frozenset(Settings().blocked_scanner_directions) == EXPECTED_BLOCKED_COMBINATIONS


def test_production_config_safety_blocklist_matches_approved_matrix():
    root = Path(__file__).resolve().parent.parent
    settings = load_settings(path=root / "config.yaml", env_file=root / ".env.missing")

    assert settings.expectancy_filter_enabled is True
    assert frozenset(settings.blocked_scanner_directions) == EXPECTED_BLOCKED_COMBINATIONS


@pytest.mark.parametrize("scanner_name,direction", sorted(EXPECTED_BLOCKED_COMBINATIONS))
def test_manual_blocklist_overrides_paper_bootstrap(candidate, scanner_name, direction):
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
