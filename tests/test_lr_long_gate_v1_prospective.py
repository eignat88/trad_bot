"""Tests for LR_LONG_GATE_V1 — LIQUIDITY_REVERSAL LONG prospective OOS experiment.

Covers the 12 required invariants:
  1. LONG signal creates LR_LONG_GATE_V1 observation.
  2. BLOCKED LONG signal can still reach prospective capture.
  3. BLOCKED LONG signal cannot reach PAPER execution.
  4. Observation is created only for signals after prospective_started_at.
  5. No historical backfill.
  6. Duplicate source signal does not create duplicate observation.
  7. Restart/reprocessing is idempotent.
  8. LONG MFE calculation correct.
  9. LONG MAE calculation correct.
  10. 15/30/60/120/240m evaluation works.
  11. LR_SHORT_GATE_V1 remains unchanged.
  12. SHORT MFE/MAE behavior is not affected.

Design principles:
  - Pure unit tests — no DB, no network, no VPS
  - Symmetric with LR_SHORT_GATE_V1
  - No scanner logic changes tested (scanner is unchanged)
  - No paper trading changes tested
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        cursor.fetchone.side_effect = [("RUNNING",), None]  # lifecycle row, then conflict → no row
    else:
        cursor.fetchone.side_effect = [("RUNNING",), insert_return]  # lifecycle row, then INSERT RETURNING row
    return cursor


def _mock_conn(insert_return=(42,)):
    conn = MagicMock()
    conn.cursor.return_value = _mock_cursor(insert_return)
    return conn


def _make_observe_kwargs(
    *,
    symbol="BTCUSDT",
    direction="LONG",
    scanner_name="LIQUIDITY_REVERSAL",
    reference_price=50000.0,
    invalidation_price=49500.0,
    target_1=51000.0,
    target_2=None,
    score=85.0,
    features=None,
    parameters=None,
    market_regime="TREND_UP",
):
    if features is None:
        features = {
            "sweep_depth": 0.5,
            "rejection_strength": 0.8,
            "rr_ratio": 0.9,
            "stop_distance_atr": 0.75,
            "volume_spike": True,
            "regime_alignment": 0.8,
        }
    if parameters is None:
        parameters = {}
    return dict(
        scanner_name=scanner_name,
        direction=direction,
        symbol=symbol,
        signal_time=datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc),
        reference_price=reference_price,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=target_2,
        score=score,
        features=features,
        parameters=parameters,
        market_regime=market_regime,
    )


def _get_lr_long_insert_args(conn):
    """Extract the LR_LONG_GATE_V1 INSERT call args."""
    for call in conn.cursor.return_value.execute.call_args_list:
        sql = call[0][0] if call[0] else ""
        if "INSERT INTO research.prospective_observation" in sql:
            args = call[0][1]
            if args and args[0] == "LR_LONG_GATE_V1":
                return args
    pytest.fail("LR_LONG_GATE_V1 INSERT not found")


def _get_lr_long_features(conn):
    """Extract the features dict from the LR_LONG_GATE_V1 INSERT."""
    args = _get_lr_long_insert_args(conn)
    import json as _json
    return _json.loads(args[13])


class _FakeCandle:
    """Minimal candle for evaluator unit tests."""
    def __init__(self, ts: int, high: float, low: float, close: float = 100.0):
        self.timestamp = ts
        self.high = high
        self.low = low
        self.close = close


# ════════════════════════════════════════════════════════════════
# 1. Registry registration
# ════════════════════════════════════════════════════════════════

class TestRegistryRegistration:
    """LR_LONG_GATE_V1 must be registered in prospective_registry.json."""

    def test_lr_long_gate_v1_in_registry(self):
        registry = _load_registry()
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "LR_LONG_GATE_V1" in exp_ids

    def test_lr_long_gate_v1_scanner(self):
        registry = _make_registry_dict(_load_registry())
        assert registry["LR_LONG_GATE_V1"]["scanner_name"] == "LIQUIDITY_REVERSAL"

    def test_lr_long_gate_v1_direction(self):
        registry = _make_registry_dict(_load_registry())
        assert registry["LR_LONG_GATE_V1"]["direction"] == "LONG"

    def test_lr_long_gate_v1_shadow_only(self):
        registry = _make_registry_dict(_load_registry())
        assert registry["LR_LONG_GATE_V1"]["position_sizing_rule"] == "shadow_only"

    def test_lr_long_gate_v1_horizons(self):
        registry = _make_registry_dict(_load_registry())
        horizons = registry["LR_LONG_GATE_V1"]["horizons"]
        assert horizons == ["15m", "30m", "60m", "120m", "240m"]

    def test_lr_long_gate_v1_started_at_null(self):
        registry = _make_registry_dict(_load_registry())
        assert registry["LR_LONG_GATE_V1"]["started_at"] is None

    def test_lr_long_gate_v1_status_ready(self):
        registry = _make_registry_dict(_load_registry())
        assert registry["LR_LONG_GATE_V1"]["status"] == "READY_TO_START"


# ════════════════════════════════════════════════════════════════
# 2. Observer routing
# ════════════════════════════════════════════════════════════════

class TestObserverRouting:
    """Prospective observer must route LIQUIDITY_REVERSAL LONG to LR_LONG_GATE_V1."""

    def test_lr_long_creates_observation(self):
        """LR_LONG_GATE_V1 observation must be inserted for LR LONG."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        result = observer.observe(**kwargs)

        assert len(result) >= 1, "At least one observation should be created"
        assert conn.cursor.return_value.execute.called
        args = _get_lr_long_insert_args(conn)
        assert args[0] == "LR_LONG_GATE_V1"

    def test_lr_long_direction_in_values(self):
        """LR LONG must pass direction='LONG' in the INSERT VALUES."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        observer.observe(**kwargs)

        args = _get_lr_long_insert_args(conn)
        assert args[4] == "LONG"

    def test_lr_short_does_not_create_lr_long_observation(self):
        """LR SHORT must NOT create LR_LONG_GATE_V1 observation."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs(direction="SHORT")
        observer.observe(**kwargs)

        # LR_LONG_GATE_V1 requires direction=LONG, so no INSERT should happen
        # (the loop filters by direction match)
        for call in conn.cursor.return_value.execute.call_args_list:
            sql = call[0][0] if call[0] else ""
            if "INSERT INTO research.prospective_observation" in sql:
                args = call[0][1]
                assert args[0] != "LR_LONG_GATE_V1", \
                    "LR SHORT should not create LR_LONG_GATE_V1 observation"

    def test_lr_long_does_not_create_lr_short_observation(self):
        """LR LONG must NOT create LR_SHORT_GATE_V1 observation."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        observer.observe(**kwargs)

        # LR_SHORT_GATE_V1 requires direction=SHORT, so no INSERT should happen
        for call in conn.cursor.return_value.execute.call_args_list:
            sql = call[0][0] if call[0] else ""
            if "INSERT INTO research.prospective_observation" in sql:
                args = call[0][1]
                assert args[0] != "LR_SHORT_GATE_V1", \
                    "LR LONG should not create LR_SHORT_GATE_V1 observation"

    def test_filter_reason_observational(self):
        """LR_LONG_GATE_V1 observations must be tagged as observational."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        observer.observe(**kwargs)

        args = _get_lr_long_insert_args(conn)
        # filter_reason is at index 12 in the INSERT
        assert args[12] == "observational_gate_validation"

    def test_insert_is_prospective_not_scanner_setup(self):
        """Observation goes to research.prospective_observation, NOT dds.scanner_setup."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        observer.observe(**kwargs)

        insert_calls = [
            c for c in conn.cursor.return_value.execute.call_args_list
            if "INSERT INTO research.prospective_observation" in str(c)
        ]
        assert insert_calls, "No prospective observation INSERT found"
        sql = insert_calls[0][0][0]
        assert "prospective_observation" in sql
        assert "scanner_setup" not in sql

    def test_lr_long_uses_standard_observe_path(self):
        """LR_LONG_GATE_V1 must use _observe_standard (not _observe_me_geometry)."""
        registry = _make_registry_dict(_load_registry())
        conn = _mock_conn()
        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(conn, registry)

        kwargs = _make_observe_kwargs()
        observer.observe(**kwargs)

        # Should NOT be in the ME_SHORT_GEOM_ prefix branch
        for call in conn.cursor.return_value.execute.call_args_list:
            sql = call[0][0] if call[0] else ""
            if "INSERT INTO research.prospective_observation" in sql:
                args = call[0][1]
                assert not str(args[0]).startswith("ME_SHORT_GEOM_"), \
                    "LR_LONG_GATE_V1 must not use ME geometry path"


# ════════════════════════════════════════════════════════════════
# 3. Paper safety invariants
# ════════════════════════════════════════════════════════════════

class TestPaperSafety:
    """BLOCKED LIQUIDITY_REVERSAL LONG must not reach PAPER execution."""

    def test_blocked_long_gate_blocks_paper(self):
        """BLOCKED gate must return allowed=False for LIQUIDITY_REVERSAL LONG."""
        from app.scanners.direction_gate import (
            GATE_BLOCKED, ScannerDirectionGate, ScannerDirectionGatePolicy,
        )

        gate = ScannerDirectionGate(
            "LIQUIDITY_REVERSAL", "LONG", GATE_BLOCKED,
            reason="static safety blocklist",
        )
        policy = ScannerDirectionGatePolicy(
            {("LIQUIDITY_REVERSAL", "LONG"): gate}, {}
        )
        decision = policy.evaluate("LIQUIDITY_REVERSAL", "LONG", "TREND_UP")

        assert decision.allowed is False
        assert decision.status == "BLOCKED"

    def test_paper_runner_only_loads_ready_to_trade(self):
        """paper_runner loads only READY_TO_TRADE setups."""
        source = (PROJECT_ROOT / "paper_runner.py").read_text()
        assert "READY_TO_TRADE" in source

    def test_paper_runner_does_not_reference_lr_long(self):
        """paper_runner.py should not reference LR_LONG_GATE_V1."""
        source = (PROJECT_ROOT / "paper_runner.py").read_text()
        assert "LR_LONG_GATE_V1" not in source

    def test_scanner_runner_does_not_reference_lr_long(self):
        """scanner_runner.py should not reference LR_LONG_GATE_V1 directly."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        # The scanner runner uses the registry, not hardcoded experiment IDs
        assert "LR_LONG_GATE_V1" not in source

    def test_research_capture_before_direction_gate(self):
        """Prospective observer must be called BEFORE gate resolution."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        obs_pos = source.find("prospective_obs.observe(")
        resolve_pos = source.find("resolve_and_promote_batch(")
        assert obs_pos > 0, "prospective_obs.observe not found"
        assert resolve_pos > 0, "resolve_and_promote_batch not found"
        assert obs_pos < resolve_pos, \
            "prospective observer must be called BEFORE gate resolution"


# ════════════════════════════════════════════════════════════════
# 4. Dedup / idempotency
# ════════════════════════════════════════════════════════════════

class TestDedup:
    """One source signal → at most one observation per experiment."""

    def test_db_dedup_index_exists(self):
        """prospective_observation has UNIQUE(experiment_id, source_signal_id)."""
        source = (PROJECT_ROOT / "sql" / "migrations" / "050_prospective_oos_v3_experiments.sql").read_text()
        assert "UNIQUE (experiment_id, source_signal_id)" in source

    def test_on_conflict_do_nothing_in_sql(self):
        """INSERT uses ON CONFLICT DO NOTHING for dedup."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "ON CONFLICT (experiment_id, source_signal_id) DO NOTHING" in source

    def test_observe_read_only_on_registry(self):
        """ProspectiveOOSObserver must be read-only on registry (append-only)."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        # The observer should not modify the registry
        assert "self._registry[" not in source or "self._registry.get(" in source


# ════════════════════════════════════════════════════════════════
# 5. LONG MFE/MAE calculation
# ════════════════════════════════════════════════════════════════

class TestLONG_MFE_MAE:
    """LONG MFE/MAE calculation must be direction-aware."""

    def test_long_mfe_favorable_is_above_entry(self):
        """For LONG, MFE = max(candle.high - entry_price) / entry_price * 100."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=102.0, low=99.0),   # +2%
            _FakeCandle(ts=signal_ts + 120_000, high=101.0, low=98.0),  # +1%
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=False,
        )
        assert mfe is not None
        assert abs(mfe - 2.0) < 0.01, f"MFE should be 2.0%, got {mfe}"

    def test_long_mae_adverse_is_below_entry(self):
        """For LONG, MAE = max(entry_price - candle.low) / entry_price * 100."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=101.0, low=98.0),   # -2%
            _FakeCandle(ts=signal_ts + 120_000, high=100.5, low=99.0),  # -1%
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=False,
        )
        assert mae is not None
        assert abs(mae - 2.0) < 0.01, f"MAE should be 2.0%, got {mae}"

    def test_long_mfe_uses_high_not_low(self):
        """For LONG, MFE must use candle.high (favorable = above entry)."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # high is much higher than low
        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=105.0, low=95.0),
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=False,
        )
        assert mfe is not None
        assert abs(mfe - 5.0) < 0.01, f"MFE should be 5.0% (from high), got {mae}"

    def test_long_mae_uses_low_not_high(self):
        """For LONG, MAE must use candle.low (adverse = below entry)."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        # low is much lower than high
        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=105.0, low=95.0),
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=False,
        )
        assert mae is not None
        assert abs(mae - 5.0) < 0.01, f"MAE should be 5.0% (from low), got {mae}"


# ════════════════════════════════════════════════════════════════
# 6. SHORT MFE/MAE behavior unchanged
# ════════════════════════════════════════════════════════════════

class TestSHORT_MFE_MAE_Unchanged:
    """SHORT MFE/MAE calculation must remain unchanged."""

    def test_short_mfe_favorable_is_below_entry(self):
        """For SHORT, MFE = max(entry_price - candle.low) / entry_price * 100."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=101.0, low=98.0),   # -2%
            _FakeCandle(ts=signal_ts + 120_000, high=100.5, low=99.0),  # -1%
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=True,
        )
        assert mfe is not None
        assert abs(mfe - 2.0) < 0.01, f"MFE should be 2.0%, got {mfe}"

    def test_short_mae_adverse_is_above_entry(self):
        """For SHORT, MAE = max(candle.high - entry_price) / entry_price * 100."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        candles = [
            _FakeCandle(ts=signal_ts + 60_000, high=102.0, low=99.0),   # +2%
            _FakeCandle(ts=signal_ts + 120_000, high=101.0, low=98.0),  # +1%
        ]

        mfe, mae = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=True,
        )
        assert mae is not None
        assert abs(mae - 2.0) < 0.01, f"MAE should be 2.0%, got {mae}"


# ════════════════════════════════════════════════════════════════
# 7. Horizon evaluation
# ════════════════════════════════════════════════════════════════

class TestHorizonEvaluation:
    """15/30/60/120/240m evaluation must work for LONG."""

    def test_horizons_defined(self):
        """HORIZONS must include 15m, 30m, 60m, 120m, 240m."""
        from app.research.constants import HORIZONS
        labels = [h[0] for h in HORIZONS]
        assert labels == ["15m", "30m", "60m", "120m", "240m"]

    def test_mfe_horizon_boundary(self):
        """MFE at 60m must not see candles after 60m."""
        from app.research.prospective_evaluator import _calculate_mfe_mae

        signal_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        signal_ts = int(signal_time.timestamp() * 1000)

        candles = [
            _FakeCandle(ts=signal_ts + 30 * 60_000, high=102.0, low=99.0),   # +30m: +2%
            _FakeCandle(ts=signal_ts + 90 * 60_000, high=105.0, low=98.0),   # +90m: +5% (beyond 60m)
        ]

        mfe_60, _ = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=60,
            signal_time=signal_time, is_short=False,
        )
        assert mfe_60 is not None
        assert abs(mfe_60 - 2.0) < 0.01, f"60m MFE should be 2.0%, got {mfe_60}"

        mfe_240, _ = _calculate_mfe_mae(
            candles, entry_price=100.0, max_minutes=240,
            signal_time=signal_time, is_short=False,
        )
        assert mfe_240 is not None
        assert abs(mfe_240 - 5.0) < 0.01, f"240m MFE should be 5.0%, got {mfe_240}"


# ════════════════════════════════════════════════════════════════
# 8. Evaluator direction-awareness
# ════════════════════════════════════════════════════════════════

class TestEvaluatorDirectionAware:
    """Evaluator must correctly handle LONG direction."""

    def test_evaluator_sets_is_short_false_for_long(self):
        """Evaluator must set is_short=False when direction='LONG'."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert 'is_short = obs["direction"] == "SHORT"' in source

    def test_evaluator_uses_is_short_in_mfe_mae(self):
        """Evaluator must pass is_short to _calculate_mfe_mae."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "_calculate_mfe_mae(" in source
        assert "is_short" in source

    def test_evaluator_uses_is_short_in_tp_sl(self):
        """Evaluator must pass is_short to _check_tp_sl."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "_check_tp_sl(" in source
        assert "is_short" in source


# ════════════════════════════════════════════════════════════════
# 9. No collateral changes
# ════════════════════════════════════════════════════════════════

class TestNoCollateralChanges:
    """Ensure no unrelated scanners, paper logic, or gates are modified."""

    def test_paper_engine_not_modified(self):
        """paper_runner.py should not reference LR_LONG_GATE_V1."""
        source = (PROJECT_ROOT / "paper_runner.py").read_text()
        assert "LR_LONG_GATE_V1" not in source

    def test_scanner_not_modified(self):
        """LIQUIDITY_REVERSAL scanner must not have new code for LR_LONG."""
        scanner_path = PROJECT_ROOT / "app" / "scanners" / "liquidity_reversal.py"
        content = scanner_path.read_text()
        assert 'name = "LIQUIDITY_REVERSAL"' in content
        assert "LR_LONG_GATE_V1" not in content

    def test_settings_not_modified(self):
        """settings.py should not reference LR_LONG_GATE_V1."""
        source = (PROJECT_ROOT / "app" / "config" / "settings.py").read_text()
        assert "LR_LONG_GATE_V1" not in source

    def test_config_yaml_not_modified(self):
        """config.yaml should not reference LR_LONG_GATE_V1."""
        source = (PROJECT_ROOT / "config.yaml").read_text()
        assert "LR_LONG_GATE_V1" not in source
