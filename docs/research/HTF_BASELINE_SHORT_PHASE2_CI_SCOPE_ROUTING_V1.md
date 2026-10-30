# HTF Phase-2 CI Scope Routing V1 (Main PR Safety)

Task ID: `MAIN_PR_SAFETY_HTF_PHASE2_SCOPE_FIX_V1` (round R2)
Branch: `ci/main-pr-safety-htf-phase2-scope-routing-v1`
Workflow: `.github/workflows/main-pr-safety.yml` (`Main PR Safety`, PR gate for `main`)
Scope of this change: **CI workflow + routing tests + this document only.** No
application code, no frozen protocol (`tests/test_htf_baseline_short_phase2_isolation_v1.py`
is untouched), no Git history, no branch protection. Commit/push/merge/deploy
are prohibited here (operator owns commit/push).

## 1. Problem

### R1 — fail-open substring routing (fixed in R1)

The `Detect HTF Phase-2 changes` step routed PRs with one substring rule
(`'htf_baseline_short_phase2' in p.lower()`), so the hyphenated observer unit,
the gate workflow itself, the frozen Point-B stack and rename sources escaped
the gate entirely. R1 made routing fail-closed over the whole governed surface.

### R2 — Phase-2-only confinement used as a universal gate (the PR #233 blocker)

R1 still treated **every** PR with a Phase-2 hit as a Phase-2-**only** PR and
ran the full `tests/test_htf_baseline_short_phase2_isolation_v1.py`. That
module's two surface-confinement tests

* `test_no_tracked_file_modified_against_base`
* `test_new_files_are_confined_to_phase2_surface`

are only meaningful for PRs **confined** to the Phase-2 surface. PR #233
(issue #229 Bybit retry budget work, 19 files) touches exactly two Phase-2
files (`app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py`,
`tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py`) mixed with
unrelated Bybit work, so the confinement tests failed with the same **2 FAIL**
and blocked the PR.

## 2. Fix — fail-closed three-way routing

`Detect HTF Phase-2 changes` classifies the changed-file list (`--no-renames`,
NUL-separated, strict read — a broken feed fails the step, never "false"):

| class | condition | gate |
| --- | --- | --- |
| `PHASE2_ONLY` | every changed file is governed (Phase-2 surface ∪ frozen Point-B stack ∪ gate workflow) and ≥1 governed file | **full** Phase-2 isolation gate (15 tests), including the two surface-confinement tests |
| `MIXED_WITH_PHASE2` | ≥1 governed file AND ≥1 file outside the surface | frozen-invariant regression (the isolation module **minus** the two confinement tests) + HTF causal/parity/closed-bar regression |
| `NO_PHASE2` | no governed file | no HTF gate |

Governed surface (mirrors `FROZEN_PATHS`, `allowed_modified`,
`ALLOWED_NEW_PREFIXES` of the isolation module; drift-guarded by tests):

* `htf_baseline_short_phase2`-named paths (code/tools/tests/sql/docs,
  migrations 074/075);
* `deploy/systemd/trad-bot-htf-baseline-short-observer.service` (hyphenated);
* the frozen Point-B stack (`FROZEN_PATHS`, 8 files);
* `.github/workflows/main-pr-safety.yml` (the gate verifies its own routing).

MIXED_WITH_PHASE2 suite (requirement: HTF causal/parity/closed-bar +
frozen-invariant regression):

| selection | module / tests | role |
| --- | --- | --- |
| frozen-invariant | `tests/test_htf_baseline_short_phase2_isolation_v1.py` minus the 2 confinement tests (13 tests) | frozen Point-B byte-identity, FIX-5/FIX-7 exception verifiers, branch-semantics pins, static guards |
| causal | `tests/test_htf_baseline_short_phase2_causal_contracts_v1.py` | causal 5m/1H contracts, ATR, no look-ahead |
| parity | `tests/test_htf_baseline_short_episode_id_parity_asof_batch_v1.py` | parity matching / ASOF batches |
| closed-bar | `tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py` | closed-Bybit 5m source: forming candle never used, confirmed closure |

### Frozen protection is NOT weakened (requirement)

* a PR touching any frozen file always routes to a gate (frozen files are
  governed): frozen-only → `PHASE2_ONLY` (full gate rejects it), frozen +
  outside → `MIXED_WITH_PHASE2` (the 13 frozen-invariant tests reject it);
* `NO_PHASE2` by construction means the PR touches no frozen file;
* drift guard: the two deselected tests are exactly the isolation tests that
  depend on the PR diff (`_pr_base()`), and the workflow's `FROZEN_PATHS` /
  `PHASE2_EXACT_PATHS` stay equal to the isolation module's lists.

## 3. Routing tests

`tests/test_htf_baseline_short_phase2_scope_routing_v1.py` executes the **real**
classification script extracted from the workflow heredoc and pins:

* one fixture per route (`FIXTURE_PHASE2_ONLY`, `FIXTURE_MIXED`,
  `FIXTURE_NO_PHASE2`) + this CI-fix PR's own file set (it routes
  `PHASE2_ONLY` and passes the full gate itself);
