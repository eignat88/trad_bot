"""MAIN_PR_SAFETY_HTF_PHASE2_SCOPE_FIX_V1 (R2) — routing tests for the
three-way HTF Phase-2 scope gate in ``.github/workflows/main-pr-safety.yml``.

R1 fixed the fail-open substring routing but still treated every PR with a
Phase-2 hit as a Phase-2-ONLY PR. That made the Phase-2-only surface
confinement a universal gate and blocked mixed PRs (PR #233: Bybit retry
budget work touching ``app/research/htf_baseline_short_phase2_bybit_candle_
source_v1.py`` hit the two confinement tests and failed with 2 FAILs).

R2 classifies the changed-file list fail-closed into exactly three routes:

* ``PHASE2_ONLY`` — every changed file is inside the governed surface
  (Phase-2 surface + frozen Point-B stack + the gate workflow). The FULL
  Phase-2 isolation gate runs, including the surface-confinement checks.
* ``MIXED_WITH_PHASE2`` — at least one governed file AND at least one file
  outside the surface (the PR #233 shape). Phase-2-only confinement does NOT
  apply; instead the frozen-invariant regression (the isolation module minus
  the two confinement tests) plus the HTF causal/parity/closed-bar regression
  runs. Frozen constraints stay fail-closed for mixed PRs.
* ``NO_PHASE2`` — no governed file at all. No HTF gate.

These tests execute the REAL classification script extracted from the
workflow heredoc (``python3 - <<'PY_SCOPE'``) and pin:

* one fixture per route (Phase-2-only, mixed, no-Phase2) plus the frozen
  fail-closed edges and this CI-fix PR's own file set (it must itself route
  ``PHASE2_ONLY`` and pass ``Main PR Safety``);
* a MANDATORY integration test classifying the REAL diff of PR #233 against
  ``origin/main`` (live ``refs/pull/233/head`` when fetchable, otherwise the
  audited head pin) and checking the expected routing (mixed regression,
  never the Phase-2-only confinement gate);
* drift guards: the workflow's ``FROZEN_PATHS``/surface lists stay equal to
  the isolation gate's own lists, and the two confinement tests stay exactly
  the tests that depend on the PR diff;
* workflow wiring: class-routed gate steps, ``--no-renames`` rename safety,
  the unconditional wiring of these routing tests, no fail-open escapes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "main-pr-safety.yml"
ISOLATION = ROOT / "tests" / "test_htf_baseline_short_phase2_isolation_v1.py"
HEREDOC_START = "python3 - <<'PY_SCOPE'"
HEREDOC_END = "PY_SCOPE"

PHASE2_ONLY = "PHASE2_ONLY"
MIXED_WITH_PHASE2 = "MIXED_WITH_PHASE2"
NO_PHASE2 = "NO_PHASE2"

#: the two Phase-2-only surface-confinement tests (meaningless — and
#: PR-blocking — outside a Phase-2-only PR)
CONFINEMENT_TESTS = [
    "test_no_tracked_file_modified_against_base",
    "test_new_files_are_confined_to_phase2_surface",
]

#: HTF regression run for MIXED_WITH_PHASE2 PRs: causal / parity / closed-bar
MIXED_HTF_MODULES = [
    "tests/test_htf_baseline_short_phase2_causal_contracts_v1.py",
    "tests/test_htf_baseline_short_episode_id_parity_asof_batch_v1.py",
    "tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py",
]

ISOLATION_MODULE_PATH = "tests/test_htf_baseline_short_phase2_isolation_v1.py"
ISOLATION_STEP = "Verify HTF Phase-2 isolation"
MIXED_STEP = (
    "Verify HTF frozen invariants and causal/parity/closed-bar regression"
)

# --- PR #233 (issue #229 Bybit retry budget) -------------------------------
#: audited PR #233 head at task time (fallback when the live ref cannot be
#: fetched, e.g. in a write-restricted sandbox)
PR233_HEAD_PIN = "e5cd31f3c1e3e172d4fc6b91c11f3929bbc00b62"
#: Phase-2 surface files PR #233 touches (the reason it routes "mixed")
PR233_KNOWN_PHASE2_PATHS = {
    "app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py",
    "tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py",
}
#: audited PR #233 changed-file list (name-only, --no-renames) vs origin/main
PR233_CHANGED_FILES_PIN = [
    ".github/workflows/bybit-retry-budget-ci-v1.yml",
    "app/analytics/candle_sync.py",
    "app/config/settings.py",
    "app/exchange/bybit_client.py",
    "app/exchange/retry_budget.py",
    "app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py",
    "app/shadow/runner.py",
    "app/shadow/v2d_runner.py",
    "docs/research/BYBIT_ORDER_SUBMIT_GUARD_RESTART_RECOVERY_R3_V1.md",
    "docs/research/BYBIT_RETRY_BUDGET_CI_V1_IMPLEMENTATION_PROPOSAL_V1.md",
    "docs/research/BYBIT_RETRY_BUDGET_HARDENING_V1_IMPLEMENTATION_V1.md",
    "tests/conftest.py",
    "tests/test_bybit_api_telemetry_v1.py",
    "tests/test_bybit_order_submit_guard_multiproc_v1.py",
    "tests/test_bybit_order_submit_guard_v1.py",
    "tests/test_bybit_retry_budget_v1.py",
    "tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py",
    "tests/test_shadow_cli.py",
    "tools/bybit_order_submit_reconcile_v1.py",
]

# --- routing fixtures ------------------------------------------------------
#: Phase-2-only fixture: confined to the governed surface
FIXTURE_PHASE2_ONLY = [
    ".github/workflows/main-pr-safety.yml",
    "app/research/htf_baseline_short_phase2_observer_runner_v1.py",
    "tests/test_htf_baseline_short_phase2_isolation_v1.py",
    "docs/research/HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1.md",
    "deploy/systemd/trad-bot-htf-baseline-short-observer.service",
    "sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql",
]
#: mixed fixture: Phase-2 files + unrelated work (the PR #233 shape)
FIXTURE_MIXED = [
    "app/research/htf_baseline_short_phase2_observer_runner_v1.py",
    "tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py",
    "app/main.py",
    "tests/test_db_config.py",
]
#: no-Phase-2 fixture: nothing governed
FIXTURE_NO_PHASE2 = [
    "README.md",
    "app/main.py",
    "tests/test_db_config.py",
    "docs/research/ME_R_V3_RUNNER_ENABLEMENT_SAFETY_V1.md",
    "sql/migrations/077_me_short_a_fill_feasibility_v1.sql",
]
#: this CI-fix PR's own file set — it must route PHASE2_ONLY and pass the
#: full Main PR Safety gate itself
FIXTURE_CI_FIX_PR = [
    ".github/workflows/main-pr-safety.yml",
    "tests/test_htf_baseline_short_phase2_scope_routing_v1.py",
    "docs/research/HTF_BASELINE_SHORT_PHASE2_CI_SCOPE_ROUTING_V1.md",
]

#: Phase-2 surface and frozen-guarded single paths (regression anchors from
#: R1 kept: hyphenated observer unit, the gate workflow, the frozen stack)
GOVERNED_PATHS = [
    "app/research/htf_baseline_short_phase2_observer_runner_v1.py",
    "tools/research/htf_baseline_short_phase2_freeze_record_v1.py",
    "tests/test_htf_baseline_short_phase2_isolation_v1.py",
    "tests/test_htf_baseline_short_phase2_runtime_v1.py",
    "docs/research/HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1.md",
    "docs/research/HTF_BASELINE_SHORT_PHASE2_CI_SCOPE_ROUTING_V1.md",
    "sql/migrations/074_htf_baseline_short_phase2_registration.sql",
    "sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql",
    "deploy/systemd/trad-bot-htf-baseline-short-observer.service",
    ".github/workflows/main-pr-safety.yml",
    "app/research/htf_keylevel_point_b.py",
    "app/research/prospective_observer.py",
    "app/research/prospective_registry.json",
    "app/research/htf_baseline_short_multi_level_lifecycle_v1.py",
    "app/research/htf_baseline_short_stateful_episode_v1.py",
    "app/research/htf_baseline_short_causal_level_adapter_v1.py",
    "app/research/htf_keylevel_baseline_short_causal_observer_v1.py",
    "tools/research/htf_baseline_short_multi_level_replay_v1.py",
]

UNGOVERNED_PATHS = [
    "README.md",
    "app/main.py",
    "config.yaml",
    "requirements.txt",
    "scanner_runner.py",
    "tests/test_db_config.py",
    "tests/test_lr_long_gate_v1_routing_fix.py",
    "app/research/me_reverse_long_v3/runner.py",
    "app/research/srr_short_execution_r_expansion_prospective_evaluator.py",
    "docs/research/ME_R_V3_RUNNER_ENABLEMENT_SAFETY_V1.md",
    "deploy/systemd/trad-bot.service",
    "sql/migrations/077_me_short_a_fill_feasibility_v1.sql",
    ".github/workflows/me-short-a-execution-ci.yml",
]


# ---------------------------------------------------------------------------
# workflow script extraction and execution (the REAL classifier)
# ---------------------------------------------------------------------------


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _detection_script() -> str:
    """Extract the real PY_SCOPE classification script from the workflow."""
    lines = _workflow_text().splitlines()
    start = next((i for i, ln in enumerate(lines) if HEREDOC_START in ln), None)
    if start is None:
        pytest.fail("workflow no longer embeds the PY_SCOPE detection heredoc")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == HEREDOC_END:
            break
        body.append(line)
    else:
        pytest.fail("PY_SCOPE heredoc terminator not found")
    indents = [len(ln) - len(ln.lstrip()) for ln in body if ln.strip()]
    if not indents:
        pytest.fail("PY_SCOPE heredoc is empty")
    cut = min(indents)
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in body)


def _run_scope(paths, tmp_path, monkeypatch) -> str:
    """Execute the workflow's real classifier over ``paths``; return the class.

    Also validates the output contract: the script MUST append ``class=<tok>``
    and a consistent ``changed=`` decision to ``$GITHUB_OUTPUT``.
    """
    changed = tmp_path / "changed_files"
    changed.write_bytes(b"\0".join(p.encode("utf-8") for p in paths) + b"\0")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("HTF_PHASE2_CHANGED_FILES", str(changed))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    exec(  # noqa: S102 - executing the audited workflow heredoc is the point
        compile(_detection_script(), "main-pr-safety.yml:PY_SCOPE", "exec"),
        {"__name__": "__main__"},
    )
    written = output.read_text(encoding="utf-8")
    classes = re.findall(r"class=(\S+)", written)
    assert classes, f"detection script wrote no class decision: {written!r}"
    changed_flags = re.findall(r"changed=(true|false)", written)
    assert changed_flags, f"detection script wrote no changed decision: {written!r}"
    result = classes[-1]
    assert result in {PHASE2_ONLY, MIXED_WITH_PHASE2, NO_PHASE2}, result
    expected_changed = "false" if result == NO_PHASE2 else "true"
    assert changed_flags[-1] == expected_changed, (
        f"class={result} inconsistent with changed={changed_flags[-1]}"
    )
    return result


def _git(*args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(
            f"git command failed ({result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    return result.stdout


def _git_ok(*args: str) -> str | None:
    """Best-effort git call; None on failure (network/sandbox restrictions)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except Exception:  # noqa: BLE001 - best-effort resolver
        return None
    return result.stdout if result.returncode == 0 else None


