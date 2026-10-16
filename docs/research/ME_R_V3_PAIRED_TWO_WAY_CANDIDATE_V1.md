# ME_R V3 — paired two-way candidate (exploratory)

Status: **DRAFT / NEVER CONFIRMATORY / NO FREEZE**.

Adds:
- `two_way_candidate.py`: event-weighted paired mean; inclusion-exclusion
  cluster variance by symbol + day - symbol/day intersection; Student-t
  critical with degrees-of-freedom proxy `min(symbols,days)-1` capped at
  t(30); refuse <10 distinct symbols or days, duplicate source IDs,
  non-finite deltas, nonpositive/non-finite variance.
- `paired_candidate_comparison.py`: runs unchanged envelope and the
  new candidate on **exactly the same independently generated datasets**.
  Any invalid CI is counted as a coverage failure in `coverage_all`.
  Prints coverage, Monte Carlo uncertainty, widths, and invalid rates.
- `tests/test_me_r_early_mae_v3_two_way_candidate.py`: deterministic,
  cluster count, degenerate variance, identity and paired comparison tests.

**Do not infer validation from the label 'two-way'.** The t-df proxy and
inclusion-exclusion estimator are heuristic here. Simulation results
must be inspected before choosing a method. Heavy tails, imbalance,
few overlapping clusters, dependence structure, other sample sizes
and real-cohort missingness may lead to undercoverage. No POSITIVE
or NEGATIVE verdict can be produced from these diagnostics.

VPS isolated worktree procedure (no DB, systemd or migrations):
```bash
cd /opt/trad_bot_pr148
git status --short
git fetch --no-tags origin research/me-r-v3-statistical-method-quality-gates-v1
git switch --detach 47c5ac5291d69e585610eeec45a8b070436763b5
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m pytest -q -p no:cacheprovider \
 tests/test_me_r_early_mae_v3_statistics_proposal.py \
 tests/test_me_r_early_mae_v3_coverage_validation.py \
 tests/test_me_r_early_mae_v3_two_way_candidate.py --tb=short
PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python -m \
 app.research.me_r_long_early_mae_v3.paired_candidate_comparison \
 --datasets 100 --reps 300
```

All output is **diagnostic**. Confirmatory candidate selection,
predeclared holdout simulations, tested inferential properties,
final quality gates and operator authorization remain pending.