* **mandatory integration test** `test_pr233_real_diff_routes_to_mixed_regression`:
  classifies the REAL PR #233 diff against `origin/main` (live
  `refs/pull/233/head` when fetchable, else audited head pin
  `e5cd31f3c1e3e172d4fc6b91c11f3929bbc00b62`; no silent skip) and checks the
  expected routing: `MIXED_WITH_PHASE2`, mixed gate runs, Phase-2-only
  confinement never runs on it;
* frozen fail-closed fixtures (frozen-only → full gate, frozen+outside →
  frozen-guarded mixed route);
* drift guards (workflow lists vs isolation module; confinement set = PR-diff
  -dependent tests; mixed-suite module mapping stays causal/parity/closed-bar);
* workflow wiring: class-routed gate steps, `--no-renames`, unconditional
  routing-test step, no fail-open escapes.

## 4. Verification (regression on this worktree)

| Suite | Result |
| --- | --- |
| `tests/test_htf_baseline_short_phase2_scope_routing_v1.py` (incl. PR #233 integration test) | **56 passed** |
| `tests/test_htf_baseline_short_phase2_isolation_v1.py` (PHASE2_ONLY route, full gate) | **15 passed** |
| mixed suite on this tree (workflow MIXED command verbatim) | **59 passed, 2 deselected** |
| `tests/test_paper_safety_gate_config.py` (workflow baseline step) | **6 passed** |
| workflow YAML parse (`yaml.safe_load`) | OK |

### Evidence on the REAL PR #233 tree (issue #229)

PR #233 head `e5cd31f3c1e3e172d4fc6b91c11f3929bbc00b62` extracted via
`git archive` into ignored `.cache/pr233_tree/` with an isolated worktree
gitdir (detached HEAD at the PR head, own index) and the workflow's MIXED
command run verbatim against PR #233's own content:

| Run on PR #233 content | Result |
| --- | --- |
| mixed gate (13 frozen-invariant + causal + parity + closed-bar) | **59 passed, 2 deselected** |
| the 2 confinement tests alone (negative control = old universal gate) | **2 failed** — exactly the reported "2 FAIL" (`test_no_tracked_file_modified_against_base`: 10 tracked files outside the surface; `test_new_files_are_confined_to_phase2_surface`: 9 new files outside the surface) |

So PR #233 passes the new CI routing (`MIXED_WITH_PHASE2` → frozen-invariant +
HTF causal/parity/closed-bar regression, all green on its content) while the
Phase-2-only confinement gate remains intact for Phase-2-only PRs and the
frozen constraints remain fail-closed for every class.

## 5. Changed files

| File | Change |
| --- | --- |
| `.github/workflows/main-pr-safety.yml` | three-way fail-closed classification (`PHASE2_ONLY`/`MIXED_WITH_PHASE2`/`NO_PHASE2`), class-routed gate steps (full isolation vs frozen-invariant + HTF causal/parity/closed-bar), `--no-renames`, unconditional routing-test step |
| `tests/test_htf_baseline_short_phase2_scope_routing_v1.py` | routing tests: 3 fixtures, PR #233 real-diff integration test, frozen fail-closed fixtures, drift guards, workflow wiring |
| `docs/research/HTF_BASELINE_SHORT_PHASE2_CI_SCOPE_ROUTING_V1.md` | this document (R1 + R2) |

## 6. Status

`CI_SCOPE_FIX_R2_PREPARED / READY_FOR_INDEPENDENT_REVIEW`

Commit/push is performed by the operator; merge/deploy is prohibited.
