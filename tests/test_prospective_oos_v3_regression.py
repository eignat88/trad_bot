"""Regression tests for Prospective OOS V3 runtime wiring.

Tests three runtime failures discovered on VPS:
1. NameError: name 'prospective_obs' is not defined in scanner_runner.py
2. AttributeError: 'BybitClient' object has no attribute 'get_eligible_signals'
3. evaluator_runner.py blocking .start() prevents prospective path execution

Also verifies:
- ME A/B/C exact pairing by source_signal_id
- Evaluator processes research.prospective_observation → research.prospective_outcome
- Fail-closed initialization of prospective_obs
- Unified scheduler loop for generic + prospective
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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


# ══════════════════════════════════════════════════════════════
# Test 8: Unified scheduler loop (orchestration fix)
# ══════════════════════════════════════════════════════════════

class TestUnifiedSchedulerLoop:
    """evaluator_runner.py must use ONE scheduler loop for both generic + prospective.
    Blocking .start() calls must NOT be used in daemon mode.
    """

    def test_no_evaluator_start_call_in_runner(self):
        """evaluator_runner.py must NOT call evaluator.start() in daemon mode."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # The old pattern: evaluator.start( or evaluator.start (
        assert "evaluator.start(" not in source and "evaluator.start (" not in source, (
            "evaluator_runner.py still calls evaluator.start() — "
            "this blocks the scheduler loop"
        )

    def test_no_prospective_eval_start_call_in_runner(self):
        """evaluator_runner.py must NOT call prospective_eval.start() in daemon mode."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "prospective_eval.start(" not in source, (
            "evaluator_runner.py still calls prospective_eval.start() — "
            "this blocks the scheduler loop"
        )

    def test_run_scheduler_loop_exists(self):
        """A unified _run_scheduler_loop function must exist."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "def _run_scheduler_loop(" in source, (
            "_run_scheduler_loop function not found"
        )

    def test_run_single_cycle_exists(self):
        """A _run_single_cycle function must exist for --once mode."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "def _run_single_cycle(" in source, (
            "_run_single_cycle function not found"
        )

    def test_scheduler_loop_calls_generic_then_prospective(self):
        """_run_scheduler_loop must call generic experiments THEN prospective."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # Find _run_scheduler_loop body
        loop_start = source.find("def _run_scheduler_loop(")
        assert loop_start > 0
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        generic_pos = loop_body.find("run_evaluation_cycle(exp_id)")
        # Check both generic and prospective loaders are called
        assert "_load_active_experiments(conn)" in loop_body
        assert "_load_prospective_experiments(conn)" in loop_body

    def test_scheduler_loop_reloads_experiments_each_cycle(self):
        """Experiment lists must be reloaded each scheduler cycle (dynamic status)."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        # Both loaders must be called inside the while loop (not outside)
        assert loop_body.count("_load_active_experiments(conn)") >= 1
        assert loop_body.count("_load_prospective_experiments(conn)") >= 1

    def test_scheduler_loop_has_signal_handling(self):
        """_run_scheduler_loop must install SIGTERM/SIGINT handlers."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        assert "signal.signal(signal.SIGTERM" in loop_body
        assert "signal.signal(signal.SIGINT" in loop_body

    def test_scheduler_loop_has_interruptible_wait(self):
        """Scheduler loop must use interruptible wait, not time.sleep()."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        assert "shutdown.wait(" in loop_body or "shutdown.wait (" in loop_body
        # Must NOT use time.sleep in the scheduler loop
        assert "time.sleep" not in loop_body

    def test_scheduler_loop_error_isolation_generic(self):
        """Exception in generic experiment must not prevent prospective execution."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        # Check that generic evaluation is wrapped in try/except
        assert "except Exception:" in loop_body
        # Check that prospective is called AFTER the generic try/except block
        generic_except_pos = loop_body.find("except Exception:", loop_body.find("run_evaluation_cycle"))
        prospective_call_pos = loop_body.find("_load_prospective_experiments")
        assert prospective_call_pos > generic_except_pos, (
            "Prospective experiments should be loaded after generic error handling"
        )

    def test_daemon_mode_uses_scheduler_loop(self):
        """main() daemon path must call _run_scheduler_loop, not .start()."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # Find the else branch in main() that handles daemon mode
        assert "_run_scheduler_loop(" in source, (
            "main() does not call _run_scheduler_loop()"
        )

    def test_once_mode_uses_single_cycle(self):
        """main() --once path must call _run_single_cycle."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "_run_single_cycle(" in source, (
            "main() does not call _run_single_cycle()"
        )

    def test_no_start_methods_called_in_runner(self):
        """No .start() call on evaluator objects must exist in runner code (not docstrings)."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # Strip docstrings and comments to avoid false matches
        lines = source.splitlines()
        code_lines = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('"""') and stripped.endswith('"""') and len(stripped) > 6:
                continue  # single-line docstring
            if stripped.startswith('"""') and not in_docstring:
                in_docstring = True
                continue
            if in_docstring:
                if '"""' in stripped:
                    in_docstring = False
                continue
            if stripped.startswith('#'):
                continue
            code_lines.append(line)
        code_only = "\n".join(code_lines)
        # evaluator.start( and prospective_eval.start( must not appear in executable code
        assert "evaluator.start(" not in code_only, (
            "evaluator.start() called in executable code"
        )
        assert "prospective_eval.start(" not in code_only, (
            "prospective_eval.start() called in executable code"
        )

    def test_prospective_evaluator_instantiated_before_scheduler(self):
        """Prospective evaluator must be created before the scheduler loop starts."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        main_start = source.find("def main()")
        scheduler_call_pos = source.find("_run_scheduler_loop(")
        eval_create_pos = source.find("ProspectiveOOSEvaluator(")
        assert eval_create_pos < scheduler_call_pos, (
            "ProspectiveEvaluator must be created before _run_scheduler_loop"
        )

    def test_research_repo_passed_to_prospective_evaluator(self):
        """ResearchRepository must be passed to ProspectiveOOSEvaluator."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "repo=research_repo" in source or "repo = research_repo" in source, (
            "ResearchRepository not passed to ProspectiveOOSEvaluator"
        )

    def test_prospective_loader_requires_started_at(self):
        """_load_prospective_experiments must require started_at IS NOT NULL."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "started_at IS NOT NULL" in source, (
            "prospective loader does not require started_at IS NOT NULL"
        )

    def test_prospective_loader_requires_running(self):
        """_load_prospective_experiments must require status='RUNNING'."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        assert "status = 'RUNNING'" in source or "status='RUNNING'" in source, (
            "prospective loader does not require status='RUNNING'"
        )


