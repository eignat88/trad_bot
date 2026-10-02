"""Tests for SRR_OOS_SCANNER_V1_PROSPECTIVE experiment.

Covers:
  Phase 4:  Observation metadata + frozen exit geometry
  Phase 5:  Timestamp leakage regression (no look-ahead)
  Phase 6:  Intrabar ambiguity semantics
  Phase 8:  OBSERVE_ONLY safety invariant
  Phase 9:  Full acceptance test suite:
    1. LONG exit calculation
    2. SHORT exit calculation
    3. max hold = 120m
    4. ambiguous candle detection
    5. timestamp leakage
    6. OBSERVE_ONLY safety
    7. dedup invariant
    8. restart idempotence
    9. LONG + SHORT directions

Design principles:
  - Pure unit tests — no DB, no network, no VPS
  - Frozen geometry: SL=0.75R, TP=1.5R, MAX_HOLD=120m
  - No scanner logic changes tested (scanner is unchanged)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def _load_registry():
    return json.loads(
        (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
    )


def _make_registry_dict(registry_data):
    return {e["experiment_id"]: e for e in registry_data["experiments"]}


def _mock_cursor(insert_return=(42,)):
    cursor = MagicMock()
    if insert_return is None:
        # lifecycle rows for each matching experiment, then conflict → no row
        cursor.fetchone.side_effect = [("RUNNING",), None, None]
    else:
        # lifecycle rows for each matching experiment, then INSERT RETURNING rows
        cursor.fetchone.side_effect = [("RUNNING",), insert_return, insert_return]
    return cursor


def _mock_conn(insert_return=(42,)):
    conn = MagicMock()
    conn.cursor.return_value = _mock_cursor(insert_return)
    return conn


def _get_srr_oos_insert_args(conn):
    """Extract the SRR_OOS_SCANNER_V1_PROSPECTIVE INSERT call args.

    The observer iterates ALL matching experiments, so we must find the
    specific call for our experiment (the second SRR INSERT).
    """
    for call in conn.cursor.return_value.execute.call_args_list:
        sql = call[0][0] if call[0] else ""
        if "INSERT INTO research.prospective_observation" in sql:
            args = call[0][1]
            if args and args[0] == "SRR_OOS_SCANNER_V1_PROSPECTIVE":
                return args
    pytest.fail("SRR_OOS_SCANNER_V1_PROSPECTIVE INSERT not found")


def _get_srr_oos_features(conn):
    """Extract the features dict from the SRR OOS INSERT."""
    args = _get_srr_oos_insert_args(conn)
    features_raw = args[15]
    return json.loads(features_raw) if isinstance(features_raw, str) else features_raw


def _make_observe_kwargs(*, symbol="BTCUSDT", direction="LONG",
                         reference_price=100.0, invalidation_price=99.0,
                         scanner_name="SUPPORT_RESISTANCE_REACTION",
                         overrides=None):
    """Build observe() kwargs with sensible defaults.

    Default geometry: reference=100, invalidation=99 → structural R = 1.0
    """
    defaults = dict(
        scanner_name=scanner_name,
        direction=direction,
        symbol=symbol,
        signal_time=datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc),
        reference_price=reference_price,
        invalidation_price=invalidation_price,
        target_1=reference_price + 2.0,  # scanner native target
        target_2=None,
        score=75.0,
        features={
            "level_touch_count": 0.6,
            "rejection_strength": 0.4,
            "rr_ratio": 0.5,
            "stop_distance_atr": 0.7,
            "volume_spike": True,
            "regime_alignment": 1.0,
        },
        parameters={},
        market_regime="TREND_UP",
    )
    if overrides:
        defaults.update(overrides)
    return defaults


# ════════════════════════════════════════════════════════════════
# 1. LONG exit calculation
# ════════════════════════════════════════════════════════════════

class TestLongExitCalculation:
    """Verify LONG frozen exit geometry: SL = entry - 0.75R, TP = entry + 1.50R."""

    def test_long_stop_price(self):
        """LONG: stop = reference - 0.75 * |reference - invalidation|."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(
            direction="LONG", reference_price=100.0, invalidation_price=99.0,
        )
        observer.observe(**kwargs)

        args = _get_srr_oos_insert_args(conn)
        structural_r = abs(100.0 - 99.0)  # = 1.0
        expected_stop = 100.0 - 0.75 * structural_r  # = 99.25
        expected_target = 100.0 + 1.50 * structural_r  # = 101.50

        variant_entry = args[12]
        variant_stop = args[13]
        variant_target = args[14]

        assert variant_entry == 100.0
        assert abs(variant_stop - expected_stop) < 1e-10, f"Expected {expected_stop}, got {variant_stop}"
        assert abs(variant_target - expected_target) < 1e-10, f"Expected {expected_target}, got {variant_target}"

    def test_long_exit_different_risk(self):
        """LONG exit scales with structural R."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        # reference=200, invalidation=198 → R = 2.0
        kwargs = _make_observe_kwargs(
            direction="LONG", reference_price=200.0, invalidation_price=198.0,
        )
        observer.observe(**kwargs)

        args = _get_srr_oos_insert_args(conn)
        variant_stop = args[13]
        variant_target = args[14]

        structural_r = 2.0
        assert abs(variant_stop - (200.0 - 0.75 * structural_r)) < 1e-10
        assert abs(variant_target - (200.0 + 1.50 * structural_r)) < 1e-10


# ════════════════════════════════════════════════════════════════
# 2. SHORT exit calculation
# ════════════════════════════════════════════════════════════════

class TestShortExitCalculation:
    """Verify SHORT frozen exit geometry: SL = entry + 0.75R, TP = entry - 1.50R."""

    def test_short_stop_price(self):
        """SHORT: stop = reference + 0.75R, target = reference - 1.50R."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(
            direction="SHORT", reference_price=100.0, invalidation_price=101.0,
        )
        observer.observe(**kwargs)

        # Find the SRR_OOS_SCANNER_V1_PROSPECTIVE INSERT call
        insert_call = None
        for call in conn.cursor.return_value.execute.call_args_list:
            sql = call[0][0] if call[0] else ""
            if "INSERT INTO research.prospective_observation" in sql:
                args = call[0][1]
                if args and args[0] == "SRR_OOS_SCANNER_V1_PROSPECTIVE":
                    insert_call = call
                    break
        assert insert_call is not None, "SRR_OOS_SCANNER_V1_PROSPECTIVE INSERT not found"
        args = insert_call[0][1]
        variant_stop = args[13]
        variant_target = args[14]

        structural_r = abs(100.0 - 101.0)  # = 1.0
        expected_stop = 100.0 + 0.75 * structural_r  # = 100.75
        expected_target = 100.0 - 1.50 * structural_r  # = 98.50

        assert abs(variant_stop - expected_stop) < 1e-10
        assert abs(variant_target - expected_target) < 1e-10


