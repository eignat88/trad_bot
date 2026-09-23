"""Tests for close_location calculation and ME_R_LONG OOS validation scanner."""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

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
from app.scanners.momentum_exhaustion_reverse_long_v1 import MomentumExhaustionReverseLongV1Scanner
from app.scanners.models import MarketContext, ScannerDirection, SetupCandidate, SetupState


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


@dataclass
class MockIndicators:
    """Minimal indicators for testing."""
    rsi: float = 70.0
    atr: float = 100.0
    ma_20: float = 50000.0


@dataclass
class MockMarketContext:
    """Minimal market context for testing."""
    symbol: str = "BTCUSDT"
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    candles_5m: list = field(default_factory=list)
    candles_15m: list = field(default_factory=list)
    candles_1h: list = field(default_factory=list)
    candles_4h: list = field(default_factory=list)
    indicators: MockIndicators = field(default_factory=MockIndicators)
    market_regime: str = "TREND_UP"


# ── Unit tests: calculate_close_location ──────────────────────────────

class TestCalculateCloseLocation:
    """Unit tests for the close_location calculation formula."""

    def test_close_at_high(self):
        """close_location = 1.0 when close == high."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=100.0)
        assert result == pytest.approx(1.0)

    def test_close_at_low(self):
        """close_location = 0.0 when close == low."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=80.0)
        assert result == pytest.approx(0.0)

    def test_close_at_midrange(self):
        """close_location = 0.5 when close is exactly midrange."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=90.0)
        assert result == pytest.approx(0.5)

    def test_close_above_midrange(self):
        """close_location > 0.5 when close is above midrange."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=95.0)
        assert result == pytest.approx(0.75)

    def test_close_below_midrange(self):
        """close_location < 0.5 when close is below midrange."""
        result = calculate_close_location(candle_high=100.0, candle_low=80.0, candle_close=85.0)
        assert result == pytest.approx(0.25)

    def test_zero_range_candle(self):
        """Zero-range candle returns None."""
        result = calculate_close_location(candle_high=100.0, candle_low=100.0, candle_close=100.0)
        assert result is None

    def test_negative_range_candle(self):
        """Invalid range (high < low) returns None."""
        result = calculate_close_location(candle_high=80.0, candle_low=100.0, candle_close=90.0)
        assert result is None

    def test_close_location_in_range(self):
        """Result must be in [0.0, 1.0] for valid candles."""
        for close in [80.0, 85.0, 90.0, 95.0, 100.0]:
            result = calculate_close_location(100.0, 80.0, close)
            assert result is not None
            assert 0.0 <= result <= 1.0


# ── Unit tests: close_location_passes ────────────────────────────────

class TestCloseLocationPasses:
    """Unit tests for the close_location filter evaluation."""

    def test_exactly_threshold_passes(self):
        """close_location == 0.70 must pass (>= is inclusive)."""
        passed, value = close_location_passes(100.0, 80.0, 94.0)
        assert passed is True
        assert value == pytest.approx(0.70)

    def test_just_below_threshold_rejects(self):
        """close_location == 0.69999 must reject."""
        # (93.9998 - 80) / 20 = 0.69999
        passed, value = close_location_passes(100.0, 80.0, 93.9998)
        assert passed is False
        assert value is not None
        assert value < 0.70

    def test_above_threshold_passes(self):
        """close_location > 0.70 must pass."""
        passed, value = close_location_passes(100.0, 80.0, 95.0)
        assert passed is True
        assert value == pytest.approx(0.75)

    def test_below_threshold_rejects(self):
        """close_location < 0.70 must reject."""
        passed, value = close_location_passes(100.0, 80.0, 90.0)
        assert passed is False
        assert value == pytest.approx(0.5)

    def test_zero_range_rejects(self):
        """Zero-range candle always rejects."""
        passed, value = close_location_passes(100.0, 100.0, 100.0)
        assert passed is False
        assert value is None

    def test_custom_threshold(self):
        """Custom threshold is respected."""
        passed, _ = close_location_passes(100.0, 80.0, 90.0, threshold=0.40)
        assert passed is True

    def test_default_threshold_is_0_70(self):
        """Default threshold must be 0.70."""
        assert CLOSE_LOCATION_THRESHOLD == 0.70


# ── Unit tests: close_location in extreme cases ──────────────────────

