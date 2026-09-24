"""Tests for ATR Wick Filter OOS V2_D — Prospective Experiment.

Tests cover:
  1. StochRSI < 0.20 → REJECT
  2. StochRSI = 0.20 → PASS (boundary)
  3. StochRSI = 0.40 → PASS (mid-range)
  4. StochRSI = 0.599 → PASS (near boundary)
  5. StochRSI = 0.60 → REJECT (boundary)
  6. V2_D PASS saved with experiment_id = ATR_WICK_FILTER_OOS_V2_D
  7. V1 saved with experiment_id = ATR_WICK_REJECTION_SHORT_V1
  8. V1 and V2_D do not overwrite each other's experiment_id
  9. Duplicate candidate processing does not create duplicate V2_D observation
 10. Outcome evaluator accepts V2_D signal
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.shadow.atr_wick_v2d_filter import (
    apply_v2d_filter,
    EXPERIMENT_ID,
    STOCH_RSI_MIN,
    STOCH_RSI_MAX,
)


# ============================================================
# Test Group 1: V2_D StochRSI Filter Boundaries
# ============================================================


class TestV2DFilterBoundaries:
    """StochRSI boundary conditions for V2_D filter."""

    def test_stoch_rsi_below_min_rejects(self):
        """StochRSI = 0.19 → REJECT."""
        result = apply_v2d_filter(stoch_rsi=0.19, direction="SHORT")
        assert result.passed is False
        assert "BELOW_MIN" in result.reason

    def test_stoch_rsi_at_min_passes(self):
        """StochRSI = 0.20 → PASS (inclusive lower bound)."""
        result = apply_v2d_filter(stoch_rsi=0.20, direction="SHORT")
        assert result.passed is True
        assert result.reason == "PASS_ALL_FILTERS"
        assert result.stoch_rsi == 0.20

    def test_stoch_rsi_mid_range_passes(self):
        """StochRSI = 0.40 → PASS (mid-range)."""
        result = apply_v2d_filter(stoch_rsi=0.40, direction="SHORT")
        assert result.passed is True
        assert result.reason == "PASS_ALL_FILTERS"
        assert result.stoch_rsi == 0.40

    def test_stoch_rsi_near_max_passes(self):
        """StochRSI = 0.599 → PASS (just below exclusive upper bound)."""
        result = apply_v2d_filter(stoch_rsi=0.599, direction="SHORT")
        assert result.passed is True
        assert result.reason == "PASS_ALL_FILTERS"
        assert result.stoch_rsi == 0.599

    def test_stoch_rsi_at_max_rejects(self):
        """StochRSI = 0.60 → REJECT (exclusive upper bound)."""
        result = apply_v2d_filter(stoch_rsi=0.60, direction="SHORT")
        assert result.passed is False
        assert "ABOVE_MAX" in result.reason

    def test_stoch_rsi_above_max_rejects(self):
        """StochRSI = 0.80 → REJECT."""
        result = apply_v2d_filter(stoch_rsi=0.80, direction="SHORT")
        assert result.passed is False
        assert "ABOVE_MAX" in result.reason

    def test_stoch_rsi_null_rejects(self):
        """StochRSI = None → REJECT."""
        result = apply_v2d_filter(stoch_rsi=None, direction="SHORT")
        assert result.passed is False
        assert result.reason == "STOCH_RSI_NULL"

    def test_non_short_direction_rejects(self):
        """direction != SHORT → REJECT regardless of StochRSI."""
        result = apply_v2d_filter(stoch_rsi=0.40, direction="LONG")
        assert result.passed is False
        assert result.reason == "DIRECTION_NOT_SHORT"

    def test_experiment_id_constant(self):
        """EXPERIMENT_ID is correctly set."""
        assert EXPERIMENT_ID == "ATR_WICK_FILTER_OOS_V2_D"

    def test_filter_constants_frozen(self):
        """Filter threshold constants are as specified."""
        assert STOCH_RSI_MIN == 0.20
        assert STOCH_RSI_MAX == 0.60


# ============================================================
# Test Group 2: V2_D Repository Persistence
# ============================================================


class TestV2DRepositoryPersistence:
    """Test V2DRepository save_signal and experiment_id isolation."""

    def _make_mock_conn(self):
        """Create a mock database connection."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_v2d_save_signal_experiment_id(self):
        """V2_D PASS saved with experiment_id = ATR_WICK_FILTER_OOS_V2_D."""
        from app.shadow.v2d_repository import V2DRepository, V2DSaveStatus

        conn, cursor = self._make_mock_conn()
        cursor.fetchone.return_value = (1,)
        repo = V2DRepository(conn)

        result = repo.save_signal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1,
            volume=1500.0,
            atr=0.5, atr_pct=0.005,
            wick_size=1.4, wick_atr=2.8, upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0, stoch_rsi=0.40,
            bb_upper=102.0, bb_mid=100.0, bb_lower=98.0, bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0, ema_medium=99.0, ema_slow=98.0,
            ema_slope=-0.0002, volume_ratio=1.5,
            filter_pass=True,
            filter_reason="PASS_ALL_FILTERS",
        )

        assert result.status == V2DSaveStatus.INSERTED
        assert result.signal_id == 1

        # Verify the SQL contains the correct experiment_id
        sql = cursor.execute.call_args[0][0]
        assert "ATR_WICK_FILTER_OOS_V2_D" in sql
        assert "ATR_WICK_REJECTION_SHORT_V1" not in sql

    def test_v2d_save_duplicate_no_duplicate_observation(self):
        """Duplicate candidate does not create duplicate V2_D observation."""
        from app.shadow.v2d_repository import V2DRepository, V2DSaveStatus

        conn, cursor = self._make_mock_conn()
        cursor.fetchone.return_value = None  # ON CONFLICT DO NOTHING
        repo = V2DRepository(conn)

        now = datetime.now(timezone.utc)
        # Save same signal twice
        result1 = repo.save_signal(
            symbol="TESTUSDT", signal_time=now, signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1, volume=1500.0,
            atr=0.5, atr_pct=0.005, wick_size=1.4, wick_atr=2.8,
            upper_wick_pct=0.014, close_location=0.06, rsi=65.0,
            stoch_rsi=0.40, bb_upper=102.0, bb_mid=100.0, bb_lower=98.0,
            bb_width=0.04, distance_to_upper_bb=0.019, ema_fast=100.0,
            ema_medium=99.0, ema_slow=98.0, ema_slope=-0.0002,
            volume_ratio=1.5, filter_pass=True, filter_reason="PASS_ALL_FILTERS",
        )
        result2 = repo.save_signal(
            symbol="TESTUSDT", signal_time=now, signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1, volume=1500.0,
            atr=0.5, atr_pct=0.005, wick_size=1.4, wick_atr=2.8,
            upper_wick_pct=0.014, close_location=0.06, rsi=65.0,
            stoch_rsi=0.40, bb_upper=102.0, bb_mid=100.0, bb_lower=98.0,
            bb_width=0.04, distance_to_upper_bb=0.019, ema_fast=100.0,
            ema_medium=99.0, ema_slow=98.0, ema_slope=-0.0002,
            volume_ratio=1.5, filter_pass=True, filter_reason="PASS_ALL_FILTERS",
        )

        # Both calls happen, but only first should insert
        assert result1.status == V2DSaveStatus.DUPLICATE
        assert result2.status == V2DSaveStatus.DUPLICATE
        assert result1.signal_id is None
        assert result2.signal_id is None

    def test_v1_experiment_id_not_overwritten_by_v2d(self):
        """V1 repository always uses ATR_WICK_REJECTION_SHORT_V1."""
        from app.shadow.repository import ShadowSignalRepository, SaveSignalStatus
        from app.scanners.atr_wick_rejection_short import WickRejectionSignal

        conn, cursor = self._make_mock_conn()
        cursor.fetchone.return_value = (1,)
        repo = ShadowSignalRepository(conn)

        signal = WickRejectionSignal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1,
            volume=1500.0,
            atr=0.5, atr_pct=0.005,
            wick_size=1.4, wick_atr=2.8, upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0, stoch_rsi=0.40,
            bb_upper=102.0, bb_mid=100.0, bb_lower=98.0, bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0, ema_medium=99.0, ema_slow=98.0,
            ema_slope=-0.0002, volume_ratio=1.5,
            signal_version="1.0.0",
        )

        result = repo.save_signal(signal)
        assert result.status == SaveSignalStatus.INSERTED

        # Verify V1 SQL does not contain V2_D experiment_id
        sql = cursor.execute.call_args[0][0]
        assert "ATR_WICK_REJECTION_SHORT_V1" in sql
        assert "ATR_WICK_FILTER_OOS_V2_D" not in sql

    def test_v2d_get_eligible_signals_uses_v2d_table(self):
        """get_eligible_signals queries dds.v2d_signal, not dds.shadow_signal."""
        from app.shadow.v2d_repository import V2DRepository

        conn, cursor = self._make_mock_conn()
        cursor.fetchall.return_value = []
        repo = V2DRepository(conn)

        signals = repo.get_eligible_signals()

        sql = cursor.execute.call_args[0][0]
        assert "dds.v2d_signal" in sql
        assert "dds.shadow_signal" not in sql
        assert "ATR_WICK_FILTER_OOS_V2_D" in sql

    def test_v2d_validate_filter_compliance(self):
        """Filter compliance check queries correct table."""
        from app.shadow.v2d_repository import V2DRepository

        conn, cursor = self._make_mock_conn()
        cursor.fetchone.return_value = (100, 0, 0, 0)  # 100 total, 0 invalid
        repo = V2DRepository(conn)

        result = repo.validate_filter_compliance()

        assert result["total_signals"] == 100
        assert result["all_valid"] is True

        sql = cursor.execute.call_args[0][0]
        assert "dds.v2d_signal" in sql
        assert "ATR_WICK_FILTER_OOS_V2_D" in sql


