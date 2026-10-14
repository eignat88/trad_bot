# ME_R_V3 — two-way inference method selection: diagnostic stage

Status: **RESEARCH ONLY / METHOD NOT SELECTED / CONFIRMATORY CI BLOCKED**.

## Why this exists
PR #148's `screening_envelope_ci95` encloses separate symbol- and
day-bootstrap marginal intervals. It is **not** a validated simultaneous
two-way cluster confidence interval. A second VPS diagnostic applied
`V_symbol + V_day - V_intersection` with an unadjusted normal 1.96
critical value; that method also is **not** validated, including at small
cluster counts and when the variance estimate is nonpositive.

VPS baseline observations (research-only; not stored outcomes):

| Scenario | Original screening envelope (100 datasets, 300 reps) | Normal two-way variance CI (1000 datasets) |
|---|---:|---:|
| IID | 96.0% | 91.7% overall (1 invalid) |
| SYMBOL_SHOCK | 92.0% | 92.5% |
| DAY_SHOCK | 95.0% | 94.1% |
| CROSSED_SHOCK | 91.0% | 93.3% |

The *different simulation sample designs* and Monte Carlo sizes mean these
columns are **not a controlled head-to-head comparison**. Neither method
may be used to issue an edge verdict.

## Added diagnostic harness
`app/research/me_r_long_early_mae_v3/coverage_validation.py`:
- deterministic random (rather than rigid modular) symbol/day incidence;
- IID, symbol random effects, day random effects, crossed effects;
- imbalanced symbol/day frequency and finite-variance heavy-tail t(3)
  idiosyncratic noise, together with crossed effects;
- fixed null truth `E[paired primary-baseline delta] = 0`;
- separated dataset and resampling seeds;
- reports `coverage_all` (invalid intervals count as failures),
  `coverage_valid`, invalid counts, average CI width and Monte Carlo SE;
- explicitly labels results `DIAGNOSTIC_ONLY_NO_CONFIRMATORY_APPROVAL`.

Example (isolated worktree only, standard-library implementation):

```bash
cd /opt/trad_bot_pr148
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_me_r_early_mae_v3_statistics_proposal.py \
  tests/test_me_r_early_mae_v3_coverage_validation.py --tb=short

PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m \
  app.research.me_r_long_early_mae_v3.coverage_validation --datasets 100 --reps 300
```

## Required before a confirmatory CI is selected
1. Preregister specific candidate algorithms and their mathematical validity
   assumptions. A naive two-way variance estimate with 1.96 is *not*
   automatically a valid small-cluster 95% CI. Compare a cluster-robust
   small-sample procedure and a carefully specified two-way multiplier/wild
   bootstrap, including non-positive covariance handling.
2. Use a **paired head-to-head** harness with identical generated datasets,
   distinct bootstrap randomness and independent PRNG streams.
3. Hold back independent, predeclared scenario/seed grids for validation
   after candidate selection. Include N=100, N>=300, crossed high leverage,
   cluster imbalance, rare large returns, missingness, low day-cluster
   counts and varying symbol/day overlap.
4. Preregister tolerance, number of simulations, MC uncertainty assessment,
   and exact fail-closed criteria (including exclusion and quality gates)
   before looking at holdout outcomes.
5. Require explicit operator review/approval of formula, estimand,
   effect direction, and finalized frozen analysis-lock specification.
   Until then, `INCONCLUSIVE`; neither `POSITIVE` nor `NEGATIVE`.

No production schema, registry, signal capture, systemd, paper or live
execution is modified by this work.