def _resolve_pr233_head() -> str:
    """Resolve the REAL PR #233 head: live ref when fetchable, else the
    audited pin. MANDATORY: no silent skip."""
    for ref in ("refs/pull/233/head", "refs/remotes/origin/pr-233"):
        sha = _git_ok("rev-parse", "--verify", ref)
        if sha is not None and sha.strip():
            return sha.strip()
    _git_ok("fetch", "--no-tags", "origin", "refs/pull/233/head")
    sha = _git_ok("rev-parse", "--verify", "FETCH_HEAD")
    if sha is not None and sha.strip():
        return sha.strip()
    if _git_ok("cat-file", "-e", f"{PR233_HEAD_PIN}^{{commit}}") is not None:
        return PR233_HEAD_PIN
    pytest.fail(
        "cannot resolve PR #233 head: refs/pull/233/head is not fetchable "
        f"and the audited pin {PR233_HEAD_PIN} is not in the object store"
    )


# ---------------------------------------------------------------------------
# routing fixtures (requirement: Phase-2-only / mixed / no-Phase2 fixtures)
# ---------------------------------------------------------------------------


def test_phase2_only_fixture_routes_full_gate(tmp_path, monkeypatch):
    assert _run_scope(FIXTURE_PHASE2_ONLY, tmp_path, monkeypatch) == PHASE2_ONLY