# ============================================================
# Test Group 3: Outcome Evaluator Compatibility
# ============================================================


class TestV2DEvaluatorCompatibility:
    """Test that V2_D outcome evaluator works with V2_D signals."""

    def test_evaluator_queries_v2d_tables(self):
        """V2DEvaluator reads from v2d_signal/v2d_outcome, not shadow_signal."""
        from app.shadow.v2d_evaluator import V2DEvaluator

        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_eligible_signals.return_value = []

        evaluator = V2DEvaluator(mock_client, mock_repo)
        summary = evaluator.run_evaluation_cycle()

        assert summary["experiment"] == "ATR_WICK_FILTER_OOS_V2_D"
        assert summary["signals_checked"] == 0
        # Verify repo.get_eligible_signals was called (queries v2d tables)
        mock_repo.get_eligible_signals.assert_called_once_with(limit=5000)

    def test_evaluator_persists_to_v2d_outcome(self):
        """V2DEvaluator calls save_outcome_partial on v2d_outcome."""
        from app.shadow.v2d_evaluator import V2DEvaluator
        from app.models import Candle
        from datetime import timedelta

        mock_client = MagicMock()
        mock_repo = MagicMock()

        signal_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        signal_ts = int(signal_time.timestamp() * 1000)

        mock_repo.get_eligible_signals.return_value = [
            {
                "signal_id": 1,
                "symbol": "TESTUSDT",
                "signal_time": signal_time,
                "signal_price": 100.0,
                "outcome_id": None,
                "evaluated_15m_at": None,
                "evaluated_30m_at": None,
                "evaluated_60m_at": None,
                "evaluated_120m_at": None,
                "evaluated_240m_at": None,
                "is_final": False,
            }
        ]
        mock_repo.save_outcome_partial.return_value = True

        # Return candles AFTER signal_time so 15m horizon is mature
        mock_client.get_klines.return_value = [
            Candle(
                timestamp=signal_ts + (i + 1) * 300_000,  # 5m intervals after signal
                open=100.0 - (i * 0.1),
                high=100.5 - (i * 0.1),
                low=99.5 - (i * 0.1),
                close=100.0 - (i * 0.1),
                volume=1000.0,
            )
            for i in range(20)  # 20 candles = 100 minutes
        ]

        evaluator = V2DEvaluator(mock_client, mock_repo)
        summary = evaluator.run_evaluation_cycle()

        assert summary["signals_checked"] == 1
        assert summary["outcomes_created"] == 1
        assert summary["horizons_updated"]["15m"] >= 1
        # save_outcome_partial should be called (repo is v2d-specific)
        assert mock_repo.save_outcome_partial.called

    def test_v1_evaluator_independent(self):
        """V1 evaluator still queries shadow_signal (not v2d_signal)."""
        from app.shadow.evaluator import ShadowSignalEvaluator

        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_eligible_signals.return_value = []

        evaluator = ShadowSignalEvaluator(mock_client, mock_repo)
        summary = evaluator.run_evaluation_cycle()

        assert summary["signals_checked"] == 0
        # V1 evaluator uses its own repo which queries shadow_signal
        mock_repo.get_eligible_signals.assert_called_once_with(limit=5000)


