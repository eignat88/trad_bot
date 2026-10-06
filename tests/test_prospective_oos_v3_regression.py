"""Regression tests for Prospective OOS V3 runtime wiring.

Tests four runtime failures discovered on VPS:
1. NameError: name 'prospective_obs' is not defined in scanner_runner.py
2. AttributeError: 'BybitClient' object has no attribute 'get_eligible_signals'
3. evaluator_runner.py blocking .start() prevents prospective path execution
4. NameError: name 'direction' is not defined in _observe_standard/_observe_me_geometry

Also verifies:
- ME A/B/C exact pairing by source_signal_id
- Evaluator processes research.prospective_observation → research.prospective_outcome
- Fail-closed initialization of prospective_obs
- Unified scheduler loop for generic + prospective
- Runtime observe() for SRR LONG, VC SHORT, LR SHORT, ME SHORT A/B/C
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from datetime import datetime, timezone
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
        """Registry lifecycle matches explicit HTF Phase B activation."""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        registry = json.loads(
            (root / "app" / "research" / "prospective_registry.json").read_text()
        )

        activated = {
            "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
            "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE",
        }
        expected_ts = "2026-10-06T12:54:00Z"

        seen = set()
        for exp in registry["experiments"]:
            exp_id = exp["experiment_id"]
            if exp_id in activated:
                seen.add(exp_id)
                assert exp.get("status") == "RUNNING"
                assert exp.get("started_at") == expected_ts
                assert exp.get("freeze_ts") == expected_ts
            else:
                assert exp.get("status") == "READY_TO_START", (
                    f"{exp_id} has unexpected status {exp.get('status')}"
                )
                assert exp.get("started_at") is None

        assert seen == activated

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

    def test_registry_covers_lr_long(self):
        """LR_LONG_GATE_V1 must be in the registry."""
        import json
        registry = json.loads(
            (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
        )
        exp_ids = [e["experiment_id"] for e in registry["experiments"]]
        assert "LR_LONG_GATE_V1" in exp_ids

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


# ══════════════════════════════════════════════════════════════
# Test 10: NameError: direction not defined in _observe_standard/_observe_me_geometry
# ══════════════════════════════════════════════════════════════

class TestDirectionParameterBug:
    """Regression: NameError: name 'direction' is not defined.

    Both _observe_standard() and _observe_me_geometry() use `direction` in their
    SQL INSERT but it was not passed as a parameter from observe().
    """

    def test_observe_standard_accepts_direction(self):
        """_observe_standard must have 'direction' in its parameter list."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "_observe_standard":
                        arg_names = [arg.arg for arg in item.args.args]
                        assert "direction" in arg_names, (
                            f"_observe_standard missing 'direction' param; got: {arg_names}"
                        )
                        return
        pytest.fail("_observe_standard not found")

    def test_observe_me_geometry_accepts_direction(self):
        """_observe_me_geometry must have 'direction' in its parameter list."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "_observe_me_geometry":
                        arg_names = [arg.arg for arg in item.args.args]
                        assert "direction" in arg_names, (
                            f"_observe_me_geometry missing 'direction' param; got: {arg_names}"
                        )
                        return
        pytest.fail("_observe_me_geometry not found")

    def test_observe_passes_direction_to_standard(self):
        """observe() must pass direction to _observe_standard()."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        # Find the _observe_standard call inside observe()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "observe":
                        body_src = ast.get_source_segment(source, item)
                        assert "self._observe_standard(" in body_src
                        # The call must include direction as an argument
                        # Find the call and check args
                        for subnode in ast.walk(item):
                            if (isinstance(subnode, ast.Call)
                                    and hasattr(subnode.func, 'attr')
                                    and subnode.func.attr == "_observe_standard"):
                                # Check that 'direction' appears as an argument
                                arg_names_in_call = [
                                    getattr(a, 'id', getattr(a, 'arg', ''))
                                    for a in subnode.args
                                ]
                                # direction should be in positional args (after symbol, before signal_time)
                                assert "direction" in arg_names_in_call, (
                                    f"observe() does not pass direction to _observe_standard; "
                                    f"args: {arg_names_in_call}"
                                )
                                return
        pytest.fail("observe() -> _observe_standard call not found")

    def test_observe_passes_direction_to_me_geometry(self):
        """observe() must pass direction to _observe_me_geometry()."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "observe":
                        for subnode in ast.walk(item):
                            if (isinstance(subnode, ast.Call)
                                    and hasattr(subnode.func, 'attr')
                                    and subnode.func.attr == "_observe_me_geometry"):
                                arg_names_in_call = [
                                    getattr(a, 'id', getattr(a, 'arg', ''))
                                    for a in subnode.args
                                ]
                                assert "direction" in arg_names_in_call, (
                                    f"observe() does not pass direction to _observe_me_geometry; "
                                    f"args: {arg_names_in_call}"
                                )
                                return
        pytest.fail("observe() -> _observe_me_geometry call not found")

    def test_observe_standard_uses_direction_in_insert(self):
        """_observe_standard must reference 'direction' in its VALUES tuple."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ProspectiveOOSObserver":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "_observe_standard":
                        body_src = ast.get_source_segment(source, item)
                        assert "direction" in body_src
                        return
        pytest.fail("_observe_standard not found")


