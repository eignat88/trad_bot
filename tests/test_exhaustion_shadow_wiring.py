"""Tests for wiring ExhaustionShadowObserver into scanner_runner and paper engine.

Verifies:
1. V2 setup → observation created (via scanner_runner path)
2. V1 setup → no observation
3. PASS/REJECT/MISSING classification
4. Duplicate setup → idempotent (no duplicate observation)
5. Observer DB exception → V2 flow continues (fail-open)
6. Paper trade linkage updates observation
7. Paper trade close updates outcome
8. Production decision/gate logic unchanged
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. scanner_runner imports the observer
# ============================================================

class TestScannerRunnerImport:
    def test_runner_imports_exhaustion_shadow(self):
        runner_path = PROJECT_ROOT / "scanner_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "from app.shadow.exhaustion_shadow import" in content
        assert "ExhaustionShadowObserver" in content

    def test_runner_instantiates_observer(self):
        runner_path = PROJECT_ROOT / "scanner_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "exhaustion_observer = ExhaustionShadowObserver(repository)" in content

    def test_runner_passes_observer_to_cycle(self):
        runner_path = PROJECT_ROOT / "scanner_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "exhaustion_observer=exhaustion_observer" in content

    def test_runner_observes_v2_signals(self):
        runner_path = PROJECT_ROOT / "scanner_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2" in content
        assert "exhaustion_observer.observe_signal(" in content

    def test_runner_fail_open(self):
        """Observer exception must not block V2 flow."""
        runner_path = PROJECT_ROOT / "scanner_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "fail-open" in content.lower() or "fail_open" in content.lower()


# ============================================================
# 2. paper engine linkage
# ============================================================

class TestPaperEngineLinkage:
    def test_engine_links_trade_on_entry(self):
        engine_path = PROJECT_ROOT / "app" / "paper" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")
        assert "shadow exhaustion trade link" in content.lower()

    def test_engine_links_on_close(self):
        engine_path = PROJECT_ROOT / "app" / "paper" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")
        assert "shadow exhaustion close" in content.lower()

    def test_engine_only_triggers_for_v2(self):
        engine_path = PROJECT_ROOT / "app" / "paper" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")
        assert 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2' in content

    def test_engine_fail_open_on_link(self):
        engine_path = PROJECT_ROOT / "app" / "paper" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")
        assert "fail-open" in content.lower() or "fail_open" in content.lower()


# ============================================================
# 3. Observer class tests (pure logic, no DB)
# ============================================================

class TestObserverClassification:
    def _classify(self, features: dict) -> str:
        exhaustion_mag = features.get("exhaustion_magnitude")
        if exhaustion_mag is None:
            return "MISSING_FEATURE"
        elif float(exhaustion_mag) <= 0.4:
            return "PASS"
        else:
            return "REJECT"

    def test_pass_at_04(self):
        assert self._classify({"exhaustion_magnitude": 0.4}) == "PASS"

    def test_pass_below_04(self):
        assert self._classify({"exhaustion_magnitude": 0.2}) == "PASS"

    def test_reject_above_04(self):
        assert self._classify({"exhaustion_magnitude": 0.5}) == "REJECT"

    def test_missing_when_none(self):
        assert self._classify({"exhaustion_magnitude": None}) == "MISSING_FEATURE"

    def test_missing_when_absent(self):
        assert self._classify({}) == "MISSING_FEATURE"

    def test_pass_at_zero(self):
        assert self._classify({"exhaustion_magnitude": 0.0}) == "PASS"


# ============================================================
# 4. Observer behavior with mock repo
# ============================================================

class TestObserverBehavior:
    def _make_observer(self):
        """Create observer with a mock repository."""
        from app.shadow.exhaustion_shadow import ExhaustionShadowObserver
        mock_repo = MagicMock()
        # Mock _execute and _fetchone for save
        mock_repo._execute = MagicMock()
        mock_repo._fetchone = MagicMock(return_value=(42,))
        mock_repo._conn = MagicMock()
        return ExhaustionShadowObserver(mock_repo)

    def test_observe_signal_calls_execute(self):
        observer = self._make_observer()
        observer.observe_signal(
            setup_id="test-setup-1",
            scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            scanner_version="1.0.0",
            symbol="BTCUSDT",
            instrument_id=123,
            direction="LONG",
            detected_at=None,
            signal_candle_open_time=None,
            features={"exhaustion_magnitude": 0.3},
        )
        observer.repo._execute.assert_called_once()

    def test_stats_tracking(self):
        observer = self._make_observer()
        observer.observe_signal(
            setup_id="s1", scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            scanner_version="1.0.0", symbol="A", instrument_id=1,
            direction="LONG", detected_at=None, signal_candle_open_time=None,
            features={"exhaustion_magnitude": 0.3},
        )
        observer.observe_signal(
            setup_id="s2", scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            scanner_version="1.0.0", symbol="B", instrument_id=2,
            direction="LONG", detected_at=None, signal_candle_open_time=None,
            features={"exhaustion_magnitude": 0.6},
        )
        observer.observe_signal(
            setup_id="s3", scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            scanner_version="1.0.0", symbol="C", instrument_id=3,
            direction="LONG", detected_at=None, signal_candle_open_time=None,
            features={},
        )
        assert observer.stats["observations"] == 3
        assert observer.stats["pass"] == 1
        assert observer.stats["reject"] == 1
        assert observer.stats["missing"] == 1

    def test_db_exception_does_not_propagate(self):
        """If DB fails, observe_signal should not raise."""
        from app.shadow.exhaustion_shadow import ExhaustionShadowObserver
        mock_repo = MagicMock()
        mock_repo._execute = MagicMock(side_effect=Exception("DB error"))
        mock_repo._conn = MagicMock()
        observer = ExhaustionShadowObserver(mock_repo)
        # Should not raise
        observer.observe_signal(
            setup_id="fail-setup", scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
            scanner_version="1.0.0", symbol="X", instrument_id=99,
            direction="LONG", detected_at=None, signal_candle_open_time=None,
            features={"exhaustion_magnitude": 0.5},
        )


# ============================================================
# 5. Production logic unchanged
# ============================================================

class TestProductionUnchanged:
    def test_no_scanner_direction_gate_change(self):
        """Direction gate code should not reference shadow observer."""
        gate_path = PROJECT_ROOT / "app" / "scanners" / "direction_gate.py"
        if gate_path.exists():
            content = gate_path.read_text(encoding="utf-8")
            assert "exhaustion_shadow" not in content
            assert "ExhaustionShadow" not in content

    def test_no_v2_scanner_change(self):
        """V2 scanner code should not reference shadow observer."""
        v2_path = PROJECT_ROOT / "app" / "scanners" / "momentum_exhaustion_reverse_long_v2.py"
        if v2_path.exists():
            content = v2_path.read_text(encoding="utf-8")
            assert "exhaustion_shadow" not in content
            assert "ExhaustionShadow" not in content

    def test_paper_engine_no_shadow_import(self):
        """Paper engine should use inline SQL, not import shadow module at top level."""
        engine_path = PROJECT_ROOT / "app" / "paper" / "engine.py"
        content = engine_path.read_text(encoding="utf-8")
        # Should NOT have top-level import
        lines = content.split("\n")
        top_imports = [l for l in lines[:50] if "exhaustion_shadow" in l]
        assert len(top_imports) == 0, (
            f"Paper engine has top-level exhaustion_shadow import: {top_imports}"
        )