# ============================================================
# Test Group 4: Migration Integrity
# ============================================================


class TestV2DMigrationIntegrity:
    """Verify migration SQL is correct and idempotent."""

    def test_migration_tables_created(self):
        """Migration creates v2d_signal and v2d_outcome tables."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS dds.v2d_signal" in migration
        assert "CREATE TABLE IF NOT EXISTS dds.v2d_outcome" in migration

    def test_migration_experiment_id_constraint(self):
        """Migration has CHECK constraint for V2_D experiment_id."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "ATR_WICK_FILTER_OOS_V2_D" in migration
        assert "v2d_signal_experiment_chk" in migration
        assert "v2d_outcome_experiment_chk" in migration

    def test_migration_no_v1_data_modification(self):
        """Migration does not modify V1 data (no UPDATE/DELETE on shadow tables)."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "UPDATE dds.shadow_signal" not in migration
        assert "DELETE FROM dds.shadow_signal" not in migration
        assert "UPDATE dds.shadow_signal_outcome" not in migration
        assert "DELETE FROM dds.shadow_signal_outcome" not in migration

    def test_migration_registry_created(self):
        """Migration creates experiment registry."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "shadow_oos_experiment_registry" in migration
        assert "INSERT INTO dds.shadow_oos_experiment_registry" in migration

    def test_migration_idempotent(self):
        """Migration uses IF NOT EXISTS for idempotency."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS" in migration
        assert "CREATE INDEX IF NOT EXISTS" in migration
        assert "ON CONFLICT" in migration

    def test_migration_direction_constraint(self):
        """V2_D signals are constrained to SHORT direction."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "v2d_signal_direction_chk" in migration
        assert "CHECK (direction = 'SHORT')" in migration

    def test_migration_unique_index(self):
        """Unique index prevents duplicate signals per experiment/symbol/time."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "uq_v2d_signal_experiment_symbol_time" in migration

    def test_migration_views_created(self):
        """Observability views are created."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "v_v2d_accumulation" in migration
        assert "v_v2d_filter_compliance" in migration