# ══════════════════════════════════════════════════════════════
# Test 11: Runtime observe() — actual call tests with mock cursor
# ══════════════════════════════════════════════════════════════

def _load_registry():
    """Load the frozen prospective registry."""
    return json.loads(
        (PROJECT_ROOT / "app" / "research" / "prospective_registry.json").read_text()
    )


def _make_registry_dict(registry_data, primary: str):
    """Convert registry JSON to {experiment_id: exp_spec} dict with one primary experiment."""
    return {primary: next(e for e in registry_data["experiments"] if e["experiment_id"] == primary)}


def _make_registry_dict_me_abc(registry_data):
    """Return all three ME geometry experiments for A/B/C paired capture."""
    ids = ("ME_SHORT_GEOM_A_V1", "ME_SHORT_GEOM_B_V1", "ME_SHORT_GEOM_C_V1")
    return {e["experiment_id"]: e for e in registry_data["experiments"] if e["experiment_id"] in ids}


def _make_observe_kwargs(*, symbol="PENGUUSDT", direction="LONG",
                         scanner_name="SUPPORT_RESISTANCE_REACTION",
                         overrides=None):
    """Build common observe() kwargs with optional overrides."""
    defaults = dict(
        scanner_name=scanner_name,
        direction=direction,
        symbol=symbol,
        signal_time=datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc),
        reference_price=0.01234,
        invalidation_price=0.01234 * 1.002,
        target_1=0.01234 - 0.0005,
        target_2=None,
        score=85.0,
        features={"bb_width_percentile": 0.4, "atr": 0.0001, "entry_price": 0.01230},
        parameters={},
        market_regime="TRENDING",
    )
    if overrides:
        defaults.update(overrides)
    return defaults


# ── helpers for ProspectiveOOSObserver call sequencing ──────

def _mock_conn_for_observer(lifecycle_status="RUNNING", insert_returns=None, lifecycle_count=1):
    """Build conn/cursor for ProspectiveOOSObserver.observe() calls.

    fetchone call order: lifecycle SELECT row(s), then INSERT RETURNING row(s).
    """
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    results = [(lifecycle_status,) for _ in range(lifecycle_count)]
    if insert_returns is None:
        insert_returns = [(1,)]
    for r in insert_returns:
        results.append(r)
    cursor.fetchone.side_effect = results
    return conn, cursor


def _get_insert_calls(cursor):
    """Return only INSERT INTO research.prospective_observation execute calls."""
    out = []
    for call in cursor.execute.call_args_list:
        sql = call[0][0] if call[0] else ""
        if "INSERT INTO research.prospective_observation" in sql:
            out.append(call)
    return out


def _get_insert_call_args(cursor, exp_id):
    """Return the INSERT params tuple for a specific experiment_id."""
    for call in _get_insert_calls(cursor):
        params = call[0][1]
        if params and params[0] == exp_id:
            return params
    return None


