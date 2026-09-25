"""Tests for Phase 1.2: TREND_PULLBACK_V2 and BREAKOUT_RETEST research adapters.

Verifies that the generic research framework correctly captures candidates
from these scanners as research observations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.research.observer import ResearchObserver
from app.research.repository import ResearchRepository


# ── helpers ──────────────────────────────────────────────────


def _make_candidate(
    scanner_name: str = "TREND_PULLBACK_V2",
    scanner_version: str = "1.0.0",
    symbol: str = "ETHUSDT",
    direction: str = "LONG",
    reference_price: float = 3500.0,
    invalidation_price: float = 3450.0,
    target_1: float = 3525.0,
    target_2: float = None,
    score: float = 60.0,
    signal_candle_open_time: int = 1700000000000,
    features: dict | None = None,
    market_regime: str = "TREND_UP",
) -> SimpleNamespace:
    return SimpleNamespace(
        setup_id="test-uuid-phase12",
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
        entry_zone_high=reference_price * 1.002,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=target_2,
        score=score,
        market_regime=market_regime,
        reasons=(),
        features=features or {},
        state="SETUP_READY",
    )


def _tpv2_features() -> dict[str, Any]:
    return {
        "htf_context": True,
        "trend_alignment": True,
        "pullback_to_ema": True,
        "pullback_quality": 0.65,
        "rsi_cool": True,
        "rsi_confirmation": 0.8,
        "stop_distance_ok": True,
        "target_r": 0.50,
        "risk_r": 50.0,
        "recommended_expiry_policy": "BREAKEVEN",
    }


def _br_features() -> dict[str, Any]:
    return {
        "volume_ratio": 0.75,
        "retest_distance": 0.9,
        "rr_ratio": 0.6,
        "stop_distance_atr": 0.5,
        "regime_alignment": 1.0,
    }


def _tpv2_config() -> dict[str, Any]:
    from app.research.adapters.trend_pullback_v2 import EXPERIMENT_CONFIG
    return dict(EXPERIMENT_CONFIG)


def _br_config() -> dict[str, Any]:
    from app.research.adapters.breakout_retest import EXPERIMENT_CONFIG
    return dict(EXPERIMENT_CONFIG)


def _mer_config() -> dict[str, Any]:
    from app.research.adapters.momentum_exhaustion_r import EXPERIMENT_CONFIG
    return dict(EXPERIMENT_CONFIG)


# ── TREND_PULLBACK_V2 tests ─────────────────────────────────


class TestTrendPullbackV2Adapter:
    """Verify TREND_PULLBACK_V2 → research observation pipeline."""

    def test_tpv2_candidate_observed(self):
        """TREND_PULLBACK_V2 candidate creates a research observation."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 100

        observer = ResearchObserver(mock_repo, {"TREND_PULLBACK_V2": _tpv2_config()})
        candidate = _make_candidate(
            scanner_name="TREND_PULLBACK_V2",
            scanner_version="1.0.0",
            features=_tpv2_features(),
        )

        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 1
        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.experiment_id == "TPV2_GENERIC_V1"
        assert obs.scanner_name == "TREND_PULLBACK_V2"
        assert obs.symbol == "ETHUSDT"
        assert obs.direction == "LONG"
        assert obs.reference_price == 3500.0
        assert obs.invalidation_price == 3450.0
        assert obs.target_1 == 3525.0

    def test_tpv2_features_snapshot(self):
        """TREND_PULLBACK_V2 features are correctly captured."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 101

        observer = ResearchObserver(mock_repo, {"TREND_PULLBACK_V2": _tpv2_config()})
        candidate = _make_candidate(
            scanner_name="TREND_PULLBACK_V2",
            features=_tpv2_features(),
        )

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.features["pullback_quality"] == 0.65
        assert obs.features["rsi_confirmation"] == 0.8
        assert obs.features["target_r"] == 0.50
        assert obs.features["risk_r"] == 50.0
        assert obs.features["recommended_expiry_policy"] == "BREAKEVEN"

    def test_tpv2_parameters_snapshot(self):
        """TREND_PULLBACK_V2 parameters come from experiment config."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 102

        observer = ResearchObserver(mock_repo, {"TREND_PULLBACK_V2": _tpv2_config()})
        candidate = _make_candidate(scanner_name="TREND_PULLBACK_V2")

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.parameter_set_id == "tpv2_1.0.0_20260928"
        assert obs.parameters["pullback_tolerance"] == 0.012
        assert obs.parameters["target_r"] == 0.50


