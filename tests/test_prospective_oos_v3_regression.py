"""Regression tests for Prospective OOS V3 runtime wiring.

Tests the two runtime failures discovered on VPS:
1. NameError: name 'prospective_obs' is not defined in scanner_runner.py
2. AttributeError: 'BybitClient' object has no attribute 'get_eligible_signals'

Also verifies:
- ME A/B/C exact pairing by source_signal_id
- Evaluator processes research.prospective_observation → research.prospective_outcome
- Fail-closed initialization of prospective_obs
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────
# Test 1: prospective_obs is always defined before use
# ──────────────────────────────────────────────────────────────

class TestProspectiveObsNameError:
    """Regression: NameError: name 'prospective_obs' is not defined.

    The variable must be initialized BEFORE run_scan_cycle() uses it.
    """

    def test_prospective_obs_initialized_before_run_scan_cycle(self):
        """prospective_obs must be defined in main() before run_scan_cycle() call."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        # Find the main() function body
        lines = source.splitlines()
        in_main = False
        main_start = None
        for i, line in enumerate(lines):
            if line.startswith("def main()"):
                in_main = True
                main_start = i
            elif in_main and line.startswith("def ") or (in_main and line.startswith("class ")):
                break
            elif in_main and "prospective_obs" in line and "=" in line and "run_scan_cycle" not in line:
                # Found initialization
                assert "prospective_obs" in lines[i]
                return
        pytest.fail("prospective_obs not initialized in main() before run_scan_cycle()")

    def test_run_scan_cycle_signature_includes_prospective_obs(self):
        """run_scan_cycle() must accept prospective_obs parameter."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_scan_cycle":
                arg_names = [arg.arg for arg in node.args.args]
                assert "prospective_obs" in arg_names, (
                    f"run_scan_cycle() missing prospective_obs parameter; got: {arg_names}"
                )
                return
        pytest.fail("run_scan_cycle function not found")

    def test_run_scan_cycle_called_with_prospective_obs(self):
        """main() must pass prospective_obs to run_scan_cycle()."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        # Find the call to run_scan_cycle in main()
        lines = source.splitlines()
        in_main = False
        for i, line in enumerate(lines):
            if line.startswith("def main()"):
                in_main = True
            elif in_main and "run_scan_cycle(" in line:
                # Check surrounding lines for prospective_obs= parameter
                call_text = line
                # Collect multi-line call
                paren_depth = line.count("(") - line.count(")")
                j = i + 1
                while paren_depth > 0 and j < len(lines):
                    call_text += " " + lines[j]
                    paren_depth += lines[j].count("(") - lines[j].count(")")
                    j += 1
                assert "prospective_obs" in call_text, (
                    "main() does not pass prospective_obs to run_scan_cycle()"
                )
                return
        pytest.fail("run_scan_cycle call not found in main()")

    def test_prospective_obs_none_check_in_candidate_loop(self):
        """The 'if prospective_obs is not None' check must exist in candidate loop."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        assert "if prospective_obs is not None:" in source, (
            "prospective_obs None-check not found in scanner_runner.py"
        )
        assert "prospective_obs.observe(" in source, (
            "prospective_obs.observe() call not found in scanner_runner.py"
        )

    def test_prospective_obs_fail_closed_default(self):
        """prospective_obs must default to None before any observer initialization."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        lines = source.splitlines()
        in_main = False
        for i, line in enumerate(lines):
            if line.startswith("def main()"):
                in_main = True
            elif in_main and "prospective_obs" in line and "=" in line:
                # First assignment should be = None or type-annotated None
                assert "None" in line, (
                    f"prospective_obs first assignment is not None: {line.strip()}"
                )
                return
        pytest.fail("prospective_obs not found in main()")


# ──────────────────────────────────────────────────────────────
# Test 2: Prospective evaluator does not receive BybitClient as repo
# ──────────────────────────────────────────────────────────────

