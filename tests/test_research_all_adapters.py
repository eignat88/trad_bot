"""Parameterized tests for ALL generic research adapters.

Verifies that each adapter:
- has correct experiment_id, scanner_name, parameter_set_id
- produces valid EXPERIMENT_CONFIG dict
- observer correctly captures candidate → observation
- duplicate observation suppressed
- LONG and SHORT candidates captured where applicable
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.research.observer import ResearchObserver
from app.research.repository import ResearchRepository


# ── Adapter registry ─────────────────────────────────────────

ADAPTERS = [
    # (module_path, experiment_id, scanner_name, directions, has_raw_features)
    ("app.research.adapters.momentum_exhaustion_r", "MER_GENERIC_V1", "MOMENTUM_EXHAUSTION_R", ["LONG", "SHORT"], False),
    ("app.research.adapters.trend_pullback_v2", "TPV2_GENERIC_V1", "TREND_PULLBACK_V2", ["LONG"], False),
    ("app.research.adapters.breakout_retest", "BR_GENERIC_V1", "BREAKOUT_RETEST", ["LONG", "SHORT"], False),
    ("app.research.adapters.volatility_compression", "VC_GENERIC_V1", "VOLATILITY_COMPRESSION", ["LONG", "SHORT"], False),
    ("app.research.adapters.momentum_exhaustion", "ME_GENERIC_V1", "MOMENTUM_EXHAUSTION", ["LONG", "SHORT"], False),
    ("app.research.adapters.support_resistance", "SRR_GENERIC_V1", "SUPPORT_RESISTANCE_REACTION", ["LONG", "SHORT"], True),
    ("app.research.adapters.liquidity_reversal", "LR_GENERIC_V1", "LIQUIDITY_REVERSAL", ["LONG", "SHORT"], False),
    ("app.research.adapters.liquidity_sweep_choch", "LSCO_GENERIC_V1", "LIQUIDITY_SWEEP_CHOCH_OB", ["LONG", "SHORT"], False),
    ("app.research.adapters.trend_pullback_v3", "TPV3_GENERIC_V1", "TREND_PULLBACK_V3", ["LONG"], False),
    ("app.research.adapters.momentum_exhaustion_reverse_long_v2", "ME_RL_V2_GENERIC_V1", "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", ["LONG"], False),
    ("app.research.adapters.momentum_exhaustion_reverse_long_v1", "ME_RL_V1_GENERIC_V1", "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", ["LONG"], False),
    ("app.research.adapters.fvg_reaction_long", "FVG_GENERIC_V1", "FVG_REACTION_LONG_LOCAL_STRUCT_V1", ["LONG"], False),
]


def _load_config(module_path: str) -> dict[str, Any]:
    import importlib
    mod = importlib.import_module(module_path)
    return dict(mod.EXPERIMENT_CONFIG)


def _make_candidate(
    scanner_name: str,
    scanner_version: str,
    direction: str,
    features: dict | None = None,
    candle_ts: int = 1700000000000,
) -> SimpleNamespace:
    return SimpleNamespace(
        setup_id=f"test-{scanner_name}-{direction}-{candle_ts}",
        scanner_name=scanner_name,
        scanner_version=scanner_version,
        symbol="BTCUSDT",
        direction=direction,
        htf_timeframe="1h",
        setup_timeframe="15m",
        entry_timeframe="5m",
        detected_at=datetime.now(timezone.utc),
        setup_started_at=datetime.now(timezone.utc),
        signal_candle_open_time=candle_ts,
        reference_price=50000.0,
        entry_zone_low=49900.0,
        entry_zone_high=50100.0,
        invalidation_price=48000.0 if direction == "LONG" else 52000.0,
        target_1=53000.0 if direction == "LONG" else 47000.0,
        target_2=None,
        score=55.0,
        market_regime="TREND_UP",
        reasons=(),
        features=features or {"test_feature": 0.5},
        state=SimpleNamespace(value="SETUP_READY"),
    )


# ── Config validation tests ──────────────────────────────────


class TestAdapterConfigs:
    """Verify every adapter config has required fields."""

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_config_fields(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        assert config["experiment_id"] == exp_id
        assert config["scanner_name"] == scanner_name
        assert "scanner_version" in config
        assert "parameter_set_id" in config
        assert "parameters" in config
        assert isinstance(config["parameters"], dict)
        assert len(config["parameters"]) > 0

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_experiment_id_matches_naming(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        assert config["experiment_id"].endswith("_GENERIC_V1")


# ── Observer capture tests ───────────────────────────────────


class TestObserverCapture:
    """Verify observer captures candidates from all scanners."""

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_long_candidate_captured(self, module_path, exp_id, scanner_name, directions, has_raw):
        if "LONG" not in directions:
            pytest.skip(f"{scanner_name} does not support LONG")

        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 1

        observer = ResearchObserver(mock_repo, {scanner_name: config})
        candidate = _make_candidate(
            scanner_name=scanner_name,
            scanner_version=config["scanner_version"],
            direction="LONG",
        )

        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 1
        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.experiment_id == exp_id
        assert obs.scanner_name == scanner_name
        assert obs.direction == "LONG"

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_short_candidate_captured(self, module_path, exp_id, scanner_name, directions, has_raw):
        if "SHORT" not in directions:
            pytest.skip(f"{scanner_name} does not support SHORT")

        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 2

        observer = ResearchObserver(mock_repo, {scanner_name: config})
        candidate = _make_candidate(
            scanner_name=scanner_name,
            scanner_version=config["scanner_version"],
            direction="SHORT",
        )

        observer.observe(candidate)

        assert mock_repo.save_observation.call_count == 1
        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.direction == "SHORT"


# ── Duplicate suppression ────────────────────────────────────


class TestDuplicateSuppression:
    """Verify duplicate observations are suppressed."""

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_duplicate_suppressed(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = None  # duplicate

        observer = ResearchObserver(mock_repo, {scanner_name: config})

        for _ in range(5):
            observer.observe(_make_candidate(
                scanner_name=scanner_name,
                scanner_version=config["scanner_version"],
                direction="LONG",
                candle_ts=1700000000000,
            ))

        assert mock_repo.save_observation.call_count == 5
        assert observer.stats["duplicates"] == 5


# ── Fail-open ────────────────────────────────────────────────


class TestFailOpen:
    """Verify DB failure doesn't affect scanner."""

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_db_failure_no_raise(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.side_effect = ConnectionError("timeout")

        observer = ResearchObserver(mock_repo, {scanner_name: config})

        # Must not raise
        observer.observe(_make_candidate(
            scanner_name=scanner_name,
            scanner_version=config["scanner_version"],
            direction="LONG",
        ))

        assert observer.stats["errors"] == 1


# ── Feature snapshot ─────────────────────────────────────────


class TestFeatureSnapshot:
    """Verify features are captured as immutable snapshot."""

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_features_captured(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 3

        observer = ResearchObserver(mock_repo, {scanner_name: config})

        test_features = {"quality_a": 0.8, "quality_b": 0.3, "flag_x": True}
        candidate = _make_candidate(
            scanner_name=scanner_name,
            scanner_version=config["scanner_version"],
            direction="LONG",
            features=test_features,
        )

        observer.observe(candidate)

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.features["quality_a"] == 0.8
        assert obs.features["quality_b"] == 0.3
        assert obs.features["flag_x"] is True

    @pytest.mark.parametrize("module_path,exp_id,scanner_name,directions,has_raw", ADAPTERS,
                             ids=[a[2] for a in ADAPTERS])
    def test_parameters_captured(self, module_path, exp_id, scanner_name, directions, has_raw):
        config = _load_config(module_path)
        mock_repo = MagicMock(spec=ResearchRepository)
        mock_repo.save_observation.return_value = 4

        observer = ResearchObserver(mock_repo, {scanner_name: config})
        observer.observe(_make_candidate(
            scanner_name=scanner_name,
            scanner_version=config["scanner_version"],
            direction="LONG",
        ))

        obs = mock_repo.save_observation.call_args[0][0]
        assert obs.parameters == config["parameters"]
        assert obs.parameter_set_id == config["parameter_set_id"]


# ── Migration seeds ──────────────────────────────────────────


class TestMigrationSeeds:
    """Verify all experiments are seeded in migration 049."""

    def test_all_experiments_in_migration(self):
        from pathlib import Path
        migration = Path(__file__).parent.parent / "sql" / "migrations" / "049_generic_research_framework.sql"
        content = migration.read_text(encoding="utf-8")

        expected_ids = [
            "MER_GENERIC_V1",
            "TPV2_GENERIC_V1",
            "BR_GENERIC_V1",
            "VC_GENERIC_V1",
            "ME_GENERIC_V1",
            "SRR_GENERIC_V1",
            "LR_GENERIC_V1",
            "LSCO_GENERIC_V1",
            "TPV3_GENERIC_V1",
            "ME_RL_V2_GENERIC_V1",
            "ME_RL_V1_GENERIC_V1",
            "FVG_GENERIC_V1",
        ]

        for exp_id in expected_ids:
            assert f"'{exp_id}'" in content, f"Missing seed for {exp_id}"

    def test_all_idempotent(self):
        from pathlib import Path
        migration = Path(__file__).parent.parent / "sql" / "migrations" / "049_generic_research_framework.sql"
        content = migration.read_text(encoding="utf-8")

        # Every INSERT must have ON CONFLICT DO NOTHING
        lines = content.split("\n")
        in_insert = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("INSERT INTO research.research_experiment"):
                in_insert = True
            if in_insert and "ON CONFLICT" in stripped:
                in_insert = False
            if in_insert and stripped.startswith("--"):
                # comment between INSERT and ON CONFLICT is fine
                continue
        # If we end still in_insert, that's a problem
        assert not in_insert, "INSERT without ON CONFLICT DO NOTHING found"
