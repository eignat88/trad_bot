# ME_R V3 — cluster concentration and provisional inference gates

Status: **RESEARCH / DRAFT / NO CONFIRMATORY APPROVAL**.

## Metrics

For event-weighted, paired-finalized outcomes, compute empirical symbol
and OOS-day cluster sizes, maximum and top-two event shares, Herfindahl
concentration HHI = sum(cluster_share^2), and descriptive effective count
= 1/HHI. Also report crossed symbol/day cell count, incidence density
(observed distinct cells / all possible symbol-day cells), and maximum
observations in one cell.

These are **descriptive concentration metrics**, not a substitute for
statistical effective sample size or a theorem establishing CI validity.

## Provisional engineering screen, not preregistered threshold

`structural_quality_gate` reports `INCONCLUSIVE` if distinct symbols or
days are below 20 or either HHI-based effective cluster count is below 10.
These are explicitly **proposed** thresholds, not empirically validated
or frozen criteria. Passing yields `PROVISIONAL_SCREEN_ONLY`, never
`APPROVED`, `POSITIVE`, `NEGATIVE` or permission to run OOS.

A screen failure may be explored diagnostically but cannot yield a
positive-edge inference. The screening threshold must not be tuned to
the previously observed 80%/82% coverage on unbalanced scenarios and
then claimed as independent validation.

## Evidence and remaining blockers

Prior matched 100-dataset tests found 80% coverage for the two-way
Student-t candidate in the deliberately unbalanced crossed setting;
the envelope gave 82%. These results motivated a structural analysis,
but do **not** identify a valid exclusion threshold.

Before confirmatory use:
- Freeze the analysis cohort, estimand, alternative, one-analysis stopping
  rule, dataset quality/missingness and structural thresholds.
- Validate candidate CI and fail-closed strategy on **independent**
  preregistered Monte Carlo grids (balanced, crossed, highly concentrated,
  heavy-tailed, n=100 and larger, sparse repeated cells, non-ideal overlap).
- Report coverage across **all generated datasets**, counting invalid
  intervals as failure (and separately tabulate invalid rate). Report
  conditional-on-screen coverage and percentage of screens failed;
  conditionally covered subcohorts alone cannot establish unconditional
  95% coverage.
- Protect against selecting a method based on the existing diagnostic
  scenarios; use fresh holdout seeds and prespecified acceptance criteria.
- Require explicit operator approval before inference and deployment.

No production code, schema, registry, live/paper execution, or services
are modified by this draft.

### VPS isolated verification

```bash
cd /opt/trad_bot_pr148
git status --short
git fetch --no-tags origin research/me-r-v3-statistical-method-quality-gates-v1
git switch --detach 50b60da94f82e4b0ed1d6325c01eaf80dc44bb54
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_me_r_early_mae_v3_statistics_proposal.py \
  tests/test_me_r_early_mae_v3_coverage_validation.py \
  tests/test_me_r_early_mae_v3_two_way_candidate.py \
  tests/test_me_r_early_mae_v3_concentration_diagnostics.py --tb=short
```