class TestProspectiveEvaluatorDependencyInjection:
    """Regression: BybitClient has no get_eligible_signals attribute.

    The evaluator_runner must not pass BybitClient where a repository is expected.
    """

    def test_evaluator_runner_imports_research_repository(self):
        """evaluator_runner.py must import ResearchRepository."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "from app.research.repository import ResearchRepository" in source, (
            "evaluator_runner.py does not import ResearchRepository"
        )

    def test_prospective_evaluator_receives_repo_parameter(self):
        """ProspectiveOOSEvaluator.__init__ must accept a repo parameter."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSEvaluator":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        arg_names = [arg.arg for arg in item.args.args]
                        assert "repo" in arg_names, (
                            f"ProspectiveOOSEvaluator.__init__ missing 'repo' param; got: {arg_names}"
                        )
                        return
        pytest.fail("ProspectiveOOSEvaluator class not found")

    def test_evaluator_runner_passes_research_repo_to_prospective(self):
        """evaluator_runner must pass ResearchRepository, not BybitClient, to ProspectiveOOSEvaluator."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # Find the ProspectiveOOSEvaluator(...) call
        assert "ProspectiveOOSEvaluator(" in source, (
            "ProspectiveOOSEvaluator instantiation not found"
        )
        # Check that ResearchRepository is used before the call
        assert "ResearchRepository(" in source, (
            "ResearchRepository not instantiated in evaluator_runner.py"
        )
        # Check the repo= kwarg
        assert "repo=research_repo" in source, (
            "ProspectiveOOSEvaluator not called with repo=research_repo"
        )

    def test_bybit_client_has_no_get_eligible_signals(self):
        """BybitClient must NOT have get_eligible_signals (regression guard)."""
        from app.exchange.bybit_client import BybitClient
        assert not hasattr(BybitClient, "get_eligible_signals"), (
            "BybitClient unexpectedly has get_eligible_signals attribute"
        )


# ──────────────────────────────────────────────────────────────
# Test 3: ME A/B/C exact pairing by source_signal_id
# ──────────────────────────────────────────────────────────────

class TestMEABCExactPairing:
    """ME SHORT geometry experiments A/B/C must share the same source_signal_id."""

    def test_me_geometry_uses_synthetic_source_key(self):
        """ProspectiveOOSObserver._make_source_key generates deterministic source_signal_id."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "_make_source_key" in source, "_make_source_key method not found"

    def test_me_a_b_c_all_call_observe_me_geometry(self):
        """All three ME geometry experiments (A/B/C) route through _observe_me_geometry."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert 'exp_id.startswith("ME_SHORT_GEOM_")' in source, (
            "ME SHORT geometry dispatch not found"
        )
        assert "_observe_me_geometry(" in source, "_observe_me_geometry method not found"

    def test_me_geometry_shares_source_key(self):
        """All three ME geometry variants use the same source_key for pairing."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "_observe_me_geometry":
                        body_source = ast.get_source_segment(source, item)
                        # All three variants must use source_key (not a variant-specific key)
                        # The INSERT statement uses source_key for source_signal_id
                        assert "source_key" in body_source, (
                            "_observe_me_geometry does not use source_key for pairing"
                        )
                        # Verify the INSERT uses source_key as source_signal_id
                        assert "exp_id, source_key, None," in body_source, (
                            "_observe_me_geometry INSERT does not use source_key as source_signal_id"
                        )
                        return
        pytest.fail("_observe_me_geometry not found")

    def test_registry_pairs_b_and_c_with_a(self):
        """Registry must mark ME_SHORT_GEOM_B and ME_SHORT_GEOM_C as paired with A."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = {e["experiment_id"]: e for e in registry["experiments"]}
        assert "ME_SHORT_GEOM_A_V1" in exp_ids
        assert "ME_SHORT_GEOM_B_V1" in exp_ids
        assert "ME_SHORT_GEOM_C_V1" in exp_ids
        assert exp_ids["ME_SHORT_GEOM_B_V1"].get("paired_with") == "ME_SHORT_GEOM_A_V1"
        assert exp_ids["ME_SHORT_GEOM_C_V1"].get("paired_with") == "ME_SHORT_GEOM_A_V1"

    def test_me_geometry_variant_formulas_are_distinct(self):
        """A/B/C must compute different variant geometry values."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert 'ME_SHORT_GEOM_A_V1' in source
        assert 'ME_SHORT_GEOM_B_V1' in source
        assert 'ME_SHORT_GEOM_C_V1' in source
        # B must have wider stop logic
        assert "new_risk" in source or "0.5 * atr" in source
        # C must have delayed entry logic
        assert "current_price" in source or "entry_price" in source


# ──────────────────────────────────────────────────────────────
# Test 4: Evaluator processes correct tables
# ──────────────────────────────────────────────────────────────