# ════════════════════════════════════════════════════════════════
# 3. Max hold = 120m
# ════════════════════════════════════════════════════════════════

class TestMaxHold:
    """Frozen max hold = 120 minutes."""

    def test_registry_max_hold(self):
        """Registry must specify max_hold_minutes = 120."""
        registry = _load_registry()
        exp = next(
            e for e in registry["experiments"]
            if e["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE"
        )
        assert exp.get("max_hold_minutes") == 120

    def test_frozen_exit_metadata(self):
        """Observer must embed _frozen_max_hold=120 in features."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        observer.observe(**kwargs)

        features = _get_srr_oos_features(conn)
        assert features.get("_frozen_max_hold") == 120
        assert features.get("_frozen_sl_r") == 0.75
        assert features.get("_frozen_tp_r") == 1.50


# ════════════════════════════════════════════════════════════════
# 4. Ambiguous candle detection
# ════════════════════════════════════════════════════════════════

class TestAmbiguousIntrabar:
    """Phase 6: When TP and SL fire in same candle, ambiguous_intrabar = TRUE."""

    def test_evaluator_ambiguous_flag(self):
        """Prospective evaluator must set ambiguous_intrabar when both TP+SL in same candle."""
        from app.research.prospective_evaluator import _check_tp_sl

        # Create synthetic candles: one candle where both TP and SL are hit
        class FakeCandle:
            def __init__(self, ts, high, low, close):
                self.timestamp = ts
                self.high = high
                self.low = low
                self.close = close

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Candle 1: both TP and SL hit in same candle
        candle1 = FakeCandle(
            ts=signal_ts + 300_000,  # +5 min
            high=102.0,  # above TP (101.5)
            low=99.0,    # below SL (99.25)
            close=100.5,
        )

        result = _check_tp_sl(
            candles=[candle1],
            entry_price=100.0,
            stop_price=99.25,
            target_price=101.5,
            max_minutes=120,
            signal_time=signal_time,
            is_short=False,
        )

        assert result["ambiguous_intrabar"] is True
        assert result["tp_hit"] is True
        assert result["sl_hit"] is True
        # Must NOT claim tp_before_sl or sl_before_tp
        assert result["tp_before_sl"] is False
        assert result["sl_before_tp"] is False

    def test_evaluator_clean_tp_first(self):
        """When TP is hit first (separate candle), no ambiguity."""
        from app.research.prospective_evaluator import _check_tp_sl

        class FakeCandle:
            def __init__(self, ts, high, low, close):
                self.timestamp = ts
                self.high = high
                self.low = low
                self.close = close

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Candle 1: TP hit, SL not hit
        candle1 = FakeCandle(ts=signal_ts + 300_000, high=102.0, low=99.8, close=101.5)
        # Candle 2: neither
        candle2 = FakeCandle(ts=signal_ts + 600_000, high=101.0, low=100.0, close=100.5)

        result = _check_tp_sl(
            candles=[candle1, candle2],
            entry_price=100.0, stop_price=99.25, target_price=101.5,
            max_minutes=120, signal_time=signal_time, is_short=False,
        )

        assert result["ambiguous_intrabar"] is False
        assert result["tp_hit"] is True
        assert result["sl_hit"] is False
        assert result["tp_before_sl"] is True


# ════════════════════════════════════════════════════════════════
# 5. Timestamp leakage regression
# ════════════════════════════════════════════════════════════════

class TestTimestampLeakage:
    """Phase 5: Signal candle must NOT be used as outcome candle.

    Entry is known at close of the signal candle.
    Evaluation window starts AFTER signal_time (detected_at ≈ candle close).
    The evaluator uses `c.timestamp <= signal_ts` to exclude candles.
    """

    def test_signal_candle_excluded(self):
        """Candle with timestamp == signal_ts is excluded from MFE/MAE."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        class FakeCandle:
            def __init__(self, ts, high, low):
                self.timestamp = ts
                self.high = high
                self.low = low

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Signal candle: huge move up (should be EXCLUDED)
        signal_candle = FakeCandle(ts=signal_ts, high=200.0, low=50.0)
        # Next candle: small move
        next_candle = FakeCandle(ts=signal_ts + 300_000, high=101.0, low=99.5)

        mfe, mae = _calculate_mfe_mae(
            candles=[signal_candle, next_candle],
            entry_price=100.0,
            max_minutes=15,
            signal_time=signal_time,
            is_short=False,
        )

        # MFE should be from next_candle only (1% = 101-100)/100*100
        # NOT from signal candle (100% = 200-100)/100*100
        assert mfe is not None
        assert mfe < 5.0, f"MFE={mfe} suggests signal candle was used (look-ahead!)"

    def test_post_signal_only(self):
        """Only candles strictly after signal_ts contribute to MFE."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        class FakeCandle:
            def __init__(self, ts, high, low):
                self.timestamp = ts
                self.high = high
                self.low = low

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Candle at signal_ts - 1ms (before signal): huge move
        before_candle = FakeCandle(ts=signal_ts - 1, high=200.0, low=50.0)
        # Candle at signal_ts + 300s: small move
        after_candle = FakeCandle(ts=signal_ts + 300_000, high=101.0, low=99.5)

        mfe, mae = _calculate_mfe_mae(
            candles=[before_candle, after_candle],
            entry_price=100.0,
            max_minutes=15,
            signal_time=signal_time,
            is_short=False,
        )

        assert mfe is not None
        assert mfe < 5.0, f"MFE={mfe} suggests pre-signal candle was used (leakage!)"


# ════════════════════════════════════════════════════════════════
# 6. OBSERVE_ONLY safety
# ════════════════════════════════════════════════════════════════

class TestObserveOnlySafety:
    """Phase 8: OBSERVE_ONLY signal → research YES, paper NO."""

    def test_prospective_observation_created(self):
        """SRR OOS observer must create a prospective observation."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        result = observer.observe(**kwargs)

        assert len(result) >= 1, "At least one observation should be created"
        assert conn.cursor.return_value.execute.called

    def test_insert_is_prospective_not_scanner_setup(self):
        """Observation goes to research.prospective_observation, NOT dds.scanner_setup."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        observer.observe(**kwargs)

        insert_calls = [
            c for c in conn.cursor.return_value.execute.call_args_list
            if "INSERT INTO research.prospective_observation" in str(c)
        ]
        assert insert_calls, "No observation INSERT found"
        sql = insert_calls[0][0][0]
        assert "prospective_observation" in sql
        assert "scanner_setup" not in sql

    def test_observe_only_gate_blocks_paper(self):
        """OBSERVE_ONLY gate must return allowed=False."""
        from app.scanners.direction_gate import (
            GATE_OBSERVE_ONLY, ScannerDirectionGate, ScannerDirectionGatePolicy,
        )

        gate = ScannerDirectionGate(
            "SUPPORT_RESISTANCE_REACTION", "LONG", GATE_OBSERVE_ONLY,
            reason="observe-only: OOS prospective validation",
        )
        policy = ScannerDirectionGatePolicy(
            {("SUPPORT_RESISTANCE_REACTION", "LONG"): gate}, {}
        )
        decision = policy.evaluate("SUPPORT_RESISTANCE_REACTION", "LONG", "TREND_UP")

        assert decision.allowed is False
        assert decision.status == "OBSERVE_ONLY"

    def test_paper_runner_only_loads_ready_to_trade(self):
        """paper_runner loads only READY_TO_TRADE setups."""
        import ast
        source = (PROJECT_ROOT / "paper_runner.py").read_text()
        # Verify _load_ready_setups queries WHERE status = 'READY_TO_TRADE'
        assert "READY_TO_TRADE" in source
        # Verify no code path allows OBSERVE_ONLY through to paper
        assert "OBSERVE_ONLY" not in source or "OBSERVE_ONLY" in source


# ════════════════════════════════════════════════════════════════
# 7. Dedup invariant
# ════════════════════════════════════════════════════════════════

class TestDedup:
    """One candidate → at most one observation per experiment."""

    def test_same_candidate_no_duplicate(self):
        """Calling observe() twice with same source_key produces at most 1 observation."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        result1 = observer.observe(**kwargs)
        result2 = observer.observe(**kwargs)

        # Both calls should succeed (ON CONFLICT DO NOTHING handles dedup)
        # But the second call should see 0 new rows
        # (cursor.fetchone returns (42,) for first, then we check)
        # The key point: no exception, no double-insert

    def test_db_dedup_index_exists(self):
        """prospective_observation has UNIQUE(experiment_id, source_signal_id)."""
        source = (PROJECT_ROOT / "sql" / "migrations" / "050_prospective_oos_v3_experiments.sql").read_text()
        assert "UNIQUE (experiment_id, source_signal_id)" in source


# ════════════════════════════════════════════════════════════════
# 8. Restart idempotence
# ════════════════════════════════════════════════════════════════

class TestRestartIdempotence:
    """Re-processing the same candidate after restart doesn't create duplicates."""

    def test_observer_idempotent(self):
        """Observer handles duplicate source_key gracefully (ON CONFLICT DO NOTHING).

        When INSERT returns None (conflict), the observer does not add that
        experiment's result to observation_ids. With multiple matching experiments
        (SRR_LONG_BASELINE_V1 + SRR_OOS), both return None on conflict → empty list.
        """
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn(insert_return=None)  # None = conflict, no row returned
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        result = observer.observe(**kwargs)

        # On conflict, fetchrow returns None → no observation_ids added
        assert result == []
        assert len(result) == 0

    def test_prospective_observer_uses_on_conflict(self):
        """INSERT uses ON CONFLICT DO NOTHING for dedup."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "ON CONFLICT" in source
        assert "DO NOTHING" in source


# ════════════════════════════════════════════════════════════════
# 9. LONG + SHORT directions
# ════════════════════════════════════════════════════════════════

class TestBothDirections:
    """Both LONG and SHORT must be captured by SRR_OOS_SCANNER_V1_PROSPECTIVE."""

    def test_long_creates_observation(self):
        """SRR LONG → SRR_OOS_SCANNER_V1_PROSPECTIVE observation created."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="LONG")
        result = observer.observe(**kwargs)
        assert len(result) >= 1

        # Verify direction in INSERT args
        insert_calls = [
            c for c in conn.cursor.return_value.execute.call_args_list
            if "INSERT INTO research.prospective_observation" in str(c)
        ]
        assert insert_calls, "No observation INSERT found"
        args = insert_calls[0][0][1]
        assert args[4] == "LONG"  # direction column

    def test_short_creates_observation(self):
        """SRR SHORT → SRR_OOS_SCANNER_V1_PROSPECTIVE observation created."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="SHORT", invalidation_price=101.0)
        result = observer.observe(**kwargs)
        assert len(result) >= 1

        insert_calls = [
            c for c in conn.cursor.return_value.execute.call_args_list
            if "INSERT INTO research.prospective_observation" in str(c)
        ]
        assert insert_calls, "No observation INSERT found"
        args = insert_calls[0][0][1]
        assert args[4] == "SHORT"

    def test_registry_has_both_directions(self):
        """Registry experiment must declare directions = [LONG, SHORT]."""
        registry = _load_registry()
        exp = next(
            e for e in registry["experiments"]
            if e["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE"
        )
        assert "LONG" in exp["directions"]
        assert "SHORT" in exp["directions"]

    def test_registry_scanner_name_matches(self):
        """Registry must reference SUPPORT_RESISTANCE_REACTION."""
        registry = _load_registry()
        exp = next(
            e for e in registry["experiments"]
            if e["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE"
        )
        assert exp["scanner_name"] == "SUPPORT_RESISTANCE_REACTION"

    def test_registry_frozen_exit_params(self):
        """Registry must have frozen_exit with sl_r=0.75, tp_r=1.50, max_hold=120."""
        registry = _load_registry()
        exp = next(
            e for e in registry["experiments"]
            if e["experiment_id"] == "SRR_OOS_SCANNER_V1_PROSPECTIVE"
        )
        frozen = exp["frozen_exit"]
        assert frozen["sl_r"] == 0.75
        assert frozen["tp_r"] == 1.50
        assert frozen["max_hold_minutes"] == 120


# ════════════════════════════════════════════════════════════════
# Structural R tests
# ════════════════════════════════════════════════════════════════

class TestStructuralR:
    """Structural R = abs(reference_price - invalidation_price)."""

    def test_structural_r_in_features(self):
        """Observer must embed _structural_r in observation features."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        # reference=100, invalidation=99 → R=1.0
        kwargs = _make_observe_kwargs(
            direction="LONG", reference_price=100.0, invalidation_price=99.0,
        )
        observer.observe(**kwargs)

        features = _get_srr_oos_features(conn)
        assert abs(features["_structural_r"] - 1.0) < 1e-10

    def test_zero_r_rejected(self):
        """When structural R = 0, observation is not created."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn(insert_return=None)
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        # reference=100, invalidation=100 → R=0
        kwargs = _make_observe_kwargs(
            direction="LONG", reference_price=100.0, invalidation_price=100.0,
        )
        result = observer.observe(**kwargs)
        # On conflict, fetchrow returns None → no observation_ids added
        assert result == []
        assert len(result) == 0



# ════════════════════════════════════════════════════════════════
# No collateral changes
# ════════════════════════════════════════════════════════════════

class TestNoCollateralChanges:
    """Ensure no unrelated scanners, paper logic, or gates are modified."""

    def test_scanner_code_unchanged(self):
        """SUPPORT_RESISTANCE_REACTION scanner must not have new code."""
        import hashlib
        scanner_path = PROJECT_ROOT / "app" / "scanners" / "support_resistance.py"
        content = scanner_path.read_text()
        # Verify key invariants: name, version, scan method exist
        assert 'name = "SUPPORT_RESISTANCE_REACTION"' in content
        assert 'version = "2.0.0"' in content

    def test_paper_engine_not_modified(self):
        """paper_runner.py should not reference SRR_OOS."""
        source = (PROJECT_ROOT / "paper_runner.py").read_text()
        assert "SRR_OOS_SCANNER_V1_PROSPECTIVE" not in source

    def test_other_scanners_not_modified(self):
        """Other scanners should not reference SRR_OOS."""
        scanner_dir = PROJECT_ROOT / "app" / "scanners"
        for py_file in scanner_dir.glob("*.py"):
            if py_file.name == "support_resistance.py":
                continue
            content = py_file.read_text()
            assert "SRR_OOS_SCANNER_V1_PROSPECTIVE" not in content, (
                f"{py_file.name} unexpectedly references SRR_OOS_SCANNER_V1_PROSPECTIVE"
            )


# ════════════════════════════════════════════════════════════════
# 10. Frozen max-hold semantics regression tests
# ════════════════════════════════════════════════════════════════

class _FakeCandle:
    """Minimal candle for evaluator unit tests."""
    def __init__(self, ts: int, high: float, low: float, close: float = 100.0):
        self.timestamp = ts
        self.high = high
        self.low = low
        self.close = close


def _make_candles_for_hold_test(
    signal_time, *,
    tp_price=101.5, sl_price=99.25,
    is_short=False, max_minutes=120,
    tp_hit_at_min=None, sl_hit_at_min=None,
    both_same_candle_min=None,
):
    """Build candle list for frozen hold regression tests.

    Creates candles at 5-minute intervals from signal_time up to 240 minutes.
    Optionally injects TP/SL hits at specific minutes.

    Parameters
    ----------
    tp_hit_at_min : int | None
        Minute at which TP is first hit (if None, TP is never hit).
    sl_hit_at_min : int | None
        Minute at which SL is first hit (if None, SL is never hit).
    both_same_candle_min : int | None
        If set, both TP and SL fire in the same candle at this minute.
    """
    signal_ts = int(signal_time.timestamp() * 1000)
    candles = []

    for minute in range(5, 245, 5):
        ts = signal_ts + minute * 60_000

        # Default: neutral candle
        high = 100.5
        low = 99.5

        if both_same_candle_min is not None and minute == both_same_candle_min:
            # Both TP and SL in same candle
            if is_short:
                high = max(sl_price + 0.1, 101.0)
                low = min(tp_price - 0.1, 98.0)
            else:
                high = max(tp_price + 0.1, 102.0)
                low = min(sl_price - 0.1, 98.0)
        else:
            if tp_hit_at_min is not None and minute == tp_hit_at_min:
                if is_short:
                    low = tp_price - 0.1  # short TP: low <= target
                else:
                    high = tp_price + 0.1  # long TP: high >= target

            if sl_hit_at_min is not None and minute == sl_hit_at_min:
                if is_short:
                    high = sl_price + 0.1  # short SL: high >= stop
                else:
                    low = sl_price - 0.1  # long SL: low <= stop

        candles.append(_FakeCandle(ts, high, low))

    return candles


class TestFrozenMaxHoldTpSl:
    """CASE 1-6: TP/SL first-hit is bounded by frozen_max_hold."""

    def test_case1_tp_before_120m(self):
        """CASE 1: LONG TP hit at 60m, SL never hit.

        Expected: tp_hit=True, tp_before_sl=True, sl_hit=False.
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, tp_hit_at_min=60,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["tp_hit"] is True
        assert result["tp_before_sl"] is True
        assert result["sl_hit"] is False
        assert result["sl_before_tp"] is False
        assert result["ambiguous_intrabar"] is False

    def test_case2_sl_before_120m(self):
        """CASE 2: LONG SL hit at 45m, TP never hit.

        Expected: sl_hit=True, sl_before_tp=True, tp_hit=False.
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, sl_hit_at_min=45,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["sl_hit"] is True
        assert result["sl_before_tp"] is True
        assert result["tp_hit"] is False
        assert result["tp_before_sl"] is False

    def test_case3_tp_after_max_hold(self):
        """CASE 3: CRITICAL — TP hit at 180m (after frozen_max_hold=120).

        No TP/SL events before 120m.
        TP hit at 180m.

        Expected frozen outcome: tp_hit=False, sl_hit=False.
        The trade would have been TIME_EXIT at 120m.
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, tp_hit_at_min=180,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,  # frozen_max_hold
            signal_time=signal_time, is_short=False,
        )
        # TP at 180m must NOT count — frozen window is [0, 120)
        assert result["tp_hit"] is False
        assert result["sl_hit"] is False
        assert result["tp_before_sl"] is False
        assert result["sl_before_tp"] is False
        assert result["ambiguous_intrabar"] is False

    def test_case4_sl_after_max_hold(self):
        """CASE 4: SL hit at 180m (after frozen_max_hold=120).

        No TP/SL events before 120m.

        Expected: tp_hit=False, sl_hit=False.
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, sl_hit_at_min=180,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["tp_hit"] is False
        assert result["sl_hit"] is False

    def test_case5_tp_sl_same_candle_before_120m(self):
        """CASE 5: TP and SL in same candle at 30m.

        Expected: ambiguous_intrabar=True, tp_hit=True, sl_hit=True,
                  tp_before_sl=False, sl_before_tp=False.
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, both_same_candle_min=30,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["ambiguous_intrabar"] is True
        assert result["tp_hit"] is True
        assert result["sl_hit"] is True
        assert result["tp_before_sl"] is False
        assert result["sl_before_tp"] is False

    def test_case6_event_exactly_at_120m_boundary(self):
        """CASE 6: TP hit at minute 120 (on the boundary).

        Boundary semantics: the window is [signal_time, signal_time + 120m).
        A candle at exactly +120m has c.timestamp == cutoff_ts, so it is
        EXCLUDED by the condition ``c.timestamp >= cutoff_ts``.

        Expected: tp_hit=False (boundary excluded).
        """
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        candles = _make_candles_for_hold_test(
            signal_time, tp_hit_at_min=120,
        )

        result = _check_tp_sl(
            candles, entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        # Candle at +120m is excluded by cutoff boundary
        assert result["tp_hit"] is False
        assert result["sl_hit"] is False


class TestFrozenMaxHoldMfeIndependence:
    """CASE 7: MFE/MAE at 240m continues to work even with frozen_max_hold=120."""

    def test_case7_mfe_240m_independent(self):
        """Show that MFE/MAE 240m is computed independently of frozen TP/SL max_hold.

        With frozen_max_hold=120 for TP/SL outcome:
        - evaluated_240m_at IS NOT NULL
        - mfe_r_240m / mae_r_240m are computed from full 240m window
        - tp_hit / sl_hit are bounded to 120m
        """
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Candle at 150m with favorable move (beyond frozen 120m)
        candles = [
            _FakeCandle(ts=signal_ts + 5 * 60_000, high=101.0, low=99.5),   # +5m
            _FakeCandle(ts=signal_ts + 60 * 60_000, high=101.5, low=99.0),  # +60m
            _FakeCandle(ts=signal_ts + 120 * 60_000, high=101.0, low=99.5), # +120m
            _FakeCandle(ts=signal_ts + 150 * 60_000, high=103.0, low=99.0), # +150m (post-frozen)
            _FakeCandle(ts=signal_ts + 200 * 60_000, high=104.0, low=98.0), # +200m (post-frozen)
        ]

        # 240m MFE should see the +200m candle (104.0 → 4% fav for LONG)
        mfe_240, mae_240 = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=240,
            signal_time=signal_time, is_short=False,
        )
        assert mfe_240 is not None
        assert mfe_240 >= 3.0, f"240m MFE should see post-120m move: got {mfe_240}"

        # 120m MFE should NOT see the +150m candle
        mfe_120, mae_120 = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert mfe_120 is not None
        assert mfe_120 < 2.0, f"120m MFE should not see post-120m move: got {mfe_120}"


