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
* a MANDATORY integration test classifying the REAL historical diff of
  PR #233 between its AUDITED pinned base/head commits (R3 P1: no moving
  ``origin/main`` and no ``refs/pull/*`` — the regression is independent of
  the PR lifecycle and keeps working after PR #233 merges into main) and
  checking the expected routing (mixed regression, never the Phase-2-only
  confinement gate), plus merge-simulation, stale-FETCH_HEAD and offline
  fallback proofs (R3 P2: fetch success is verified before anything is taken
  from a fetch, a stale ``FETCH_HEAD`` is never trusted, and the audited pin
  is used only after presence + identity verification);
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
#: AUDITED historical regression anchors (R3 P1): fixed commit SHAs of the
#: original PR #233 state. The historical regression MUST NOT use moving refs
#: (``origin/main`` becomes an ancestor container after merge #233 and the
#: three-dot diff would collapse to empty).
#: PR #233 base at audit time (its merge-base with main)
PR233_BASE_PIN = "ad7353029fd158d4a3ab037ad3e98a179a487971"
#: PR #233 head at audit time
PR233_HEAD_PIN = "e5cd31f3c1e3e172d4fc6b91c11f3929bbc00b62"
#: Phase-2 surface files PR #233 touches (the reason it routes "mixed")
PR233_KNOWN_PHASE2_PATHS = {
    "app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py",
    "tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py",
}
#: audited PR #233 changed-file list (name-only, --no-renames) between the
#: pinned base and head commits
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


def _git(*args: str, root: Path = ROOT, env: dict | None = None) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"git command failed ({result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    return result.stdout


def _git_ok(*args: str, root: Path = ROOT, env: dict | None = None) -> str | None:
    """Best-effort git call; None on ANY failure (non-zero exit, network or
    sandbox restrictions). Callers must check success before trusting output."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )
    except Exception:  # noqa: BLE001 - best-effort resolver
        return None
    return result.stdout if result.returncode == 0 else None


def _verify_commit(sha: str, *, root: Path = ROOT) -> str | None:
    """Return ``sha`` only after PRESENCE + IDENTITY verification: the object
    must exist and resolve to exactly the audited commit id."""
    if _git_ok("cat-file", "-e", f"{sha}^{{commit}}", root=root) is None:
        return None
    resolved = _git_ok("rev-parse", f"{sha}^{{commit}}", root=root)
    if resolved is None or resolved.strip() != sha:
        return None
    return sha


def _resolve_verified_commit(sha: str, *, root: Path = ROOT) -> str | None:
    """Resolve one audited commit (R3 P2):

    * presence + identity verification first (offline fast path);
    * on a miss, fetch the COMMIT BY SHA — and only if that fetch is
      SUCCESSFUL is its result ever usable;
    * ``FETCH_HEAD`` is NEVER consulted: an arbitrary stale ``FETCH_HEAD``
      left over from an unrelated/failed fetch can never substitute the
      audited commit;
    * the audited pin is returned only after presence + identity checks.
    """
    verified = _verify_commit(sha, root=root)
    if verified is not None:
        return verified
    fetched = _git_ok("fetch", "--no-tags", "origin", sha, root=root)
    if fetched is not None:
        # fetch SUCCESS verified above before trusting anything from it
        verified = _verify_commit(sha, root=root)
        if verified is not None:
            return verified
    # fetch failed (or post-fetch identity mismatch): the audited pin is used
    # only after presence + identity verification — never FETCH_HEAD
    return _verify_commit(sha, root=root)


def _resolve_pr233_regression_pair() -> tuple[str, str]:
    """Verified AUDITED (base, head) commit pair of the historical PR #233
    regression. MANDATORY: no silent skip, no moving refs, no PR refs."""
    base = _resolve_verified_commit(PR233_BASE_PIN)
    head = _resolve_verified_commit(PR233_HEAD_PIN)
    missing = [
        pin
        for pin, sha in ((PR233_BASE_PIN, base), (PR233_HEAD_PIN, head))
        if sha is None
    ]
    if missing:
        pytest.fail(
            "cannot materialize audited PR #233 commit(s) "
            f"{missing}: fetch failed and the pin is absent/unverified"
        )
    return base, head


def _changed_files_between(
    base: str, head: str, *, root: Path = ROOT, env: dict | None = None
) -> list[str]:
    """Historical changed-file list between two COMMIT SHAs (immutable
    history — never a moving ref)."""
    diff = _git(
        "diff", "--name-only", "--no-renames", f"{base}...{head}",
        root=root, env=env,
    )
    return sorted({ln.strip() for ln in diff.splitlines() if ln.strip()})


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
# MANDATORY integration test: the REAL historical diff of PR #233 between
# its AUDITED pinned commits
# ---------------------------------------------------------------------------


def test_pr233_historical_diff_routes_to_mixed_regression(tmp_path, monkeypatch):
    """Classify the REAL historical diff of PR #233 (R3 P1: between its
    AUDITED pinned base/head commits — never a moving ref, never a PR ref)
    and check the expected routing: MIXED_WITH_PHASE2 (frozen-invariant + HTF
    causal/parity/closed-bar regression), NEVER the Phase-2-only confinement
    gate that produced the 2 FAILs. Immortal: keeps passing after merge #233
    because commit SHAs are immutable history."""
    base, head = _resolve_pr233_regression_pair()
    paths = _changed_files_between(base, head)
    assert paths, f"empty PR #233 diff for {base}...{head}"
    assert paths == sorted(PR233_CHANGED_FILES_PIN), (
        "the audited PR #233 diff changed: "
        f"{sorted(set(paths) ^ set(PR233_CHANGED_FILES_PIN))}"
    )

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
# R3 P1: the historical regression survives PR #233 MERGING into main
# ---------------------------------------------------------------------------


def test_historical_regression_survives_pr233_merge_into_main(
    tmp_path, monkeypatch
):
    """Imitate main CONTAINING PR #233 (post-merge): the old moving-base
    three-dot diff collapses to empty, while the audited pinned pair still
    yields the full PR #233 diff and the same mixed routing."""
    base, head = _resolve_pr233_regression_pair()

    # isolated object store for the simulated merge commit (nothing is
    # written to the real repository; real objects are visible read-only)
    import os

    common_dir = (
        _git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    )
    sim_obj = tmp_path / "sim_objects"
    sim_obj.mkdir()
    sim_env = dict(
        os.environ,
        GIT_OBJECT_DIRECTORY=str(sim_obj),
        GIT_ALTERNATE_OBJECT_DIRECTORIES=os.pathsep.join(
            [str(Path(common_dir) / "objects")]
        ),
        GIT_AUTHOR_NAME="ci-fix-sim",
        GIT_AUTHOR_EMAIL="ci-fix-sim@local",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00 +0000",
        GIT_COMMITTER_NAME="ci-fix-sim",
        GIT_COMMITTER_EMAIL="ci-fix-sim@local",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00 +0000",
    )
    # a merge commit whose history CONTAINS the PR #233 head (simulated
    # post-merge main tip)
    merged = _git(
        "commit-tree",
        f"{head}^{{tree}}",
        "-p", base,
        "-p", head,
        "-m", "simulated merge of PR #233 into main",
        env=sim_env,
    ).strip()
    assert _git_ok(
        "merge-base", "--is-ancestor", head, merged, env=sim_env
    ) is not None, "simulation broken: merged tip must contain the PR head"

    # the OLD moving-base failure mode (what P1 fixes): once main contains
    # the PR head, main...head is empty
    assert _changed_files_between(merged, head, env=sim_env) == [], (
        "expected the moving-base diff to collapse post-merge"
    )

    # the audited pinned historical regression is unaffected
    paths = _changed_files_between(base, head, env=sim_env)
    assert paths == sorted(PR233_CHANGED_FILES_PIN)
    assert _run_scope(paths, tmp_path, monkeypatch) == MIXED_WITH_PHASE2


def test_historical_regression_never_uses_moving_refs(monkeypatch):
    """P1 wiring: every git operand of the historical regression must be a
    pinned commit SHA — no ``origin/*``, no ``refs/*``, no ``FETCH_HEAD`` —
    so Main PR Safety cannot depend on any PR lifecycle."""
    calls: list[tuple[str, ...]] = []
    real_git, real_git_ok = _git, _git_ok

    def spy_git(*args, **kw):
        calls.append(args)
        return real_git(*args, **kw)

    def spy_git_ok(*args, **kw):
        calls.append(args)
        return real_git_ok(*args, **kw)

    monkeypatch.setitem(globals(), "_git", spy_git)
    monkeypatch.setitem(globals(), "_git_ok", spy_git_ok)
    base, head = _resolve_pr233_regression_pair()
    _changed_files_between(base, head)

    for args in calls:
        joined = " ".join(args)
        for moving in ("origin/", "refs/", "FETCH_HEAD"):
            assert moving not in joined, (
                f"historical regression used moving/untrusted git operand: {args}"
            )
    diff_args = [
        args for args in calls if args and args[0] == "diff"
    ]
    assert any(
        f"{PR233_BASE_PIN}...{PR233_HEAD_PIN}" in args for args in diff_args
    ), "historical diff must run between the audited pinned commits"


# ---------------------------------------------------------------------------
# R3 P2: fetch-success gate, stale FETCH_HEAD, offline fallback
# ---------------------------------------------------------------------------


def _sim_repo_with_two_commits(tmp_path) -> tuple[str, str]:
    """Tiny synthetic repo: returns (pin_commit, other_commit) SHAs."""
    root = tmp_path / "sim"
    root.mkdir()
    _git("init", root=root)
    ident = (
        "-c", "user.name=ci-fix-sim",
        "-c", "user.email=ci-fix-sim@local",
    )
    _git(*ident, "commit", "--allow-empty", "-m", "pin", root=root)
    pin = _git("rev-parse", "HEAD", root=root).strip()
    _git(*ident, "commit", "--allow-empty", "-m", "other", root=root)
    other = _git("rev-parse", "HEAD", root=root).strip()
    return root, pin, other


def test_failed_fetch_with_stale_fetch_head_never_substitutes_the_pin(
    tmp_path, monkeypatch
):
    """P2: a FAILED fetch plus an existing stale FETCH_HEAD must never
    substitute the audited commit."""
    root, pin, other = _sim_repo_with_two_commits(tmp_path)
    # stale FETCH_HEAD from an unrelated past fetch, naming `other`
    (root / ".git" / "FETCH_HEAD").write_text(
        f"{other}\t\tbranch 'main' of https://example.invalid/repo.git\n",
        encoding="utf-8",
    )

    # (a) audited pin present: resolved to the pin, never to FETCH_HEAD
    assert _resolve_verified_commit(pin, root=root) == pin

    # (b) audited pin MISSING, fetch FAILS (no 'origin' remote): resolution
    #     fails closed instead of returning the stale FETCH_HEAD commit
    missing = "1234567890abcdef1234567890abcdef12345678"
    fetches: list[tuple[str, ...]] = []
    real_git_ok = _git_ok

    def spy(*args, **kw):
        if args and args[0] == "fetch":
            fetches.append(args)
        return real_git_ok(*args, **kw)

    monkeypatch.setitem(globals(), "_git_ok", spy)
    resolved = _resolve_verified_commit(missing, root=root)
    assert resolved is None, "missing pin must resolve fail-closed"
    assert resolved != other, "stale FETCH_HEAD must never be accepted"
    assert fetches, "the miss path must have attempted the fetch"


def test_offline_fallback_verifies_pin_without_network(tmp_path, monkeypatch):
    """The audited pins resolve OFFLINE (presence + identity only) — no
    fetch, no network, no FETCH_HEAD."""
    root, pin, _ = _sim_repo_with_two_commits(tmp_path)
    calls: list[tuple[str, ...]] = []
    real_git_ok = _git_ok

    def spy(*args, **kw):
        calls.append(args)
        return real_git_ok(*args, **kw)

    monkeypatch.setitem(globals(), "_git_ok", spy)
    assert _resolve_verified_commit(pin, root=root) == pin
    assert not any(args and args[0] == "fetch" for args in calls), (
        "offline fast path must not attempt any fetch"
    )


def test_fetch_success_is_required_before_any_fetch_result(monkeypatch):
    """P2 gate: when the fetch reports failure, nothing derived from a fetch
    may be used (FETCH_HEAD is never read); only the identity-verified pin."""
    pin = "1234567890abcdef1234567890abcdef12345678"
    seen: list[tuple[str, ...]] = []

    def fake_git_ok(*args, **kw):
        seen.append(args)
        if args and args[0] == "fetch":
            return None  # fetch FAILS — must gate everything downstream
        if args and args[0] == "cat-file":
            return None  # audited pin absent (pre- and post-fetch checks)
        if args and args[0] == "rev-parse":
            raise AssertionError(
                "fetch failed: no fetch-derived rev-parse may run (FETCH_HEAD)"
            )
        return None

    monkeypatch.setitem(globals(), "_git_ok", fake_git_ok)
    assert _resolve_verified_commit(pin) is None, "must fail closed"
    assert any(args and args[0] == "fetch" for args in seen), (
        "the miss path must have attempted the fetch"
    )
    assert not any("FETCH_HEAD" in args for args in seen), (
        "FETCH_HEAD must never be consulted"
    )

    # success path: fetch OK, then the pin is returned only after
    # presence + identity verification
    def fake_git_ok_success(*args, **kw):
        if args and args[0] == "fetch":
            return ""  # success
        if args and args[0] == "cat-file":
            return ""  # present
        if args and args[0] == "rev-parse":
            assert args[1] == f"{pin}^{{commit}}", (
                "identity check must resolve the audited pin itself"
            )
            return pin
        return None

    monkeypatch.setitem(globals(), "_git_ok", fake_git_ok_success)
    assert _resolve_verified_commit(pin) == pin


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
