# ME_R_V3_STATISTICAL_METHOD_AND_QUALITY_GATES_V1

**Status:** PROPOSAL / REQUIRES OPERATOR APPROVAL AND VALIDATION. Research-only; no capture, migration, freeze, registry or VPS changes.

## Existing approvals (unchanged)

- Operational checkpoint: >=30 filled events, >=10 symbols, >=5 OOS days; integrity review only.
- One confirmatory PRIMARY: `MAE_15m_050`, paired normal-cost Delta Net E[R] against its shared baseline.
- Statistical checkpoint: >=100 filled, >=20 symbols, >=20 OOS days **and >=100 valid paired FINALIZED source events**, all satisfied.
- Exactly **one** confirmatory analysis at a preregistered locked extract. Six other variants are descriptive exploratory only. No early positive edge verdict, no repeated tests to seek significance.

## Explicit proposed statistical computation

Standalone Python module: `app/research/me_r_long_early_mae_v3/statistics_proposal.py` (stdlib only, not imported by pipeline/runner).

One observation is a unique, fully validated filled source with the **seven complete variant tuples** and matching baseline, after the separately approved activation cutoff. Define `delta_i = variant_normal_net_r_i - baseline_normal_net_r_i`. Estimand: simple event-weighted mean paired delta.

Two one-way clustered nonparametric bootstrap calculations are implemented **as preliminary sensitivity diagnostics**, not a validated joint two-way confidence procedure:

1. Group events by **symbol**, then sample symbols with replacement; include all events of every selected symbol, preserving within-symbol duplication and using the event-weighted mean of the resulting observations.
2. Independently group events by **UTC OOS day**, then sample days with replacement, preserving within-day dependence.
3. For each grouping use 10,000 replicates, seed 20261014 for symbols and seed+1 for days, sorted source IDs/cluster keys, percentile order statistics with linear interpolation at 0.025 and 0.975.
4. Report both marginal 95% intervals plus the enclosing interval from their outer endpoints. **The envelope is a conservative sensitivity screen, NOT a demonstrated 95% simultaneous/multiway confidence interval.** No POSITIVE/NEGATIVE confirmatory verdict may depend on it until a correct joint-dependence method is selected, calibrated and approved.

This separate-cluster envelope avoids pretending that symbol/day crossed dependence is solved. Future work: specify/test a valid multiway-cluster resampling (or demonstrate conservatism and adequate coverage with a preregistered Monte Carlo simulation design) before freezing a confirmatory CI. The simulations must include common date shocks, persistent symbol shocks, unbalanced symbol-day cell counts, sparse clusters and heavy tails. Predeclare acceptance criteria before simulation. No production Python packages are required.

## Quality gate proposal — NOT YET OPERATOR APPROVED

Input must be an immutable as-of extract with source IDs, symbol, UTC day, source detection/activation times, entry fill state, baseline maturity, all 7 outcome statuses, ambiguity and candle-coverage fields, paired net R and normal/elevated costs. Reconcile **all** detected sources through filled, immature, ambiguous, invalid, coverage incomplete, finalized and excluded states. No silent drop, duplicate IDs, synthetic history or pre-activation sources. Persist hashes of extract/commit/protocol/SQL query, as-of timestamp and exclusion flow.

Count gate is fully approved: >=100 valid paired FINALIZED and other thresholds. The additional integrity and concentration cutoffs are **not yet approved**; do not invent a permissible missing/ambiguous percentage. Report rates, 20 largest symbol shares, daily shares, cluster counts, and sensitivity to exclusion patterns. Any unexplained mismatch or source-candle coverage violation blocks favorable verdict. Clusters with only one unique symbol/day cannot produce an interval.

If a single source has partially finalized variants, mark it nonfinal for confirmatory analysis and provide a diagnostic count. Report relative paired delta separately from absolute variant Net E[R], as well as elevated-slippage sensitivity. The completed horizon 5m CLOSE remains a research valuation proxy, not an executable price guarantee.

## One-look analysis lock — proposed

When >=100/20/20 filled-symbol-day thresholds AND >=100 paired finalized are met, record a fixed UTC analysis `as_of`, immutable source ID cohort, protocol fingerprint, query hash and analysis code commit. Enforce source-event timestamps post activation, all horizons mature at as-of, no subsequent additions or re-query during confirmatory evaluation. If checks fail, defer the **single** analysis without looking at outcome-dependent positive verdicts. Operator must preapprove what happens after NEGATIVE or INCONCLUSIVE and any subsequent research cohort (not another peek at same holdout).

## Proposed verdict (not approved or executable)

- Positive relative research improvement candidate only when the eventual validated confirmatory 95% CI lower bound >0 and the point mean >0 and all quality gates pass.
- Negative relative-improvement result if validated upper bound <0 at the single predeclared look.
- Inconclusive if the interval includes zero, data quality/sufficient independence fail or the method cannot establish reliable coverage.
- This does **not** establish profitable strategy absolute Net E[R] nor implementable trading edge.
- Current sensitivity module emits only `SENSITIVITY_ONLY_NOT_APPROVED_CONFIRMATORY_CI` and never a positive-edge verdict.

## Deterministic tests

`tests/test_me_r_early_mae_v3_statistics_proposal.py` uses synthetic data; checks thresholds, paired-finalized count, reproducibility/invariance under input permutation, envelope bounds, invalid/duplicate data failure, degenerate clusters and dependency-free isolation. The tests do not access PostgreSQL or real trades.

Local developer invocation (not VPS):

```bash
python -m pytest -q -p no:cacheprovider tests/test_me_r_early_mae_v3_statistics_proposal.py
```

**Pending before freeze:** approve/validate genuine two-way CI method, quantitative quality gates, missingness sensitivity policy, analysis-lock exact DB projection, and full negative/inconclusive stopping disposition. No migrations 068–070, service installation, runner activation or production database writes are permitted by this proposal.