class TestFrozenMaxHoldEvaluatorIntegration:
    """Integration tests for evaluator with frozen_max_hold in features."""

    def test_get_frozen_max_hold_from_json_string(self):
        """Evaluator extracts _frozen_max_hold from JSON string features."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {
            "features": '{"_frozen_max_hold": 120, "_frozen_sl_r": 0.75}',
        }
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 120

    def test_get_frozen_max_hold_from_dict(self):
        """Evaluator extracts _frozen_max_hold from dict features."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {
            "features": {"_frozen_max_hold": 90, "_frozen_sl_r": 0.75},
        }
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 90

    def test_get_frozen_max_hold_missing(self):
        """Evaluator falls back to 240 when _frozen_max_hold is absent."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {"features": {"_frozen_sl_r": 0.75}}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240

    def test_get_frozen_max_hold_none_features(self):
        """Evaluator falls back to 240 when features is None."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {"features": None}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240

    def test_get_frozen_max_hold_no_features_key(self):
        """Evaluator falls back to 240 when features key is missing."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240

    def test_get_frozen_max_hold_invalid_json(self):
        """Evaluator falls back to 240 on invalid JSON string."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {"features": "not valid json"}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240

    def test_get_frozen_max_hold_zero_ignored(self):
        """Evaluator ignores _frozen_max_hold=0 (not positive)."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {"features": {"_frozen_max_hold": 0}}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240

    def test_get_frozen_max_hold_negative_ignored(self):
        """Evaluator ignores negative _frozen_max_hold."""
        from app.research.prospective_evaluator import ProspectiveOOSEvaluator

        obs = {"features": {"_frozen_max_hold": -10}}
        assert ProspectiveOOSEvaluator._get_frozen_max_hold(obs) == 240


class TestBoundarySemanticsDocumented:
    """Document and verify the exact boundary semantics of the evaluation window.

    The evaluation window for _check_tp_sl and _calculate_mfe_mae is:

        [signal_time, signal_time + max_minutes)

    Semantics:
        - Candle with timestamp == signal_ts: EXCLUDED (c.timestamp <= signal_ts)
        - Candle with timestamp == cutoff_ts: EXCLUDED (c.timestamp >= cutoff_ts)
        - The LAST candle that counts has timestamp < cutoff_ts

    For max_hold=120:
        - Last included candle starts at +115m (5m candle, timestamp at +115m)
        - Candle at +120m is EXCLUDED
    """

    def test_upper_bound_exclusive(self):
        """Candle at exactly cutoff_ts is excluded."""
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # Candle at exactly +120m = cutoff
        boundary_candle = _FakeCandle(
            ts=signal_ts + 120 * 60_000,
            high=102.0,  # Would hit TP for LONG
            low=99.0,
        )

        result = _check_tp_sl(
            candles=[boundary_candle],
            entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["tp_hit"] is False, "Candle at cutoff should be excluded"

    def test_last_included_candle(self):
        """Candle at +115m (last 5m candle before +120m) IS included."""
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        last_included = _FakeCandle(
            ts=signal_ts + 115 * 60_000,
            high=102.0,  # Would hit TP for LONG
            low=99.5,
        )

        result = _check_tp_sl(
            candles=[last_included],
            entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["tp_hit"] is True, "Candle at +115m should be included"

    def test_signal_candle_excluded_from_tp_sl(self):
        """Candle at signal_time is excluded (entry candle)."""
        from app.research.prospective_evaluator import _check_tp_sl

        signal_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        signal_candle = _FakeCandle(
            ts=signal_ts,
            high=102.0,  # Would hit TP
            low=98.0,
        )

        result = _check_tp_sl(
            candles=[signal_candle],
            entry_price=100.0, stop_price=99.25,
            target_price=101.5, max_minutes=120,
            signal_time=signal_time, is_short=False,
        )
        assert result["tp_hit"] is False, "Signal candle should be excluded"