def test_mixed_fixture_routes_mixed_regression(tmp_path, monkeypatch):
    assert (
        _run_scope(FIXTURE_MIXED, tmp_path, monkeypatch) == MIXED_WITH_PHASE2
    )


def test_no_phase2_fixture_routes_no_htf_gate(tmp_path, monkeypatch):
    assert _run_scope(FIXTURE_NO_PHASE2, tmp_path, monkeypatch) == NO_PHASE2


def test_ci_fix_pr_itself_routes_phase2_only(tmp_path, monkeypatch):
    """This CI-fix PR must itself classify PHASE2_ONLY and pass the full
    Main PR Safety gate (its files are all on the governed surface)."""
    assert _run_scope(FIXTURE_CI_FIX_PR, tmp_path, monkeypatch) == PHASE2_ONLY


def test_pr233_pinned_changed_files_route_mixed(tmp_path, monkeypatch):
    assert (
        _run_scope(PR233_CHANGED_FILES_PIN, tmp_path, monkeypatch)
        == MIXED_WITH_PHASE2
    )


def test_frozen_only_change_stays_on_the_full_fail_closed_gate(
    tmp_path, monkeypatch
):
    # a PR touching a frozen artifact is confined to the governed surface, so
    # the full gate runs and its confinement checks reject the change
    paths = ["app/research/prospective_observer.py"]
    assert _run_scope(paths, tmp_path, monkeypatch) == PHASE2_ONLY