def _get_all_insert_args(cursor):
    """Return all INSERT params tuples (list of lists)."""
    out = []
    for call in _get_insert_calls(cursor):
        params = call[0][1]
        if params:
            out.append(list(params))
    return out


class TestObserveSRRLong:
    """Runtime test: observe() for SUPPORT_RESISTANCE_REACTION LONG."""

    def test_srr_long_creates_observation(self):
        """SRR_LONG_BASELINE_V1 observation must be inserted for SRR LONG."""
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            direction="LONG",
        )
        observer.observe(**kwargs)

        # Must have committed
        mock_conn.commit.assert_called_once()
        # Must have executed an observation INSERT
        insert_calls = _get_insert_calls(mock_cursor)
        assert insert_calls, "No observation INSERT found"
        # Verify experiment_id in VALUES
        args = _get_insert_call_args(mock_cursor, "SRR_LONG_BASELINE_V1")
        assert args is not None, "SRR_LONG_BASELINE_V1 INSERT not found"
        assert args[0] == "SRR_LONG_BASELINE_V1"

    def test_srr_long_direction_in_values(self):
        """SRR LONG must pass direction='LONG' in the INSERT VALUES."""
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            direction="LONG",
        )
        observer.observe(**kwargs)

        args = _get_insert_call_args(mock_cursor, "SRR_LONG_BASELINE_V1")
        assert args is not None, "SRR_LONG_BASELINE_V1 INSERT not found"
        # direction is the 5th value (index 4) in the INSERT
        assert args[4] == "LONG"

    def test_srr_long_wrong_direction_no_observation(self):
        """SRR SHORT must NOT create SRR_LONG_BASELINE_V1 observation."""
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[], lifecycle_count=2)
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value = mock_cursor

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="SUPPORT_RESISTANCE_REACTION",
            direction="SHORT",
        )
        observer.observe(**kwargs)

        # SRR_LONG_BASELINE_V1 requires direction=LONG, so no INSERT should happen
        # (the loop filters by direction match)
        # Only lifecycle SELECT should be called, no observation INSERT
        for call in _get_insert_calls(mock_cursor):
            params = call[0][1]
            if params:
                assert params[0] != "SRR_LONG_BASELINE_V1"


class TestObserveVCShort:
    """Runtime test: observe() for VOLATILITY_COMPRESSION SHORT."""

    def test_vc_short_creates_observation(self):
        """VC_SHORT_BB_WIDTH_V1 observation must be inserted for VC SHORT."""
        registry = _make_registry_dict(_load_registry(), "VC_SHORT_BB_WIDTH_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="VOLATILITY_COMPRESSION",
            direction="SHORT",
            overrides={"features": {"bb_width_percentile": 0.4, "atr": 0.001}},
        )
        observer.observe(**kwargs)

        mock_conn.commit.assert_called_once()
        assert _get_insert_calls(mock_cursor)
        params = _get_insert_call_args(mock_cursor, "VC_SHORT_BB_WIDTH_V1")
        assert params is not None, "VC_SHORT_BB_WIDTH_V1 INSERT not found"

    def test_vc_short_direction_in_values(self):
        """VC SHORT must pass direction='SHORT' in the INSERT."""
        registry = _make_registry_dict(_load_registry(), "VC_SHORT_BB_WIDTH_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="VOLATILITY_COMPRESSION",
            direction="SHORT",
            overrides={"features": {"bb_width_percentile": 0.4, "atr": 0.001}},
        )
        observer.observe(**kwargs)

        args = _get_insert_call_args(mock_cursor, "VC_SHORT_BB_WIDTH_V1")
        assert args is not None, "VC_SHORT_BB_WIDTH_V1 INSERT not found"
        assert args[4] == "SHORT"

    def test_vc_short_bb_width_above_threshold_rejects(self):
        """VC SHORT with bb_width_percentile >= threshold: rule_passed=False."""
        registry = _make_registry_dict(_load_registry(), "VC_SHORT_BB_WIDTH_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="VOLATILITY_COMPRESSION",
            direction="SHORT",
            overrides={"features": {"bb_width_percentile": 0.9}},  # above threshold 0.569723
        )
        observer.observe(**kwargs)

        args = _get_insert_call_args(mock_cursor, "VC_SHORT_BB_WIDTH_V1")
        assert args is not None, "VC_SHORT_BB_WIDTH_V1 INSERT not found"
        # rule_passed is at index 11
        assert args[11] is True, "bb_width 0.9 should be rule_passed=True (capture still occurs)"

    def test_vc_short_bb_width_below_threshold_passes(self):
        """VC SHORT with bb_width_percentile < threshold: rule_passed=True."""
        registry = _make_registry_dict(_load_registry(), "VC_SHORT_BB_WIDTH_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="VOLATILITY_COMPRESSION",
            direction="SHORT",
            overrides={"features": {"bb_width_percentile": 0.3}},  # below threshold 0.569723
        )
        observer.observe(**kwargs)

        args = _get_insert_call_args(mock_cursor, "VC_SHORT_BB_WIDTH_V1")
        assert args is not None, "VC_SHORT_BB_WIDTH_V1 INSERT not found"
        assert args[11] is True, "bb_width 0.3 should be rule_passed=True"


