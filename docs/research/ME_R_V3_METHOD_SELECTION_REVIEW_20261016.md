# ME_R V3 — independent methodological review of PR #163

**Decision: REVIEW_COMPLETE / HOLDOUT_NO_GO / CONFIRMATORY_METHOD_NOT_SELECTED.**
Scope: synthetic comparison only; no OOS outcomes, no database, no VPS writes.

## Verified properties

1. `paired_candidate_comparison.compare` generates one `pairs` dataset per scenario/seed/n and evaluates both candidates on **that same dataset**.
2. `primary_interval` uses a separate seeded random stream for bootstrap replications. The two-way t candidate is deterministic on a given sample. Reported seed constants differ between pilot and reserved holdout.
3. `coverage_all` counts invalid intervals as failures. `coverage_valid` is distinct and cannot replace it. Code uses `NO_METHOD_APPROVED` regardless of the screen results.
4. Local retrospective pilot `20 datasets × 100 bootstrap reps × 12 cells` is explicitly **diagnostic**, not a power-calibrated holdout. A single failed unit-test fixture (`reps=10`) was corrected and 23 tests passed at HEAD `4e098285`.

## Methodological blocking findings

**M1 — CI mathematical validity is unproven (BLOCKING).** The envelope of two marginal clustered percentile intervals is not known to be a joint two-way 95% confidence interval. The `two_way_t` inclusion/exclusion sandwich estimate with `min(G_symbol,G_day)-1` Student-t critical is a heuristic, not a general finite-sample validity result. Simulated coverage in a finite set of DGPs alone cannot upgrade either to a universally valid 95% CI. An explicit estimator/critical-value theoretical justification and its assumptions are required.

**M2 — proposed gate is not calibrated as a multiple-cell approval rule (BLOCKING).** The rule `Wilson lower 95% >= 0.93` in each of 12 cells is an engineering tolerance proposal, NOT evidence that true coverage is >=0.95. It allows the lower bound to be below the nominal target by two percentage points. Twelve marginal 95% bounds also do not provide simultaneous 95% assurance. Zero invalid intervals in a finite Monte Carlo sample does not prove zero failure probability. Before accepting a statistical method specify the target minimum true coverage, a predeclared familywise/simultaneous error strategy, Monte Carlo precision and acceptable invalid rate separately.

**M3 — test scenarios omit key real-cohort failure modes (BLOCKING).** Fixed 20 symbol and 25 day sampling pools are used, while *realized* distinct cluster counts can be lower and overlap cells highly uneven. No systematic grids for sparse incidence, near-degenerate/tiny cluster counts, low symbol/day overlap, heterogeneous symbol-day cell leverage, dependence strength, jumps/outliers, different cluster sizes, exclusions dependent on outcomes, or data-quality missingness. Source-event-weighted mean under selective finalization may not estimate the intended population estimand. Add independently designed scenario families before selecting a method.

**M4 — independent validation separation remains weak (BLOCKING).** Pilot/holdout differ only by seed on the **same** six DGPs. The proposed benchmark and its threshold have already been inspected after a pilot. Thus seed separation alone does not guarantee an unbiased method-selection validation. Use a fresh, sealed external validation scenario set, preregistered before examining it, distinct from the development DGPs. New methods / tuning require new sealed validation.

**M5 — operational and computational lock missing (BLOCKING).** The CLI permits `--mode holdout` at any time, with no sealed design hash, locked implementation commit, immutable output manifest or single-run lock. The operator must approve an immutable design/code commit, reserve distinct seeds/scenario specifications, cap resources, and require one authorized holdout execution; outputs should capture SHA256 of code, protocol, scenario configuration and report. Ensure report records invalid reasons and full inclusion/exclusion accounting.

## What the existing evidence allows

- **Unit regression:** 23/23 pass for the selected four statistical test files in isolated validation worktree, per VPS output.
- **Synthetic pilot:** informative only; `n_datasets=20` per cell gives very wide uncertainty. Observed 80–100% coverage cannot discriminate ~95% coverage with adequate precision. Each candidate's `screening_gate_pass=false` is expected because Wilson lower bounds are too low with only 20 samples; it is not a candidate rejection under the preregistered 1,000-dataset design.
- **No winner**, no valid two-way confirmatory CI, no positive/negative relative-edge verdict, no absolute profitability claim.

## Required next research task

`ME_R_V3_TWO_WAY_INFERENCE_PROTOCOL_REDESIGN_V1`:
1. State precisely the target estimand, sampling unit, crossed dependence and assumptions for confirmatory inference.
2. Specify two-way candidate(s) with mathematically justified variance and critical-value or bootstrap, including nonpositive variance handling.
3. Preregister new DGP families covering M3, methodology assumptions and a held-out scenario grid; decide familywise coverage criterion and Monte Carlo budget **before** results.
4. Add a locked scenario/code/report manifest and fail-closed holdout permission workflow.
5. Run **development** simulations and unit tests, independent methodological review; only with operator approval run a one-time independent holdout.
6. Preserve PR #148 and PR #163 in Draft and do not merge or activate OOS on the basis of coverage pilot.

**Disposition:** `HOLDOUT_NO_GO`, `METHOD_NOT_APPROVED`, `NO_MERGE`, `NO_DEPLOY`, `NO_OOS_ACTIVATION`.