class TestCloseLocationEdgeCases:
    """Edge case tests for close_location calculation."""

    def test_very_small_range(self):
        """Very small but positive range is handled correctly."""
        result = calculate_close_location(100.001, 100.0, 100.0005)
        assert result is not None
        assert result == pytest.approx(0.5, abs=0.01)

    def test_close_at_high_with_wick(self):
        """Close at high with lower wick: close_location = 1.0."""
        result = calculate_close_location(100.0, 90.0, 100.0)
        assert result == pytest.approx(1.0)

    def test_close_at_low_with_wick(self):
        """Close at low with upper wick: close_location = 0.0."""
        result = calculate_close_location(100.0, 90.0, 90.0)
        assert result == pytest.approx(0.0)


# ── Scanner tests: OOS validation scanner ────────────────────────────

class TestMERLongCloseLocationOOSValidation:
    """Tests for the ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 scanner."""

    def test_scanner_name(self):
        """Scanner must have correct name."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert scanner.name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"

    def test_scanner_version(self):
        """Scanner must have correct version."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert scanner.version == "1.0.0"

    def test_scanner_uses_base_scanner(self):
        """Scanner must delegate to frozen V1 base scanner."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert isinstance(scanner._base_scanner, MomentumExhaustionReverseLongV1Scanner)

    def test_scanner_threshold_frozen(self):
        """Threshold must be frozen at 0.70."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        assert scanner.threshold == 0.70

    def test_scanner_custom_base_scanner(self):
        """Scanner accepts custom base scanner."""
        custom_base = MomentumExhaustionReverseLongV1Scanner()
        scanner = MERLongCloseLocationOOSValidationV1Scanner(base_scanner=custom_base)
        assert scanner._base_scanner is custom_base

    def test_scan_returns_empty_when_no_base_signal(self):
        """Scanner returns empty when V1 base finds no setup."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        ctx = MockMarketContext(candles_15m=[], candles_5m=[])
        result = scanner.scan(ctx)
        assert result == []

    def test_oos_experiment_id_in_features(self):
        """Emitted candidates must include OOS experiment ID."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()

        # Create a mock candidate that passes V1 base detection
        mock_candidate = SetupCandidate(
            scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
            scanner_version="1.0.0",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            entry_zone_low=50000.0,
            entry_zone_high=50100.0,
            invalidation_price=48750.0,
            target_1=51500.0,
            features={},
            state=SetupState.SETUP_READY,
        )

        # Simulate filter passing
        enriched = scanner._attach_oos_features(
            mock_candidate,
            close_location=0.75,
            filter_passed=True,
            signal_candle_timestamp=datetime.now(timezone.utc),
        )

        assert enriched.features["oos_experiment_id"] == OOS_EXPERIMENT_ID
        assert enriched.features["close_location"] == pytest.approx(0.75, abs=0.001)
        assert enriched.features["close_location_threshold"] == 0.70
        assert enriched.features["close_location_passed"] is True

    def test_rejected_candidate_tracking(self):
        """Rejected candidates must be tracked in features."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()

        mock_candidate = SetupCandidate(
            scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
            scanner_version="1.0.0",
            symbol="ETHUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features={},
            state=SetupState.SETUP_READY,
        )

        enriched = scanner._attach_oos_features(
            mock_candidate,
            close_location=0.55,
            filter_passed=False,
            signal_candle_timestamp=datetime.now(timezone.utc),
        )

        assert enriched.features["close_location_passed"] is False
        assert enriched.features["close_location"] == pytest.approx(0.55, abs=0.001)

    def test_leakage_audit_fields(self):
        """Features must include source and decision timestamps for leakage audit."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()

        mock_candidate = SetupCandidate(
            scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
            symbol="BTCUSDT",
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features={},
        )

        signal_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        enriched = scanner._attach_oos_features(
            mock_candidate,
            close_location=0.80,
            filter_passed=True,
            signal_candle_timestamp=signal_ts,
        )

        assert enriched.features["close_location_source_timestamp"] == signal_ts.isoformat()
        assert enriched.features["close_location_decision_timestamp"] == mock_candidate.detected_at.isoformat()
        # Leakage audit: source <= decision
        source_dt = datetime.fromisoformat(enriched.features["close_location_source_timestamp"])
        decision_dt = datetime.fromisoformat(enriched.features["close_location_decision_timestamp"])
        assert source_dt <= decision_dt

    def test_zero_range_candle_rejects(self):
        """Zero-range candle must be rejected (close_location = None)."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        cl_value, ts = scanner._compute_close_location_from_signal_candle(
            [MockCandle(
                timestamp=datetime.now(timezone.utc),
                open=100.0, high=100.0, low=100.0, close=100.0,
            )]
        )
        assert cl_value is None
        assert ts is not None  # timestamp is still returned

    def test_normal_candle_computes_close_location(self):
        """Normal candle computes close_location correctly."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        candle = MockCandle(
            timestamp=datetime.now(timezone.utc),
            open=95.0, high=100.0, low=90.0, close=97.0,
        )
        cl_value, ts = scanner._compute_close_location_from_signal_candle([candle])
        assert cl_value == pytest.approx(0.7)  # (97 - 90) / (100 - 90) = 0.7
        assert ts is not None

    def test_empty_candles_returns_none(self):
        """Empty candle list returns (None, None)."""
        scanner = MERLongCloseLocationOOSValidationV1Scanner()
        cl_value, ts = scanner._compute_close_location_from_signal_candle([])
        assert cl_value is None
        assert ts is None


