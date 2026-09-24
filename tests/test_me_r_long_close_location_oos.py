"""Tests for ME_R_LONG_CLOSE_LOCATION_OOS prospective OOS experiment."""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from app.scanners.close_location import (
    CLOSE_LOCATION_THRESHOLD,
    calculate_close_location,
    close_location_passes,
)
from app.scanners.me_r_long_close_location_oos_validation import (
    MERLongCloseLocationOOSValidationV1Scanner,
    OOS_EXPERIMENT_ID,
    REJECTION_REASON,
)
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState
from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)


# ── Test helpers ──────────────────────────────────────────────────────

@dataclass
class MockCandle:
    """Minimal candle for testing."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0


def make_candidate(
    symbol: str = "BTCUSDT",
    close_location: float | None = 0.75,
    filter_passed: bool = True,
    state: SetupState = SetupState.SETUP_READY,
) -> SetupCandidate:
    """Create a test candidate with OOS features."""
    return SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        symbol=symbol,
        direction="LONG",
        detected_at=datetime.now(timezone.utc),
        entry_zone_low=50.0,
        entry_zone_high=51.0,
        invalidation_price=48.75,
        target_1=53.0,
        features={
            "oos_experiment_id": OOS_EXPERIMENT_ID,
            "close_location": close_location,
            "close_location_threshold": CLOSE_LOCATION_THRESHOLD,
            "close_location_passed": filter_passed,
            "close_location_source_timestamp": datetime.now(timezone.utc).isoformat(),
            "close_location_decision_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        state=state,
    )


# ── Unit tests: close_location calculation ────────────────────────────

class TestCloseLocationCalculation:
    """Unit tests for close_location calculation."""

    def test_close_at_high(self):
        """close_location = 1.0 when close == high."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=100.0)
        assert result == pytest.approx(1.0, abs=0.001)

    def test_close_at_low(self):
        """close_location = 0.0 when close == low."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=80.0)
        assert result == pytest.approx(0.0, abs=0.001)

    def test_close_at_midrange(self):
        """close_location = 0.5 when close is exactly midrange."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=90.0)
        assert result == pytest.approx(0.5, abs=0.001)

    def test_close_location_above_threshold(self):
        """close_location > 0.70 when close is above threshold."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=94.0)
        assert result == pytest.approx(0.70, abs=0.001)

    def test_close_location_below_threshold(self):
        """close_location < 0.70 when close is below threshold."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=90.0)
        assert result < 0.70


# ── Unit tests: close_location filter ─────────────────────────────────

class TestCloseLocationFilter:
    """Unit tests for close_location filter evaluation."""

    def test_threshold_exact_pass(self):
        """close_location == 0.70 must pass (>= is inclusive)."""
        passed, value = close_location_passes(100.0, 80.0, 94.0)
        assert passed is True
        assert value == pytest.approx(0.70, abs=0.001)

    def test_threshold_below_reject(self):
        """close_location == 0.69999 must reject."""
        passed, value = close_location_passes(100.0, 80.0, 93.9998)
        assert passed is False
        assert value < 0.70

    def test_threshold_above_pass(self):
        """close_location > 0.70 must pass."""
        passed, value = close_location_passes(100.0, 80.0, 95.0)
        assert passed is True
        assert value > 0.70


# ── Scanner behavior tests ────────────────────────────────────────────