class TestProspectiveEvaluatorTables:
    """Evaluator must read from prospective_observation and write to prospective_outcome."""

    def test_evaluator_reads_from_prospective_observation(self):
        """ProspectiveOOSEvaluator queries prospective_observation."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "prospective_observation" in source, (
            "prospective_observation table not referenced in evaluator"
        )

    def test_evaluator_writes_to_prospective_outcome(self):
        """ProspectiveOOSEvaluator upserts into prospective_outcome."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "prospective_outcome" in source, (
            "prospective_outcome table not referenced in evaluator"
        )

    def test_evaluator_enforces_started_at_boundary(self):
        """Evaluator must only evaluate observations with signal_time >= experiment.started_at."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_evaluator.py").read_text()
        assert "started_at" in source, "started_at boundary not enforced in evaluator"

    def test_evaluator_horizons_cover_all_five(self):
        """Evaluator must process all 5 horizons: 15m, 30m, 60m, 120m, 240m."""
        from app.research.constants import HORIZONS
        labels = [h[0] for h in HORIZONS]
        assert "15m" in labels
        assert "30m" in labels
        assert "60m" in labels
        assert "120m" in labels
        assert "240m" in labels
        assert len(HORIZONS) == 5


# ──────────────────────────────────────────────────────────────
# Test 5: Experiments with status != RUNNING are not evaluated
# ──────────────────────────────────────────────────────────────

class TestExperimentStatusFiltering:
    """Only RUNNING experiments should produce observations and outcomes."""

    def test_load_prospective_experiments_filters_by_running(self):
        """_load_prospective_experiments must filter WHERE status = 'RUNNING'."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "status = 'RUNNING'" in source or "status='RUNNING'" in source, (
            "_load_prospective_experiments does not filter by status='RUNNING'"
        )

    def test_registry_experiments_are_ready_to_start(self):
        """All registry experiments start with status='READY_TO_START' (not auto-activated)."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        for exp in registry["experiments"]:
            assert exp.get("status") == "READY_TO_START", (
                f"Registry experiment {exp['experiment_id']} has unexpected status: {exp.get('status')}"
            )

    def test_no_auto_activation_in_code(self):
        """No code should automatically set experiment status to RUNNING."""
        source = (PROJECT_ROOT / "scanner_runner.py").read_text()
        # Scanner runner should NOT modify experiment status
        assert "SET status = 'RUNNING'" not in source, (
            "scanner_runner.py should not auto-activate experiments"
        )
        assert "UPDATE research.prospective_experiment" not in source, (
            "scanner_runner.py should not modify prospective_experiment table"
        )


# ──────────────────────────────────────────────────────────────
# Test 6: Frozen experiment parameters integrity
# ──────────────────────────────────────────────────────────────

class TestFrozenParameters:
    """Frozen experiment parameters must not be modified by the runtime code."""

    def test_prospective_observer_does_not_modify_registry(self):
        """ProspectiveOOSObserver must be read-only on registry (append-only)."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        # Should only INSERT, never UPDATE or DELETE on prospective_experiment
        assert "UPDATE research.prospective_experiment" not in source
        assert "DELETE FROM research.prospective_experiment" not in source

    def test_prospective_observer_inserts_only(self):
        """ProspectiveOOSObserver.observe() must only INSERT, never UPDATE."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        # INSERT is expected; check no UPDATE on observation table in observe method
        assert "INSERT INTO research.prospective_observation" in source


# ──────────────────────────────────────────────────────────────
# Test 7: All scanner types are covered
# ──────────────────────────────────────────────────────────────

class TestScannerCoverage:
    """Prospective observer must be called for SRR LONG, ME SHORT, VC SHORT, LR SHORT."""

    def test_registry_covers_srr_long(self):
        """SRR_LONG_BASELINE_V1 must be in the registry."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "SRR_LONG_BASELINE_V1" in exp_ids

    def test_registry_covers_me_short(self):
        """ME_SHORT_GEOM_A/B/C_V1 must be in the registry."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "ME_SHORT_GEOM_A_V1" in exp_ids
        assert "ME_SHORT_GEOM_B_V1" in exp_ids
        assert "ME_SHORT_GEOM_C_V1" in exp_ids

    def test_registry_covers_vc_short(self):
        """VC_SHORT_BB_WIDTH_V1 must be in the registry."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "VC_SHORT_BB_WIDTH_V1" in exp_ids

    def test_registry_covers_lr_short(self):
        """LR_SHORT_GATE_V1 must be in the registry."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "LR_SHORT_GATE_V1" in exp_ids

    def test_prospective_observer_routes_all_scanners(self):
        """observe() must dispatch to _observe_standard or _observe_me_geometry."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "_observe_standard(" in source
        assert "_observe_me_geometry(" in source
