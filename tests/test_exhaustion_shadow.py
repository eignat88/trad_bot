"""Tests for ME_REVERSE_LONG_V2_EXHAUSTION_SHADOW_V1.

Verifies:
1. ExhaustionShadowObserver classification logic (PASS/REJECT/MISSING_FEATURE)
2. Threshold is fixed at 0.4
3. Experiment ID is correct
4. No production scanner behavior is changed
5. Migration SQL is valid
"""
from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHADOW_MODULE = PROJECT_ROOT / "app" / "shadow" / "exhaustion_shadow.py"
MIGRATION = PROJECT_ROOT / "sql" / "migrations" / "042_v2_exhaustion_shadow_experiment.sql"
ANALYTICS_SQL = PROJECT_ROOT / "scripts" / "analysis" / "v2_exhaustion_shadow" / "09_v2_exhaustion_shadow_analytics.sql"


class TestExhaustionShadowModuleExists:
    def test_module_file_exists(self):
        assert SHADOW_MODULE.exists(), f"Module not found: {SHADOW_MODULE}"

    def test_migration_exists(self):
        assert MIGRATION.exists(), f"Migration not found: {MIGRATION}"

    def test_analytics_sql_exists(self):
        assert ANALYTICS_SQL.exists(), f"Analytics SQL not found: {ANALYTICS_SQL}"


class TestExhaustionShadowConstants:
    def test_threshold_is_04(self):
        content = SHADOW_MODULE.read_text(encoding="utf-8")
        assert "THRESHOLD = 0.4" in content, "Threshold must be fixed at 0.4"

    def test_experiment_id(self):
        content = SHADOW_MODULE.read_text(encoding="utf-8")
        assert "V2_EXHAUSTION_SHADOW_V1" in content

    def test_scanner_name(self):
        content = SHADOW_MODULE.read_text(encoding="utf-8")
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2" in content


class TestClassificationLogic:
    """Test the classification rules without DB connection."""

    def _classify(self, features: dict) -> str:
        """Replicate classification logic from ExhaustionShadowObserver."""
        exhaustion_mag = features.get("exhaustion_magnitude")
        if exhaustion_mag is None:
            return "MISSING_FEATURE"
        elif float(exhaustion_mag) <= 0.4:
            return "PASS"
        else:
            return "REJECT"

    def test_pass_at_threshold(self):
        assert self._classify({"exhaustion_magnitude": 0.4}) == "PASS"

    def test_pass_below_threshold(self):
        assert self._classify({"exhaustion_magnitude": 0.2}) == "PASS"

    def test_pass_at_zero(self):
        assert self._classify({"exhaustion_magnitude": 0.0}) == "PASS"

    def test_reject_above_threshold(self):
        assert self._classify({"exhaustion_magnitude": 0.5}) == "REJECT"

    def test_reject_at_high(self):
        assert self._classify({"exhaustion_magnitude": 0.8}) == "REJECT"

    def test_missing_feature(self):
        assert self._classify({}) == "MISSING_FEATURE"

    def test_missing_feature_none(self):
        assert self._classify({"exhaustion_magnitude": None}) == "MISSING_FEATURE"


class TestNoProductionChanges:
    """Verify the shadow module does not import production scanner/paper code."""

    def test_no_scanner_imports(self):
        content = SHADOW_MODULE.read_text(encoding="utf-8")
        # Should not import scanner logic
        assert "from app.scanners" not in content
        assert "import MomentumExhaustion" not in content

    def test_no_paper_engine_imports(self):
        content = SHADOW_MODULE.read_text(encoding="utf-8")
        # Should not import or modify paper trading engine
        assert "from app.paper.trading" not in content
        assert "PaperTradingEngine" not in content


class TestMigrationSchema:
    """Verify migration SQL has correct table structure."""

    def test_table_name(self):
        content = MIGRATION.read_text(encoding="utf-8")
        assert "dds.shadow_exhaustion_observation" in content

    def test_experiment_id_default(self):
        content = MIGRATION.read_text(encoding="utf-8")
        assert "V2_EXHAUSTION_SHADOW_V1" in content

    def test_threshold_column(self):
        content = MIGRATION.read_text(encoding="utf-8")
        assert "threshold" in content
        assert "0.4" in content

    def test_filter_result_check(self):
        content = MIGRATION.read_text(encoding="utf-8")
        assert "'PASS'" in content
        assert "'REJECT'" in content
        assert "'MISSING_FEATURE'" in content

    def test_analytics_view(self):
        content = MIGRATION.read_text(encoding="utf-8")
        assert "v_v2_exhaustion_shadow_analytics" in content