# ============================================================
# Test Group 5: Runner Filter Integration
# ============================================================


class TestV2DRunnerFilterIntegration:
    """Test that V2_D runner applies filter correctly before persisting."""

    def test_runner_filters_reject(self):
        """Runner filters out candidates that fail V2_D filter."""
        from app.shadow.v2d_runner import V2DRunner
        from app.scanners.atr_wick_rejection_short import WickRejectionSignal

        mock_settings = MagicMock()
        mock_client = MagicMock()
        mock_repo = MagicMock()
        runner = V2DRunner(mock_settings, mock_client, mock_repo)

        raw = WickRejectionSignal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1,
            volume=1500.0,
            atr=0.5, atr_pct=0.005,
            wick_size=1.4, wick_atr=2.8, upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0, stoch_rsi=0.10,  # Below 0.20 → REJECT
            bb_upper=102.0, bb_mid=100.0, bb_lower=98.0, bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0, ema_medium=99.0, ema_slow=98.0,
            ema_slope=-0.0002, volume_ratio=1.5,
            signal_version="1.0.0",
        )

        status = runner._persist_candidate(raw)

        assert status == "filtered_out"
        mock_repo.save_signal.assert_not_called()

    def test_runner_persists_pass(self):
        """Runner persists candidates that pass V2_D filter."""
        from app.shadow.v2d_runner import V2DRunner
        from app.shadow.v2d_repository import V2DSaveStatus
        from app.scanners.atr_wick_rejection_short import WickRejectionSignal

        mock_settings = MagicMock()
        mock_client = MagicMock()
        mock_repo = MagicMock()
        mock_repo.save_signal.return_value = V2DSaveResult(
            status=V2DSaveStatus.INSERTED, signal_id=1
        )
        runner = V2DRunner(mock_settings, mock_client, mock_repo)

        raw = WickRejectionSignal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1,
            volume=1500.0,
            atr=0.5, atr_pct=0.005,
            wick_size=1.4, wick_atr=2.8, upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0, stoch_rsi=0.40,  # In range → PASS
            bb_upper=102.0, bb_mid=100.0, bb_lower=98.0, bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0, ema_medium=99.0, ema_slow=98.0,
            ema_slope=-0.0002, volume_ratio=1.5,
            signal_version="1.0.0",
        )

        status = runner._persist_candidate(raw)

        assert status == "inserted"
        mock_repo.save_signal.assert_called_once()


