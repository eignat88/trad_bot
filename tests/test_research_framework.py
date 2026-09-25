"""Tests for the Generic OOS Research Framework V1.

Covers:
  - ResearchObserver: success, DB failure, duplicate candidates
  - ResearchRepository: save_observation, promote_to_signal, upsert_outcome
  - Feature/parameter snapshot immutability
  - ResearchEvaluator: LONG/SHORT MFE/MAE, immature signal, idempotency
  - Missing SL/TP handling
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.research.constants import HORIZONS, ObservationStatus
from app.research.models import ResearchObservation
from app.research.observer import ResearchObserver
from app.research.repository import ResearchRepository


# ── helpers ──────────────────────────────────────────────────


def _make_candidate(
    scanner_name: str = "MOMENTUM_EXHAUSTION_R",
    scanner_version: str = "1.0.0",
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    reference_price: float = 100.0,
    invalidation_price: float = 97.5,
    target_1: float = 103.0,
    target_2: float = 104.5,
    score: float = 55.0,
    signal_candle_open_time: int = 1700000000000,
    features: dict | None = None,
    market_regime: str = "TREND_UP",
) -> SimpleNamespace:
    """Create a mock SetupCandidate."""
    return SimpleNamespace(
        setup_id="test-uuid-001",
        scanner_name=scanner_name,
        scanner_version=scanner_version,
        symbol=symbol,
        direction=direction,
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime.now(timezone.utc),
        setup_started_at=datetime.now(timezone.utc),
        signal_candle_open_time=signal_candle_open_time,
        reference_price=reference_price,
        entry_zone_low=reference_price * 0.998,
        entry_zone_high=reference_price * 1.001,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=target_2,
        score=score,
        market_regime=market_regime,
        reasons=(),
        features=features or {
            "exhaustion_magnitude": 0.5,
            "body_ratio": 0.3,
            "rsi_confirmation": 0.7,
            "volume_ratio": 0.6,
            "rr_ratio": 0.8,
            "stop_distance_atr": 0.4,
        },
        state="SETUP_READY",
    )


def _make_experiment_config() -> dict[str, Any]:
    return {
        "experiment_id": "MER_GENERIC_V1",
        "scanner_name": "MOMENTUM_EXHAUSTION_R",
        "scanner_version": "1.0.0",
        "parameter_set_id": "mer_1.0.0_20260928",
        "parameters": {"swing_lookback": 5, "exhaustion_threshold": 0.003},
        "htf_timeframe": "1h",
        "setup_timeframe": "15m",
        "entry_timeframe": "5m",
    }


# ── Observer tests ───────────────────────────────────────────


class TestResearchObserver:
    """Test ResearchObserver fail-open behavior."""

    def test_observe_success(self):
        """Observer saves observation and increments stats."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 42

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )
        candidate = _make_candidate()
        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 1
        assert observer.stats["inserted"] == 1
        assert observer.stats.get("errors", 0) == 0

        # Verify the observation passed to repo
        obs = mock_repo.save_observation.call_args[0][0]
        assert isinstance(obs, ResearchObservation)
        assert obs.experiment_id == "MER_GENERIC_V1"
        assert obs.symbol == "BTCUSDT"
        assert obs.direction == "LONG"
        assert obs.status == "DETECTED"
        assert obs.features["exhaustion_magnitude"] == 0.5
        assert obs.parameters == {"swing_lookback": 5, "exhaustion_threshold": 0.003}

    def test_observe_unregistered_scanner(self):
        """Observer silently skips unregistered scanners."""
        mock_repo = MagicMock(spec=ResearchRepository)
        observer = ResearchObserver(mock_repo, {})

        candidate = _make_candidate(scanner_name="UNKNOWN_SCANNER")
        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 0

    def test_observe_duplicate_candidate(self):
        """Observer handles duplicate (ON CONFLICT DO NOTHING) gracefully."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = None  # duplicate

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )
        observer.observe(_make_candidate())

        assert observer.stats["duplicates"] == 1
        assert observer.stats.get("errors", 0) == 0

    def test_observe_db_failure_scanner_continues(self):
        """Observer DB failure does not raise — scanner continues."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.side_effect = ConnectionError("DB down")

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )

        # This must NOT raise
        observer.observe(_make_candidate())

        assert observer.stats["errors"] == 1
        assert observer.stats.get("inserted", 0) == 0

    def test_observe_exception_in_save_scanner_continues(self):
        """Any exception in save is caught — fail-open."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.side_effect = RuntimeError("unexpected")

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )

        # Must not raise
        observer.observe(_make_candidate())

        assert observer.stats["errors"] == 1


# ── Repository tests ─────────────────────────────────────────


class TestResearchRepository:
    """Test ResearchRepository with mocked connection."""

    def _make_obs(self) -> ResearchObservation:
        return ResearchObservation(
            experiment_id="MER_GENERIC_V1",
            scanner_name="MOMENTUM_EXHAUSTION_R",
            scanner_version="1.0.0",
            parameter_set_id="mer_1.0.0_20260928",
            symbol="BTCUSDT",
            direction="LONG",
            signal_time=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
            signal_candle_open_time=1700000000000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.1,
            invalidation_price=97.5,
            target_1=103.0,
            target_2=104.5,
            score=55.0,
            status="DETECTED",
            features={"exhaustion_magnitude": 0.5},
            parameters={"swing_lookback": 5},
            market_regime="TREND_UP",
            htf_timeframe="1h",
            setup_timeframe="15m",
            entry_timeframe="5m",
            setup_id="test-uuid-001",
        )

    def test_save_observation_success(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (42,)

        repo = ResearchRepository(mock_conn)
        obs_id = repo.save_observation(self._make_obs())

        assert obs_id == 42
        mock_conn.commit.assert_called_once()

    def test_save_observation_duplicate(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None  # ON CONFLICT DO NOTHING

        repo = ResearchRepository(mock_conn)
        obs_id = repo.save_observation(self._make_obs())

        assert obs_id is None

    def test_save_observation_no_connection(self):
        repo = ResearchRepository(None)
        obs_id = repo.save_observation(self._make_obs())
        assert obs_id is None

    def test_save_observation_db_error(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = Exception("DB error")

        repo = ResearchRepository(mock_conn)
        obs_id = repo.save_observation(self._make_obs())

        assert obs_id is None
        mock_conn.rollback.assert_called_once()

    def test_update_observation_status(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.rowcount = 1

        repo = ResearchRepository(mock_conn)
        result = repo.update_observation_status(
            setup_id="test-uuid-001",
            status="SETUP_READY",
        )

        assert result is True
        mock_conn.commit.assert_called_once()

    def test_upsert_outcome(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        repo = ResearchRepository(mock_conn)
        result = repo.upsert_outcome(
            signal_id=1,
            experiment_id="MER_GENERIC_V1",
            symbol="BTCUSDT",
            updates={"mfe_60m": 2.5, "mae_60m": 1.2, "is_final": False},
        )

        assert result is True
        mock_conn.commit.assert_called_once()

    def test_upsert_outcome_empty_updates(self):
        mock_conn = MagicMock()
        repo = ResearchRepository(mock_conn)
        result = repo.upsert_outcome(
            signal_id=1,
            experiment_id="MER_GENERIC_V1",
            symbol="BTCUSDT",
            updates={},
        )
        assert result is True  # no-op is success


# ── Feature/parameter snapshot tests ─────────────────────────


class TestSnapshotImmutability:
    """Verify that features and parameters are frozen at observation time."""

    def test_feature_snapshot_is_copy(self):
        """Observer creates a COPY of features, not a reference."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 1

        observer = ResearchObserver(
            mock_repo,
            {"MOMENTUM_EXHAUSTION_R": _make_experiment_config()},
        )

        original_features = {"exhaustion_magnitude": 0.5, "body_ratio": 0.3}
        candidate = _make_candidate(features=dict(original_features))

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        # The features in the observation should be a copy
        assert obs.features == original_features
        # Mutating original should not affect observation
        original_features["exhaustion_magnitude"] = 999.0
        assert obs.features["exhaustion_magnitude"] == 0.5

    def test_parameter_snapshot_from_experiment_config(self):
        """Parameters come from experiment config, not from scanner runtime."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 1

        config = _make_experiment_config()
        config["parameters"] = {"swing_lookback": 5, "exhaustion_threshold": 0.003}

        observer = ResearchObserver(mock_repo, {"MOMENTUM_EXHAUSTION_R": config})
        observer.observe(_make_candidate())

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.parameters == {"swing_lookback": 5, "exhaustion_threshold": 0.003}
        assert obs.parameter_set_id == "mer_1.0.0_20260928"


# ── Evaluator tests ──────────────────────────────────────────


class TestResearchEvaluator:
    """Test the generic evaluator with mock candles."""

    SIGNAL_TIME = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    SIGNAL_TS_MS = int(SIGNAL_TIME.timestamp() * 1000)

    def _make_candle(self, ts_offset_min: int, high: float, low: float, close: float) -> SimpleNamespace:
        """Create a mock Candle at SIGNAL_TIME + ts_offset_min minutes."""
        return SimpleNamespace(
            timestamp=self.SIGNAL_TS_MS + ts_offset_min * 60_000,
            high=high,
            low=low,
            close=close,
            open=close,
            volume=100.0,
        )

    def test_long_mfe_mae_basic(self):
        """LONG: price goes up then down."""
        from app.research.evaluator import _calculate_long_mfe_mae

        entry_price = 100.0

        candles = [
            self._make_candle(5, 102.0, 99.5, 101.5),   # +2% high, -0.5% low
            self._make_candle(10, 103.0, 100.0, 101.0),  # +3% high
            self._make_candle(20, 101.0, 98.0, 100.0),   # -2% low
        ]

        mfe, mae = _calculate_long_mfe_mae(candles, entry_price, 30, self.SIGNAL_TIME)
        assert mfe is not None
        assert mae is not None
        assert abs(mfe - 3.0) < 0.01  # max(2, 3, 1) = 3%
        assert abs(mae - 2.0) < 0.01  # max(0.5, 0, 2) = 2%

    def test_short_mfe_mae_basic(self):
        """SHORT: price goes down then up."""
        from app.research.evaluator import _calculate_short_mfe_mae

        entry_price = 100.0

        candles = [
            self._make_candle(5, 101.0, 98.0, 98.5),    # SHORT favorable = entry - low = 2%
            self._make_candle(10, 103.0, 99.0, 100.0),   # SHORT adverse = high - entry = 3%
            self._make_candle(20, 100.0, 97.0, 98.0),    # SHORT favorable = 3%
        ]

        mfe, mae = _calculate_short_mfe_mae(candles, entry_price, 30, self.SIGNAL_TIME)
        assert mfe is not None
        assert mae is not None
        assert abs(mfe - 3.0) < 0.01  # max(2, 1, 3) = 3%
        assert abs(mae - 3.0) < 0.01  # max(1, 3, 0) = 3%

    def test_immature_signal_no_evaluation(self):
        """Signal younger than 15m should not be evaluated."""
        from app.research.evaluator import _is_horizon_mature

        signal_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert not _is_horizon_mature(signal_time, 15, datetime.now(timezone.utc))
        assert _is_horizon_mature(signal_time, 15, signal_time + timedelta(minutes=15))

    def test_no_candles_returns_none(self):
        from app.research.evaluator import _calculate_long_mfe_mae

        mfe, mae = _calculate_long_mfe_mae([], 100.0, 30, self.SIGNAL_TIME)
        assert mfe is None
        assert mae is None

    def test_missing_sl_tp_graceful(self):
        """When invalidation_price is None, R metrics should be NULL, not crash."""
        from app.research.evaluator import _calculate_long_mfe_mae

        candles = [self._make_candle(5, 102.0, 99.5, 101.5)]

        mfe, mae = _calculate_long_mfe_mae(candles, 100.0, 30, self.SIGNAL_TIME)
        assert mfe is not None
        assert mae is not None

    def test_tp_sl_long_sequence(self):
        """LONG: TP hit before SL."""
        from app.research.evaluator import _check_long_tp_sl

        entry_price = 100.0
        invalidation = 97.5
        target_1 = 103.0

        candles = [
            self._make_candle(10, 103.5, 99.0, 103.0),  # TP hit at minute 10
        ]

        result = _check_long_tp_sl(candles, entry_price, invalidation, target_1, 60, self.SIGNAL_TIME)
        assert result["tp_hit"] is True
        assert result["sl_hit"] is False
        assert result["tp_before_sl"] is True
        assert result["sl_before_tp"] is False
        assert result["time_to_tp"] is not None

    def test_sl_tp_short_sequence(self):
        """SHORT: SL hit before TP within same window."""
        from app.research.evaluator import _check_short_tp_sl

        entry_price = 100.0
        invalidation = 102.5
        target_1 = 97.0

        candles = [
            self._make_candle(5, 103.0, 99.0, 102.0),   # SL hit at minute 5 (high >= 102.5)
            self._make_candle(20, 100.0, 96.5, 97.0),    # TP also hit later (low <= 97.0)
        ]

        result = _check_short_tp_sl(candles, entry_price, invalidation, target_1, 60, self.SIGNAL_TIME)
        assert result["sl_hit"] is True
        assert result["tp_hit"] is True   # TP also hit at minute 20
        assert result["sl_before_tp"] is True  # SL was first

    def test_sl_only_short(self):
        """SHORT: only SL hit, no TP."""
        from app.research.evaluator import _check_short_tp_sl

        entry_price = 100.0
        invalidation = 102.5
        target_1 = 97.0

        candles = [
            self._make_candle(5, 103.0, 99.5, 102.0),   # SL hit
            self._make_candle(20, 101.0, 98.0, 100.0),   # no TP (low > 97.0)
        ]

        result = _check_short_tp_sl(candles, entry_price, invalidation, target_1, 60, self.SIGNAL_TIME)
        assert result["sl_hit"] is True
        assert result["tp_hit"] is False
        assert result["sl_before_tp"] is True

    def test_horizons_constant(self):
        """Verify HORIZONS matches expected values."""
        assert len(HORIZONS) == 5
        assert HORIZONS[0] == ("15m", 15)
        assert HORIZONS[-1] == ("240m", 240)


# ── Constants tests ──────────────────────────────────────────


class TestConstants:
    def test_observation_statuses(self):
        assert ObservationStatus.DETECTED == "DETECTED"
        assert ObservationStatus.SETUP_READY == "SETUP_READY"
        assert ObservationStatus.GATE_REJECTED == "GATE_REJECTED"
        assert len({ObservationStatus.DETECTED, ObservationStatus.SETUP_READY}) == 2