def test_frozen_plus_outside_change_stays_frozen_guarded(tmp_path, monkeypatch):
    # requirement: a mixed PR must never bypass the frozen constraints — it
    # routes MIXED_WITH_PHASE2, whose gate runs the frozen-invariant selection
    paths = ["app/research/prospective_registry.json", "README.md"]
    assert _run_scope(paths, tmp_path, monkeypatch) == MIXED_WITH_PHASE2


# ---------------------------------------------------------------------------
# behavioral classification (real workflow script)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GOVERNED_PATHS)
def test_governed_paths_route_the_full_gate(path, tmp_path, monkeypatch):
    assert _run_scope([path], tmp_path, monkeypatch) == PHASE2_ONLY, (
        f"governed path escapes the Phase-2 scope routing: {path}"
    )


@pytest.mark.parametrize("path", UNGOVERNED_PATHS)
def test_ungoverned_paths_route_no_htf_gate(path, tmp_path, monkeypatch):
    assert _run_scope([path], tmp_path, monkeypatch) == NO_PHASE2, (
        f"unrelated path needlessly routes an HTF gate: {path}"
    )


def test_empty_change_set_routes_no_htf_gate(tmp_path, monkeypatch):
    assert _run_scope([], tmp_path, monkeypatch) == NO_PHASE2