# ── BREAKOUT_RETEST tests ────────────────────────────────────


class TestBreakoutRetestAdapter:
    """Verify BREAKOUT_RETEST → research observation pipeline."""

    def test_br_long_candidate_observed(self):
        """BREAKOUT_RETEST LONG candidate creates a research observation."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 200

        observer = ResearchObserver(mock_repo, {"BREAKOUT_RETEST": _br_config()})
        candidate = _make_candidate(
            scanner_name="BREAKOUT_RETEST",
            scanner_version="2.0.0",
            direction="LONG",
            features=_br_features(),
        )

        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 1
        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.experiment_id == "BR_GENERIC_V1"
        assert obs.scanner_name == "BREAKOUT_RETEST"
        assert obs.direction == "LONG"

    def test_br_short_candidate_observed(self):
        """BREAKOUT_RETEST SHORT candidate creates a research observation."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 201

        observer = ResearchObserver(mock_repo, {"BREAKOUT_RETEST": _br_config()})
        candidate = _make_candidate(
            scanner_name="BREAKOUT_RETEST",
            scanner_version="2.0.0",
            direction="SHORT",
            reference_price=42000.0,
            invalidation_price=42500.0,
            target_1=41000.0,
            features=_br_features(),
        )

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.direction == "SHORT"
        assert obs.reference_price == 42000.0

    def test_br_features_snapshot(self):
        """BREAKOUT_RETEST features are correctly captured."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 202

        observer = ResearchObserver(mock_repo, {"BREAKOUT_RETEST": _br_config()})
        candidate = _make_candidate(
            scanner_name="BREAKOUT_RETEST",
            features=_br_features(),
        )

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.features["volume_ratio"] == 0.75
        assert obs.features["retest_distance"] == 0.9
        assert obs.features["rr_ratio"] == 0.6
        assert obs.features["stop_distance_atr"] == 0.5
        assert obs.features["regime_alignment"] == 1.0

    def test_br_parameters_snapshot(self):
        """BREAKOUT_RETEST parameters come from experiment config."""
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 203

        observer = ResearchObserver(mock_repo, {"BREAKOUT_RETEST": _br_config()})
        candidate = _make_candidate(scanner_name="BREAKOUT_RETEST")

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.parameter_set_id == "br_2.0.0_20260928"
        assert obs.parameters["swing_lookback"] == 5
        assert obs.parameters["breakout_margin"] == 0.001
        assert obs.parameters["retest_margin"] == 0.003


# ── Multi-scanner observer tests ─────────────────────────────


class TestMultiScannerObserver:
    """Verify observer handles multiple scanners simultaneously."""

    def _make_multi_observer(self) -> tuple[ResearchObserver, MagicMock]:
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 999

        all_experiments = {
            "TREND_PULLBACK_V2": _tpv2_config(),
            "BREAKOUT_RETEST": _br_config(),
            "MOMENTUM_EXHAUSTION_R": _mer_config(),
        }
        observer = ResearchObserver(mock_repo, all_experiments)
        return observer, mock_repo

    def test_unregistered_scanner_ignored(self):
        """Scanner not in experiments dict is silently ignored."""
        observer, mock_repo = self._make_multi_observer()

        candidate = _make_candidate(
            scanner_name="VOLATILITY_COMPRESSION",
            features={"squeeze_detected": True},
        )
        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 0

    def test_duplicate_candidate_suppressed(self):
        """Same (experiment, symbol, direction, candle) = duplicate → suppressed."""
        observer, mock_repo = self._make_multi_observer()
        mock_repo.save_observation.return_value = None  # ON CONFLICT → None

        for _ in range(3):
            observer.observe(_make_candidate(
                scanner_name="TREND_PULLBACK_V2",
                signal_candle_open_time=1700000000000,
                features=_tpv2_features(),
            ))

        assert mock_repo.save_observation.call_count == 3
        assert observer.stats["duplicates"] == 3
        assert observer.stats.get("inserted", 0) == 0

    def test_different_scanners_independent(self):
        """Different scanners create independent observations."""
        observer, mock_repo = self._make_multi_observer()

        call_count = 0
        def side_effect(obs):
            nonlocal call_count
            call_count += 1
            return call_count
        mock_repo.save_observation.side_effect = side_effect

        observer.observe(_make_candidate(
            scanner_name="TREND_PULLBACK_V2",
            features=_tpv2_features(),
        ))
        observer.observe(_make_candidate(
            scanner_name="BREAKOUT_RETEST",
            features=_br_features(),
        ))
        observer.observe(_make_candidate(
            scanner_name="MOMENTUM_EXHAUSTION_R",
            features={"exhaustion_magnitude": 0.5},
        ))

        assert mock_repo.save_observation.call_count == 3
        assert observer.stats["inserted"] == 3

        # Verify different experiment_ids
        calls = mock_repo.save_observation.call_args_list
        experiment_ids = [call[0][0].experiment_id for call in calls]
        assert "TPV2_GENERIC_V1" in experiment_ids
        assert "BR_GENERIC_V1" in experiment_ids
        assert "MER_GENERIC_V1" in experiment_ids

    def test_observer_failure_does_not_affect_other_scanners(self):
        """If one observe() fails, subsequent ones still work."""
        observer, mock_repo = self._make_multi_observer()

        call_count = 0
        def side_effect(obs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("DB timeout")
            return call_count
        mock_repo.save_observation.side_effect = side_effect

        # First call fails
        observer.observe(_make_candidate(
            scanner_name="TREND_PULLBACK_V2",
            features=_tpv2_features(),
        ))
        # Second call succeeds
        observer.observe(_make_candidate(
            scanner_name="BREAKOUT_RETEST",
            features=_br_features(),
        ))

        assert observer.stats["errors"] == 1
        assert observer.stats["inserted"] == 1


# ── Adapter config tests ─────────────────────────────────────


class TestAdapterConfigs:
    """Verify adapter configs are correct and consistent."""

    def test_tpv2_config_fields(self):
        from app.research.adapters.trend_pullback_v2 import EXPERIMENT_CONFIG
        assert EXPERIMENT_CONFIG["experiment_id"] == "TPV2_GENERIC_V1"
        assert EXPERIMENT_CONFIG["scanner_name"] == "TREND_PULLBACK_V2"
        assert EXPERIMENT_CONFIG["scanner_version"] == "1.0.0"
        assert EXPERIMENT_CONFIG["parameter_set_id"] == "tpv2_1.0.0_20260928"
        assert "pullback_tolerance" in EXPERIMENT_CONFIG["parameters"]
        assert EXPERIMENT_CONFIG["parameters"]["pullback_tolerance"] == 0.012

    def test_br_config_fields(self):
        from app.research.adapters.breakout_retest import EXPERIMENT_CONFIG
        assert EXPERIMENT_CONFIG["experiment_id"] == "BR_GENERIC_V1"
        assert EXPERIMENT_CONFIG["scanner_name"] == "BREAKOUT_RETEST"
        assert EXPERIMENT_CONFIG["scanner_version"] == "2.0.0"
        assert EXPERIMENT_CONFIG["parameter_set_id"] == "br_2.0.0_20260928"
        assert "swing_lookback" in EXPERIMENT_CONFIG["parameters"]
        assert EXPERIMENT_CONFIG["parameters"]["swing_lookback"] == 5

    def test_mer_config_still_works(self):
        """MOMENTUM_EXHAUSTION_R adapter unchanged."""
        from app.research.adapters.momentum_exhaustion_r import EXPERIMENT_CONFIG
        assert EXPERIMENT_CONFIG["experiment_id"] == "MER_GENERIC_V1"
        assert EXPERIMENT_CONFIG["scanner_name"] == "MOMENTUM_EXHAUSTION_R"