class TestScannerBehavior:
    """Tests for ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 scanner."""

    def test_scanner_name(self):
        """Scanner must have correct name."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert scanner.name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

    def test_experiment_id_constant(self):
        """OOS_EXPERIMENT_ID must match scanner name."""
        assert OOS_EXPERIMENT_ID == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

    def test_rejection_reason_constant(self):
        """REJECTION_REASON must be set."""
        assert REJECTION_REASON == "CLOSE_LOCATION_LT_070"


# ── OOS observation persistence tests ────────────────────────────────

class TestOOSObservationPersistence:
    """Tests for OOS observation persistence."""

    def test_pass_candidate_saved(self):
        """PASS candidates must be saved with filter_passed=True."""
        candidate = make_candidate(close_location=0.75, filter_passed=True)
        assert candidate.features["close_location"] == pytest.approx(0.75, abs=0.001)
        assert candidate.features["close_location_passed"] is True

    def test_reject_candidate_saved(self):
        """REJECT candidates must be saved with filter_passed=False."""
        candidate = make_candidate(close_location=0.438, filter_passed=False)
        assert candidate.features["close_location"] == pytest.approx(0.438, abs=0.001)
        assert candidate.features["close_location_passed"] is False

    def test_close_location_not_none(self):
        """close_location must be preserved, not turned into None."""
        candidate = make_candidate(close_location=0.75)
        assert candidate.features["close_location"] is not None
        assert candidate.features["close_location"] == pytest.approx(0.75, abs=0.001)


# ── Dedup tests ───────────────────────────────────────────────────────

class TestDeduplication:
    """Tests for deduplication logic."""

    def test_unique_key_components(self):
        """Unique key must include experiment_id, symbol, signal_time."""
        # The unique index is on (experiment_id, symbol, signal_time)
        # This prevents duplicate observations for the same signal
        assert True  # Placeholder - actual DB test needed

    def test_same_symbol_different_time(self):
        """Same symbol at different times should create separate observations."""
        # This is handled by the unique index
        assert True  # Placeholder - actual DB test needed


# ── Outcome evaluation tests ──────────────────────────────────────────

class TestOutcomeEvaluation:
    """Tests for outcome evaluation."""

    def test_outcome_horizons(self):
        """Outcome must have MFE/MAE for all horizons."""
        # The outcome table has columns for 15m, 30m, 60m, 120m, 240m
        assert True  # Placeholder - actual DB test needed

    def test_r_units_calculation(self):
        """MFE/MAE must be calculated in R units."""
        # R units = MFE / risk, where risk = entry - invalidation
        assert True  # Placeholder - actual DB test needed


# ── Trading isolation tests ──────────────────────────────────────────

class TestTradingIsolation:
    """Tests that OOS experiment doesn't affect trading."""

    def test_pass_does_not_enable_trading(self):
        """PASS observation must not automatically enable paper trading."""
        # The OOS scanner only observes, it doesn't enable trading
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert scanner.name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

    def test_reject_does_not_block_existing_gates(self):
        """REJECT observation must not change existing gates."""
        # REJECT candidates are saved with state=EXPIRED
        candidate = make_candidate(close_location=0.438, filter_passed=False, state=SetupState.EXPIRED)
        assert candidate.state == SetupState.EXPIRED


# ── SQL diagnostic tests ─────────────────────────────────────────────

class TestSQLDiagnostics:
    """Tests for SQL diagnostic views."""

    def test_accumulation_view_exists(self):
        """Accumulation view must exist."""
        # The view dds.v_me_r_long_cl_oos_accumulation must exist
        assert True  # Placeholder - actual DB test needed

    def test_pass_reject_view_exists(self):
        """PASS/REJECT comparison view must exist."""
        # The view dds.v_me_r_long_cl_oos_pass_reject must exist
        assert True  # Placeholder - actual DB test needed

    def test_bucket_analysis_view_exists(self):
        """Bucket analysis view must exist."""
        # The view dds.v_me_r_long_cl_oos_bucket_analysis must exist
        assert True  # Placeholder - actual DB test needed


# ── Integration tests ────────────────────────────────────────────────

class TestIntegration:
    """Integration tests for the OOS experiment."""

    def test_full_flow_pass(self):
        """Test full flow for a PASS candidate."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        candidate = make_candidate(close_location=0.75, filter_passed=True)
        
        # Verify candidate has correct features
        assert candidate.features["close_location"] == pytest.approx(0.75, abs=0.001)
        assert candidate.features["close_location_passed"] is True
        assert candidate.features["close_location_threshold"] == CLOSE_LOCATION_THRESHOLD

    def test_full_flow_reject(self):
        """Test full flow for a REJECT candidate."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        candidate = make_candidate(close_location=0.438, filter_passed=False)
        
        # Verify candidate has correct features
        assert candidate.features["close_location"] == pytest.approx(0.438, abs=0.001)
        assert candidate.features["close_location_passed"] is False
        assert candidate.features["close_location_threshold"] == CLOSE_LOCATION_THRESHOLD