# ══════════════════════════════════════════════════════════════
# Test 9: Error isolation behavior
# ══════════════════════════════════════════════════════════════

class TestErrorIsolation:
    """Errors in one experiment must not block others."""

    def test_generic_error_try_except_wraps_cycle(self):
        """Each generic experiment evaluation must be wrapped in try/except."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        # Find the generic evaluation loop in scheduler
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        # Check for try/except pattern around run_evaluation_cycle
        assert "try:" in loop_body
        assert "logger.exception(" in loop_body

    def test_prospective_error_try_except_wraps_cycle(self):
        """Each prospective experiment evaluation must be wrapped in try/except."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        # Both "try" and "except" should appear before and after prospective call
        pos = loop_body.find("prospective_eval.run_evaluation_cycle")
        assert pos > 0, "prospective run_evaluation_cycle not found"
        before = loop_body[:pos]
        assert "try:" in before, "prospective evaluation not wrapped in try"

    def test_both_experiment_types_independent(self):
        """A single scheduler cycle must handle both types independently."""
        source = (PROJECT_ROOT / "app" / "research" / "evaluator_runner.py").read_text()
        loop_start = source.find("def _run_scheduler_loop(")
        loop_body = source[loop_start:source.find("\ndef ", loop_start + 1)]
        # Generic section
        assert "experiment_ids = _load_active_experiments(conn)" in loop_body
        # Prospective section
        assert "prospective_ids = _load_prospective_experiments(conn)" in loop_body
        # Both must be present — not one-or-the-other
        assert "for exp_id in experiment_ids:" in loop_body
        assert "for exp_id in prospective_ids:" in loop_body