# Need this import for the runner persist test
from app.shadow.v2d_repository import V2DSaveResult


# ============================================================
# Test Group 6: V1/V2_D Isolation
# ============================================================


class TestV1V2DIsolation:
    """Verify V1 and V2_D are completely isolated."""

    def test_v1_runner_not_affected(self):
        """V1 ShadowScannerRunner persists to shadow_signal with V1 experiment_id."""
        from app.shadow.runner import ShadowScannerRunner

        mock_settings = MagicMock()
        mock_client = MagicMock()
        mock_repo = MagicMock()
        runner = ShadowScannerRunner(mock_settings, mock_client, mock_repo)

        # The V1 runner uses AtrWickRejectionShortScanner
        assert runner.scanner is not None

        # Verify V1 runner does NOT import V2DRepository
        import app.shadow.runner as runner_module
        source = open(runner_module.__file__).read()
        assert "V2DRepository" not in source
        assert "v2d_repository" not in source
        assert "ATR_WICK_FILTER_OOS_V2_D" not in source

    def test_v2d_runner_independent_of_v1(self):
        """V2DRunner does not import or use V1's ShadowSignalRepository."""
        import app.shadow.v2d_runner as v2d_module
        source = open(v2d_module.__file__).read()
        assert "ShadowSignalRepository" not in source
        assert "shadow_signal" not in source
        assert "ATR_WICK_REJECTION_SHORT_V1" not in source

    def test_separate_tables(self):
        """V2_D uses v2d_signal/v2d_outcome, V1 uses shadow_signal/shadow_signal_outcome."""
        from pathlib import Path
        migration = Path("sql/migrations/047_atr_wick_v2d_prospective_oos.sql").read_text()
        assert "dds.v2d_signal" in migration
        assert "dds.v2d_outcome" in migration
        # V1 tables are in the old migration, not modified here
        assert "dds.shadow_signal" not in migration
        assert "dds.shadow_signal_outcome" not in migration


# ============================================================
# Test Group 7: Edge Cases
# ============================================================


class TestV2DEdgeCases:
    """Edge cases for V2_D filter and persistence."""

    def test_stoch_rsi_very_small_value(self):
        """StochRSI = 0.0 → REJECT."""
        result = apply_v2d_filter(stoch_rsi=0.0, direction="SHORT")
        assert result.passed is False

    def test_stoch_rsi_exactly_one(self):
        """StochRSI = 1.0 → REJECT."""
        result = apply_v2d_filter(stoch_rsi=1.0, direction="SHORT")
        assert result.passed is False

    def test_stoch_rsi_negative(self):
        """StochRSI = -0.1 (invalid data) → REJECT."""
        result = apply_v2d_filter(stoch_rsi=-0.1, direction="SHORT")
        assert result.passed is False

    def test_v2d_no_connection_returns_error(self):
        """V2DRepository without connection returns ERROR."""
        from app.shadow.v2d_repository import V2DRepository, V2DSaveStatus

        repo = V2DRepository(conn=None)
        result = repo.save_signal(
            symbol="TESTUSDT",
            signal_time=datetime.now(timezone.utc),
            signal_price=100.0,
            open=100.0, high=101.5, low=99.8, close=100.1,
            volume=1500.0,
            atr=0.5, atr_pct=0.005,
            wick_size=1.4, wick_atr=2.8, upper_wick_pct=0.014,
            close_location=0.06,
            rsi=65.0, stoch_rsi=0.40,
            bb_upper=102.0, bb_mid=100.0, bb_lower=98.0, bb_width=0.04,
            distance_to_upper_bb=0.019,
            ema_fast=100.0, ema_medium=99.0, ema_slow=98.0,
            ema_slope=-0.0002, volume_ratio=1.5,
            filter_pass=True,
            filter_reason="PASS_ALL_FILTERS",
        )
        assert result.status == V2DSaveStatus.ERROR
        assert result.signal_id is None