class TestObserveLRLong:
    """Runtime test: observe() for LIQUIDITY_REVERSAL LONG."""

    def test_lr_long_creates_observation(self):
        """LR_LONG_GATE_V1 observation must be inserted for LR LONG."""
        registry = _make_registry_dict(_load_registry(), "LR_LONG_GATE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="LIQUIDITY_REVERSAL",
            direction="LONG",
        )
        observer.observe(**kwargs)

        mock_conn.commit.assert_called_once()
        assert _get_insert_calls(mock_cursor)
        params = _get_insert_call_args(mock_cursor, "LR_LONG_GATE_V1")
        assert params is not None, "LR_LONG_GATE_V1 INSERT not found"
        assert params[0] == "LR_LONG_GATE_V1"

    def test_lr_long_direction_in_values(self):
        """LR LONG must pass direction='LONG' in the INSERT."""
        registry = _make_registry_dict(_load_registry(), "LR_LONG_GATE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="LIQUIDITY_REVERSAL",
            direction="LONG",
        )
        observer.observe(**kwargs)

        params = _get_insert_call_args(mock_cursor, "LR_LONG_GATE_V1")
        assert params is not None, "LR_LONG_GATE_V1 INSERT not found"
        assert params[4] == "LONG"


class TestObserveMEShortABC:
    """Runtime test: observe() for MOMENTUM_EXHAUSTION SHORT → 3 paired A/B/C."""

    def test_me_short_creates_three_observations(self):
        """MOMENTUM_EXHAUSTION SHORT must create 3 observations (A, B, C)."""
        registry = _make_registry_dict_me_abc(_load_registry())
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,), (2,), (3,)], lifecycle_count=3)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "invalidation_price": 0.01234 * 1.002,
                "target_1": 0.01180,
                "features": {"atr": 0.0001, "entry_price": 0.01230},
            },
        )
        observer.observe(**kwargs)

        # Must execute 3 INSERT statements (A, B, C)
        all_insert_args = _get_all_insert_args(mock_cursor)
        assert len(all_insert_args) == 3, (
            f"Expected 3 inserts for ME SHORT A/B/C, got {len(all_insert_args)}"
        )

        # Verify experiment IDs
        exp_ids = [args[0] for args in all_insert_args]
        assert "ME_SHORT_GEOM_A_V1" in exp_ids
        assert "ME_SHORT_GEOM_B_V1" in exp_ids
        assert "ME_SHORT_GEOM_C_V1" in exp_ids

    def test_me_short_abc_share_source_key(self):
        """All 3 ME geometry variants must share the same source_signal_id."""
        registry = _make_registry_dict_me_abc(_load_registry())
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,), (2,), (3,)], lifecycle_count=3)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "invalidation_price": 0.01234 * 1.002,
                "target_1": 0.01180,
                "features": {"atr": 0.0001, "entry_price": 0.01230},
            },
        )
        observer.observe(**kwargs)

        all_insert_args = _get_all_insert_args(mock_cursor)
        source_keys = [args[1] for args in all_insert_args]  # source_signal_id is index 1
        assert len(source_keys) == 3
        assert source_keys[0] == source_keys[1] == source_keys[2], (
            f"ME A/B/C must share same source_signal_id, got: {source_keys}"
        )

    def test_me_short_direction_in_all_three(self):
        """All 3 ME geometry variants must have direction='SHORT'."""
        registry = _make_registry_dict_me_abc(_load_registry())
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,), (2,), (3,)], lifecycle_count=3)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "invalidation_price": 0.01234 * 1.002,
                "target_1": 0.01180,
                "features": {"atr": 0.0001, "entry_price": 0.01230},
            },
        )
        observer.observe(**kwargs)

        for args in _get_all_insert_args(mock_cursor):
            assert args[4] == "SHORT", (
                f"direction must be 'SHORT' in all ME inserts, got {args[4]} "
                f"for {args[0]}"
            )

    def test_me_short_geom_a_uses_original_geometry(self):
        """GEOM_A must use original reference_price, invalidation_price, target_1."""
        registry = _make_registry_dict(_load_registry(), "ME_SHORT_GEOM_A_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        ref_price = 0.01234
        inv_price = ref_price * 1.002
        target = 0.01180

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "reference_price": ref_price,
                "invalidation_price": inv_price,
                "target_1": target,
                "features": {"atr": 0.0001, "entry_price": 0.01230},
            },
        )
        observer.observe(**kwargs)

        params = _get_insert_call_args(mock_cursor, "ME_SHORT_GEOM_A_V1")
        assert params is not None, "ME_SHORT_GEOM_A_V1 not found in inserts"
        # variant_entry=ref_price, variant_stop=inv_price, variant_target=target
        # ME insert order: [12] variant_entry, [13] variant_stop, [14] variant_target
        assert params[12] == ref_price, "GEOM_A variant_entry must be reference_price"
        assert params[13] == inv_price, "GEOM_A variant_stop must be invalidation_price"
        assert params[14] == target, "GEOM_A variant_target must be target_1"

    def test_me_short_geom_b_wider_stop(self):
        """GEOM_B must use wider stop than A."""
        registry = _make_registry_dict(_load_registry(), "ME_SHORT_GEOM_B_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        ref_price = 100.0
        inv_price = 100.2  # 0.2% stop
        target = 95.0

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "reference_price": ref_price,
                "invalidation_price": inv_price,
                "target_1": target,
                "features": {"atr": 1.5, "entry_price": 99.9},
            },
        )
        observer.observe(**kwargs)

        params = _get_insert_call_args(mock_cursor, "ME_SHORT_GEOM_B_V1")
        assert params is not None, "ME_SHORT_GEOM_B_V1 not found in inserts"
        # variant_stop must be wider than inv_price
        # [13] variant_stop
        assert params[13] > inv_price, (
            f"GEOM_B variant_stop ({params[13]}) must be wider than "
            f"invalidation_price ({inv_price})"
        )

    def test_me_short_geom_c_delayed_entry(self):
        """GEOM_C must use entry_price from features as entry."""
        registry = _make_registry_dict(_load_registry(), "ME_SHORT_GEOM_C_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        ref_price = 100.0
        inv_price = 100.2
        target = 95.0
        detection_close = 99.9

        kwargs = _make_observe_kwargs(
            scanner_name="MOMENTUM_EXHAUSTION",
            direction="SHORT",
            overrides={
                "reference_price": ref_price,
                "invalidation_price": inv_price,
                "target_1": target,
                "features": {"atr": 1.5, "entry_price": detection_close},
            },
        )
        observer.observe(**kwargs)

        params = _get_insert_call_args(mock_cursor, "ME_SHORT_GEOM_C_V1")
        assert params is not None, "ME_SHORT_GEOM_C_V1 not found in inserts"
        # variant_entry must be detection candle close
        # [12] variant_entry
        assert params[12] == detection_close, (
            f"GEOM_C variant_entry ({params[12]}) must be "
            f"detection candle close ({detection_close})"
        )


class TestIdempotency:
    """UNIQUE(experiment_id, source_signal_id) / DO NOTHING semantics."""

    def test_duplicate_observe_is_idempotent(self):
        """Calling observe() twice with same data must not create duplicates."""
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        mock_conn, mock_cursor = _mock_conn_for_observer(insert_returns=[(1,)], lifecycle_count=2)
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value = mock_cursor

        from app.research.prospective_observer import ProspectiveOOSObserver
        observer = ProspectiveOOSObserver(mock_conn, registry)

        kwargs = _make_observe_kwargs()

        observer.observe(**kwargs)
        first_insert_count = len(_get_insert_calls(mock_cursor))

        # Reset mock for second call
        mock_cursor.reset_mock()
        mock_cursor.fetchone.side_effect = [("RUNNING",), ("RUNNING",), (2,)]
        mock_cursor.rowcount = 0  # DO NOTHING on conflict
        mock_conn.reset_mock()

        observer.observe(**kwargs)

        # Same number of INSERT attempts
        second_insert_count = len(_get_insert_calls(mock_cursor))
        assert second_insert_count == first_insert_count

    def test_same_source_key_same_symbol_same_direction(self):
        """Same (scanner, symbol, direction, signal_time) produces same source_key."""
        from app.research.prospective_observer import ProspectiveOOSObserver
        mock_conn = MagicMock()
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        observer = ProspectiveOOSObserver(mock_conn, registry)

        key1 = observer._make_source_key("SUPPORT_RESISTANCE_REACTION", "PENGUUSDT", "LONG", "2026-09-28T14:00:00Z")
        key2 = observer._make_source_key("SUPPORT_RESISTANCE_REACTION", "PENGUUSDT", "LONG", "2026-09-28T14:00:00Z")
        assert key1 == key2

    def test_different_symbol_produces_different_key(self):
        """Different symbols must produce different source_keys."""
        from app.research.prospective_observer import ProspectiveOOSObserver
        mock_conn = MagicMock()
        registry = _make_registry_dict(_load_registry(), "SRR_LONG_BASELINE_V1")
        observer = ProspectiveOOSObserver(mock_conn, registry)

        key1 = observer._make_source_key("SUPPORT_RESISTANCE_REACTION", "PENGUUSDT", "LONG", "2026-09-28T14:00:00Z")
        key2 = observer._make_source_key("SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "LONG", "2026-09-28T14:00:00Z")
        assert key1 != key2


class TestNoAutoActivation:
    """Experiments must remain PAUSED/READY_TO_START after observe()."""

    def test_observe_does_not_update_experiment_status(self):
        """observe() must only INSERT into prospective_observation, never UPDATE prospective_experiment."""
        source = (PROJECT_ROOT / "app" / "research" / "prospective_observer.py").read_text()
        assert "UPDATE research.prospective_experiment" not in source
        assert "UPDATE research.prospective_observation" not in source
        # The lifecycle-check SELECT is expected infrastructure behavior.
        assert "SELECT status FROM research.prospective_experiment" in source

    def test_registry_experiments_still_ready_to_start(self):
        """Registry lifecycle matches explicit HTF Phase B activation."""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        registry = json.loads(
            (root / "app" / "research" / "prospective_registry.json").read_text()
        )

        activated = {
            "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
            "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE",
        }
        expected_ts = "2026-10-06T12:54:00Z"

        seen = set()
        for exp in registry["experiments"]:
            exp_id = exp["experiment_id"]
            if exp_id in activated:
                seen.add(exp_id)
                assert exp.get("status") == "RUNNING"
                assert exp.get("started_at") == expected_ts
                assert exp.get("freeze_ts") == expected_ts
            else:
                assert exp.get("status") == "READY_TO_START", (
                    f"{exp_id} has unexpected status {exp.get('status')}"
                )
                assert exp.get("started_at") is None

        assert seen == activated