def test_scope_match_is_case_insensitive_for_docs(tmp_path, monkeypatch):
    path = "docs/research/htf_baseline_short_phase2_ci_scope_routing_v1.md"
    assert _run_scope([path], tmp_path, monkeypatch) == PHASE2_ONLY


def test_path_separators_and_dot_prefixes_are_normalized(tmp_path, monkeypatch):
    backslashed = "deploy\\systemd\\trad-bot-htf-baseline-short-observer.service"
    dotted = "./app/research/htf_baseline_short_phase2_state_store_v1.py"
    assert _run_scope([backslashed], tmp_path, monkeypatch) == PHASE2_ONLY
    assert _run_scope([dotted], tmp_path, monkeypatch) == PHASE2_ONLY


def test_rename_pairs_route_on_both_old_and_new_names(tmp_path, monkeypatch):
    # ``git diff --no-renames`` lists a rename as delete(old) + add(new); the
    # routing must see the OLD name too (it is the governed artifact)
    rename_feed = [
        "deploy/systemd/observer-renamed.service",
        "deploy/systemd/trad-bot-htf-baseline-short-observer.service",
    ]
    assert (
        _run_scope(rename_feed, tmp_path, monkeypatch) == MIXED_WITH_PHASE2
    )


def test_missing_changed_file_feed_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "HTF_PHASE2_CHANGED_FILES", str(tmp_path / "does_not_exist")
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    with pytest.raises(FileNotFoundError):
        exec(  # noqa: S102 - executing the audited workflow heredoc is the point
            compile(_detection_script(), "main-pr-safety.yml:PY_SCOPE", "exec"),
            {"__name__": "__main__"},
        )


# ---------------------------------------------------------------------------
# MANDATORY integration test: the REAL diff of PR #233 vs origin/main
# ---------------------------------------------------------------------------


def test_pr233_real_diff_routes_to_mixed_regression(tmp_path, monkeypatch):
    """Classify the real PR #233 diff against origin/main and check the
    expected routing: MIXED_WITH_PHASE2 (frozen-invariant + HTF
    causal/parity/closed-bar regression), NEVER the Phase-2-only confinement
    gate that produced the 2 FAILs."""
    head = _resolve_pr233_head()
    diff = _git("diff", "--name-only", "--no-renames", f"origin/main...{head}")
    paths = [ln.strip() for ln in diff.splitlines() if ln.strip()]
    assert paths, f"empty PR #233 diff for head {head}"

    result = _run_scope(paths, tmp_path, monkeypatch)
    assert result == MIXED_WITH_PHASE2, (
        f"PR #233 must route MIXED_WITH_PHASE2, got {result}"
    )

    # structural proof of "mixed": governed AND ungoverned paths present
    governed = [p for p in paths if _is_governed(p, tmp_path, monkeypatch)]
    outside = [p for p in paths if not _is_governed(p, tmp_path, monkeypatch)]
    assert governed, "PR #233 must touch at least one governed file"
    assert outside, "PR #233 must touch at least one file outside the surface"
    assert PR233_KNOWN_PHASE2_PATHS <= set(paths), (
        "PR #233 no longer touches its audited Phase-2 files: "
        f"{sorted(PR233_KNOWN_PHASE2_PATHS - set(paths))}"
    )

    # expected routing: the mixed gate runs, the Phase-2-only gate does not
    assert _routed_steps_for(MIXED_WITH_PHASE2) == [MIXED_STEP]
    assert _routed_steps_for(PHASE2_ONLY) == [ISOLATION_STEP]
    mixed_block = _steps()[MIXED_STEP]
    for name in CONFINEMENT_TESTS:
        assert f"--deselect {ISOLATION_MODULE_PATH}::{name}" in mixed_block, (
            f"mixed route must not run the Phase-2-only confinement test {name}"
        )