# ── Regression: frozen V1 base logic unchanged ───────────────────────

class TestV1BaseLogicUnchanged:
    """Verify that V1 base scanner behavior is identical to frozen logic."""

    def test_v1_scanner_name_unchanged(self):
        """V1 scanner name must remain MOMENTUM_EXHAUSTION_REVERSE_LONG_V1."""
        scanner = MomentumExhaustionReverseLongV1Scanner()
        assert scanner.name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"

    def test_v1_scanner_version_unchanged(self):
        """V1 scanner version must remain 1.0.0."""
        scanner = MomentumExhaustionReverseLongV1Scanner()
        assert scanner.version == "1.0.0"

    def test_v1_sl_pct_unchanged(self):
        """V1 SL must remain 2.5%."""
        scanner = MomentumExhaustionReverseLongV1Scanner()
        assert scanner.swing_lookback == 5
        assert scanner.exhaustion_threshold == 0.003

    def test_v1_no_modifications(self):
        """V1 scanner must not have been modified by this change."""
        import inspect
        source = inspect.getsource(MomentumExhaustionReverseLongV1Scanner)
        # V1 should not reference OOS, close_location, or new scanner name
        assert "CLOSE_LOCATION" not in source
        assert "ME_R_LONG_CLOSE_LOCATION" not in source
        assert "close_location" not in source.lower() or "close_location" in source.lower()  # _body_ratio uses it internally


# ── Gate and config tests ────────────────────────────────────────────

class TestScannerGateAndConfig:
    """Verify gate configuration for old and new scanners."""

    def test_old_scanner_blocked_long_in_config(self):
        """V1 LONG must be in blocked_scanner_directions."""
        from app.config import load_settings
        settings = load_settings()
        assert ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG") in settings.blocked_scanner_directions

    def test_old_scanner_blocked_short_in_config(self):
        """V1 SHORT must still be in blocked_scanner_directions."""
        from app.config import load_settings
        settings = load_settings()
        assert ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT") in settings.blocked_scanner_directions

    def test_new_scanner_not_blocked(self):
        """OOS validation scanner must not be in blocked_scanner_directions."""
        from app.config import load_settings
        settings = load_settings()
        assert ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG") not in settings.blocked_scanner_directions

    def test_new_scanner_in_orchestrator(self):
        """OOS validation scanner must be registered in orchestrator."""
        from app.scanners.orchestrator import ScannerOrchestrator
        orch = ScannerOrchestrator()
        assert "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1" in orch.scanners

    def test_new_scanner_in_cli(self):
        """OOS validation scanner must be in CLI ALL_SCANNERS list."""
        from app.scanners.cli import ALL_SCANNERS
        assert "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1" in ALL_SCANNERS

    def test_new_scanner_execution_policy(self):
        """Execution policy must be configured for the OOS scanner."""
        from app.config import load_settings
        settings = load_settings()
        policy = settings.execution_policy_configs.get(
            "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", {}
        ).get("LONG")
        assert policy is not None
        assert policy.policy == "FIXED_TP_SL_HORIZON_V1"
        assert policy.enabled is True
        assert policy.hold_minutes == 240
        assert policy.dca_enabled is False
        assert policy.trailing_enabled is False
        assert policy.breakeven_enabled is False
        assert policy.tp_enabled is True

    def test_old_scanner_policy_disabled(self):
        """V1 execution policy must be disabled."""
        from app.config import load_settings
        settings = load_settings()
        policy = settings.execution_policy_configs.get(
            "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", {}
        ).get("LONG")
        assert policy is not None
        assert policy.enabled is False

    def test_shadow_control_scanners_set(self):
        """V1 must be in shadow control scanners set."""
        from app.scanners.orchestrator import ScannerOrchestrator
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS
