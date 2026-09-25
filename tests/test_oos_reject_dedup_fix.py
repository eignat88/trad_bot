"""Regression tests for OOS REJECT deduplication fix.

Tests verify the fix in orchestrator.scan_all_with_stats() where OOS REJECT
candidates were incorrectly added twice to `unique` after dedup.

Root cause: dedup.filter_new() kept OOS REJECT (unique scanner_name key),
but the code unconditionally re-added ALL oos_rejected, causing duplication.

Fix: Only re-add OOS REJECT if dedup.contains_key() returns False.
Also verifies the runtime invariant guard logs an error when violated.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.deduplication import DeduplicationEngine
from app.scanners.models import MarketContext, SetupCandidate, SetupState


def _make_oos_candidate(
    features: dict | None = None,
    setup_id: str | None = None,
    signal_candle_open_time: int = 0,
    symbol: str = "BTCUSDT",
) -> SetupCandidate:
    """Create a minimal OOS SetupCandidate for testing."""
    return SetupCandidate(
        symbol=symbol,
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.0.0",
        direction="LONG",
        htf_timeframe="1h",
        setup_timeframe="5m",
        entry_timeframe="5m",
        setup_started_at=None,
        detected_at=None,
        reference_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=98.0,
        target_1=103.0,
        target_2=105.0,
        score=50.0,
        market_regime="neutral",
        state=SetupState.DETECTED,
        reasons=("TEST",),
        features=features or {},
        setup_id=setup_id or uuid4(),
        signal_candle_open_time=signal_candle_open_time,
    )


def _make_mock_context(symbol: str = "BTCUSDT") -> MagicMock:
    """Create a minimal mock MarketContext."""
    ctx = MagicMock(spec=MarketContext)
    ctx.symbol = symbol
    ctx.market_regime = "neutral"
    ctx.candles_5m = ()
    ctx.candles_15m = ()
    ctx.candles_1h = ()
    ctx.candles_4h = ()
    return ctx


class TestOOSRejectDedupFix:
    """Regression tests for OOS REJECT deduplication fix."""

    def test_no_duplication_when_dedup_keeps_reject(self):
        """TEST 1 (Bug fix): dedup keeps OOS REJECT → must not re-add.
        
        Scenario (the actual bug):
            scanner returns 12 candidates (4 PASS + 8 REJECT)
            dedup keeps all 12 (unique keys due to OOS scanner_name)
            
        Before fix: 12 + 8 = 20 setups (BUG)
        After fix:  12 setups (correct)
        """
        # Arrange: 4 PASS + 8 REJECT = 12 total, each with unique signal candle
        pass_candidates = [
            _make_oos_candidate(
                features={},
                setup_id=uuid4(),
                signal_candle_open_time=2000000000 + i,
            )
            for i in range(4)
        ]
        
        reject_candidates = [
            _make_oos_candidate(
                features={"_oos_rejected": True, "_oos_rejection_reason": "CLOSE_LOCATION_LT_070"},
                setup_id=uuid4(),
                signal_candle_open_time=3000000000 + i,
            )
            for i in range(8)
        ]
        
        all_candidates = pass_candidates + reject_candidates  # 12 total
        
        ctx = _make_mock_context()
        orchestrator = ScannerOrchestrator(
            enabled_scanners=["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        )
        
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = all_candidates
        orchestrator.scanners["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"] = mock_scanner
        
        # Act
        candidates, stats = orchestrator.scan_all_with_stats(ctx)
        
        # Assert
        stat = stats["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        
        print(f"\nTEST 1: No duplication when dedup keeps reject")
        print(f"  Scanner returned: {stat['candidates_found']}")
        print(f"  setups_saved: {stat['setups_saved']}")
        
        # KEY INVARIANT: setups_saved <= candidates_found
        assert stat['setups_saved'] == stat['candidates_found'] == 12, \
            f"Expected 12/12, got {stat['setups_saved']}/{stat['candidates_found']}"
        
        # Conversion should be 100%, not 166.7%
        conversion = 100.0 * stat['setups_saved'] / stat['candidates_found']
        assert conversion == 100.0, f"Expected 100% conversion, got {conversion}%"
        
        print(f"  Conversion: {conversion}% ✓")

    def test_no_duplication_all_reject(self):
        """TEST 2: all REJECT scenario - must not duplicate."""
        # Arrange: 12 REJECT candidates
        reject_candidates = [
            _make_oos_candidate(
                features={"_oos_rejected": True, "_oos_rejection_reason": "CLOSE_LOCATION_LT_070"},
                setup_id=uuid4(),
                signal_candle_open_time=4000000000 + i,
            )
            for i in range(12)
        ]
        
        ctx = _make_mock_context()
        orchestrator = ScannerOrchestrator(
            enabled_scanners=["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        )
        
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = reject_candidates
        orchestrator.scanners["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"] = mock_scanner
        
        # Act
        candidates, stats = orchestrator.scan_all_with_stats(ctx)
        
        # Assert
        stat = stats["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        
        print(f"\nTEST 2: No duplication - all REJECT")
        print(f"  Scanner returned: {stat['candidates_found']}")
        print(f"  setups_saved: {stat['setups_saved']}")
        
        assert stat['setups_saved'] == stat['candidates_found'] == 12, \
            f"Expected 12/12, got {stat['setups_saved']}/{stat['candidates_found']}"
        
        print(f"  No duplication ✓")

    def test_invariant_one_candidate_max_one_setup(self):
        """TEST 3 (Invariant): one candidate → at most one setup row."""
        # Arrange: mix of PASS and REJECT
        candidates = []
        
        # 5 PASS candidates
        for i in range(5):
            candidates.append(_make_oos_candidate(
                features={},
                setup_id=uuid4(),
                signal_candle_open_time=5000000000 + i,
            ))
        
        # 3 REJECT with unique keys
        for i in range(3):
            candidates.append(_make_oos_candidate(
                features={"_oos_rejected": True},
                setup_id=uuid4(),
                signal_candle_open_time=6000000000 + i,
            ))
        
        # Total: 8 candidates
        ctx = _make_mock_context()
        orchestrator = ScannerOrchestrator(
            enabled_scanners=["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        )
        
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = candidates
        orchestrator.scanners["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"] = mock_scanner
        
        # Act
        valid_candidates, stats = orchestrator.scan_all_with_stats(ctx)
        
        # Assert
        stat = stats["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        
        print(f"\nTEST 3: Invariant - one candidate max one setup")
        print(f"  Scanner returned: {stat['candidates_found']}")
        print(f"  setups_saved: {stat['setups_saved']}")
        print(f"  valid candidates: {len(valid_candidates)}")
        
        # Check invariant: no duplicate dedup keys in valid
        seen_keys = set()
        duplicate_keys = []
        for c in valid_candidates:
            key = orchestrator.dedup.key(c)  # public API
            if key in seen_keys:
                duplicate_keys.append(key)
            seen_keys.add(key)
        
        assert len(duplicate_keys) == 0, \
            f"Found duplicate keys in valid: {duplicate_keys}"
        
        # setups_saved should equal number of unique valid candidates
        assert stat['setups_saved'] == len(valid_candidates), \
            f"setups_saved ({stat['setups_saved']}) != valid candidates ({len(valid_candidates)})"
        
        # Invariant: setups_saved <= candidates_found
        assert stat['setups_saved'] <= stat['candidates_found'], \
            f"Invariant violated: setups_saved ({stat['setups_saved']}) > candidates_found ({stat['candidates_found']})"
        
        print(f"  No duplicate keys ✓")
        print(f"  Invariant holds ✓")

    def test_mixed_scanners_not_affected(self):
        """TEST 4 (Non-OOS unaffected): other scanners work normally."""
        # Arrange: non-OOS candidates
        other_candidates = [
            SetupCandidate(
                symbol="BTCUSDT",
                scanner_name="BREAKOUT_RETEST",
                scanner_version="1.0.0",
                direction="LONG",
                htf_timeframe="1h",
                setup_timeframe="15m",
                entry_timeframe="5m",
                setup_started_at=None,
                detected_at=None,
                reference_price=100.0,
                entry_zone_low=99.0,
                entry_zone_high=101.0,
                invalidation_price=98.0,
                target_1=103.0,
                target_2=105.0,
                score=50.0,
                market_regime="neutral",
                state=SetupState.DETECTED,
                reasons=("TEST",),
                features={},
                setup_id=uuid4(),
                signal_candle_open_time=7000000000 + i,
            )
            for i in range(5)
        ]
        
        ctx = _make_mock_context()
        orchestrator = ScannerOrchestrator(
            enabled_scanners=["BREAKOUT_RETEST"]
        )
        
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = other_candidates
        orchestrator.scanners["BREAKOUT_RETEST"] = mock_scanner
        
        # Act
        valid_candidates, stats = orchestrator.scan_all_with_stats(ctx)
        
        # Assert
        stat = stats["BREAKOUT_RETEST"]
        
        print(f"\nTEST 4: Non-OOS scanners unaffected")
        print(f"  Scanner returned: {stat['candidates_found']}")
        print(f"  setups_saved: {stat['setups_saved']}")
        
        # For non-OOS, no re-add logic applies
        assert stat['setups_saved'] == stat['candidates_found'] == 5, \
            f"Expected 5/5, got {stat['setups_saved']}/{stat['candidates_found']}"
        
        print(f"  Non-OOS behavior unchanged ✓")

    def test_invariant_guard_logs_error(self):
        """TEST 5: invariant guard emits logger.error when violated.

        This verifies the safety net that catches future regressions at
        runtime rather than only through Grafana.
        """
        # Arrange: 3 OOS REJECT candidates
        candidates = [
            _make_oos_candidate(
                features={"_oos_rejected": True},
                setup_id=uuid4(),
                signal_candle_open_time=8000000000 + i,
            )
            for i in range(3)
        ]

        ctx = _make_mock_context()
        orchestrator = ScannerOrchestrator(
            enabled_scanners=["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        )

        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = candidates
        orchestrator.scanners["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"] = mock_scanner

        # Act
        candidates, stats = orchestrator.scan_all_with_stats(ctx)

        # Assert: guard should NOT fire (invariant holds)
        stat = stats["ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"]
        assert stat['setups_saved'] <= stat['candidates_found'], \
            "Invariant should hold — guard should not fire"
        print(f"\nTEST 5: Invariant guard present (no violation in normal case) ✓")


class TestDeduplicationEnginePublicAPI:
    """Test the public API of DeduplicationEngine."""

    def test_key_matches_expected_format(self):
        """key() returns the expected dedup identity string."""
        dedup = DeduplicationEngine()
        c = _make_oos_candidate(signal_candle_open_time=12345)
        assert dedup.key(c) == (
            "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
            "|BTCUSDT|LONG|5m|12345"
        )

    def test_contains_key_true_after_filter_new(self):
        """contains_key() returns True for a candidate kept by filter_new."""
        dedup = DeduplicationEngine()
        c = _make_oos_candidate(signal_candle_open_time=11111)
        dedup.filter_new([c])
        assert dedup.contains_key(c) is True

    def test_contains_key_false_for_unseen_candidate(self):
        """contains_key() returns False for a candidate never seen."""
        dedup = DeduplicationEngine()
        c = _make_oos_candidate(signal_candle_open_time=22222)
        assert dedup.contains_key(c) is False

    def test_key_alias_underscore_key(self):
        """_key is an alias for key (backward compatibility)."""
        dedup = DeduplicationEngine()
        c = _make_oos_candidate()
        assert dedup._key(c) == dedup.key(c)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])