def _is_governed(path: str, tmp_path, monkeypatch) -> bool:
    return _run_scope([path], tmp_path, monkeypatch) != NO_PHASE2


# ---------------------------------------------------------------------------
# drift guards: the workflow lists stay equal to the isolation gate's lists
# ---------------------------------------------------------------------------


def _isolation_governed_surface():
    """Read FROZEN_PATHS / ALLOWED_NEW_PREFIXES / allowed_modified from the
    isolation module source (AST, no import side effects)."""
    tree = ast.parse(ISOLATION.read_text(encoding="utf-8"))
    frozen = allowed_new = allowed_modified = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "FROZEN_PATHS" in names:
                frozen = ast.literal_eval(node.value)
            elif "ALLOWED_NEW_PREFIXES" in names:
                allowed_new = ast.literal_eval(node.value)
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "test_no_tracked_file_modified_against_base"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    names = [t.id for t in sub.targets if isinstance(t, ast.Name)]
                    if "allowed_modified" in names:
                        allowed_modified = ast.literal_eval(sub.value)
    assert frozen is not None, "FROZEN_PATHS not found in the isolation gate"
    assert allowed_new is not None, (
        "ALLOWED_NEW_PREFIXES not found in the isolation gate"
    )
    assert allowed_modified is not None, (
        "allowed_modified not found in the isolation gate"
    )
    return frozen, allowed_new, allowed_modified


def _workflow_ns(tmp_path, monkeypatch) -> dict:
    changed = tmp_path / "changed_files"
    changed.write_bytes(b"")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("HTF_PHASE2_CHANGED_FILES", str(changed))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    ns: dict = {}
    exec(  # noqa: S102 - executing the audited workflow heredoc is the point
        compile(_detection_script(), "main-pr-safety.yml:PY_SCOPE", "exec"),
        ns,
    )
    return ns


def test_workflow_frozen_set_matches_isolation_gate(tmp_path, monkeypatch):
    frozen, _, _ = _isolation_governed_surface()
    ns = _workflow_ns(tmp_path, monkeypatch)
    assert set(ns["FROZEN_PATHS"]) == set(frozen), (
        "workflow FROZEN_PATHS drifted from the isolation gate"
    )


def test_workflow_phase2_exact_paths_match_isolation_gate(tmp_path, monkeypatch):
    _, allowed_new, _ = _isolation_governed_surface()
    exact = {e for e in allowed_new if not e.endswith("_")}
    ns = _workflow_ns(tmp_path, monkeypatch)
    assert set(ns["PHASE2_EXACT_PATHS"]) == exact, (
        "workflow PHASE2_EXACT_PATHS drifted from ALLOWED_NEW_PREFIXES"
    )


def test_every_frozen_and_allowlisted_path_routes_on(tmp_path, monkeypatch):
    frozen, _, allowed_modified = _isolation_governed_surface()
    for path in (*frozen, *allowed_modified):
        assert _run_scope([path], tmp_path, monkeypatch) == PHASE2_ONLY, (
            f"isolation-governed path escapes the scope routing: {path}"
        )


def test_every_allowed_new_prefix_routes_on(tmp_path, monkeypatch):
    _, allowed_new, _ = _isolation_governed_surface()
    for entry in allowed_new:
        sample = entry if not entry.endswith("_") else entry + "sample_v1.py"
        assert _run_scope([sample], tmp_path, monkeypatch) == PHASE2_ONLY, (
            f"isolation-governed prefix escapes the scope routing: {entry}"
        )


def test_confinement_tests_are_exactly_the_pr_diff_dependent_tests():
    """Drift guard for the mixed route's --deselect set: the two confinement
    tests must be EXACTLY the isolation tests that depend on the PR diff
    (they call ``_pr_base()``). If a new diff-dependent test appears, the
    routing must be revisited before it blocks mixed PRs again."""
    tree = ast.parse(ISOLATION.read_text(encoding="utf-8"))
    diff_dependent = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_pr_base"
            for sub in ast.walk(node)
        )
    }
    assert diff_dependent == set(CONFINEMENT_TESTS), (
        "confinement set drifted: "
        f"{sorted(diff_dependent)} != {sorted(CONFINEMENT_TESTS)}"
    )


# ---------------------------------------------------------------------------
# workflow wiring (text level, dependency-free)
# ---------------------------------------------------------------------------


def _steps() -> dict[str, str]:
    chunks = re.split(r"\n      - name: ", _workflow_text())
    steps: dict[str, str] = {}
    for chunk in chunks[1:]:
        name, _, rest = chunk.partition("\n")
        steps[name.strip()] = rest
    return steps


def _routed_steps_for(cls: str) -> list[str]:
    routed = []
    for name, block in _steps().items():
        match = re.search(
            r"steps\.htf_phase2_scope\.outputs\.class == '([A-Z0-9_]+)'", block
        )
        if match and match.group(1) == cls:
            routed.append(name)
    return routed


def test_class_enum_is_pinned():
    text = _workflow_text()
    for token in (PHASE2_ONLY, MIXED_WITH_PHASE2, NO_PHASE2):
        assert token in text, f"class token {token} missing from workflow"


def test_detection_step_is_pull_request_only():
    step = _steps()["Detect HTF Phase-2 changes"]
    assert "if: github.event_name == 'pull_request'" in step
    assert "id: htf_phase2_scope" in step
    assert "git diff --name-only -z" in step
    assert "--no-renames" in step  # rename sources must route too


def test_phase2_only_gate_runs_full_isolation_without_deselect():
    step = _steps()[ISOLATION_STEP]
    assert "steps.htf_phase2_scope.outputs.class == 'PHASE2_ONLY'" in step
    assert ISOLATION_MODULE_PATH in step
    assert "--deselect" not in step  # full gate = confinement included


def test_mixed_gate_runs_frozen_invariants_and_htf_regression():
    step = _steps()[MIXED_STEP]
    assert "steps.htf_phase2_scope.outputs.class == 'MIXED_WITH_PHASE2'" in step
    # frozen-invariant selection: isolation module minus the confinement tests
    assert ISOLATION_MODULE_PATH in step
    for name in CONFINEMENT_TESTS:
        assert f"--deselect {ISOLATION_MODULE_PATH}::{name}" in step
    # HTF causal / parity / closed-bar regression
    for module in MIXED_HTF_MODULES:
        assert module in step, f"mixed route must run {module}"


def test_mixed_htf_modules_exist_and_map_causal_parity_closed_bar():
    expected_heads = {
        MIXED_HTF_MODULES[0]: "causal",
        MIXED_HTF_MODULES[1]: "parity",
        MIXED_HTF_MODULES[2]: "closed",
    }
    for module, needle in expected_heads.items():
        path = ROOT / module
        assert path.is_file(), f"mixed-suite module missing: {module}"
        head = path.read_text(encoding="utf-8")[:400].lower()
        assert needle in head, (
            f"{module} no longer looks like the '{needle}' regression module"
        )


def test_scope_routing_tests_run_on_every_pr():
    step = _steps()["Verify HTF Phase-2 scope routing"]
    assert "tests/test_htf_baseline_short_phase2_scope_routing_v1.py" in step
    # the routing contract must NOT be routed by its own (possibly broken)
    # routing decision
    assert "htf_phase2_scope.outputs" not in step
    assert "\n        if:" not in step


def test_no_fail_open_escapes_in_workflow():
    text = _workflow_text()
    for forbidden in ("continue-on-error", "|| true", "||true", "set +e"):
        assert forbidden not in text, f"fail-open escape in workflow: {forbidden}"
