# ME_R_V3_STATISTICAL_PROTOCOL_FINALIZATION_V1

**Status:** PROPOSED FOR OPERATOR REVIEW — NOT FROZEN / NOT ACTIVATED  
**Recorded:** 2026-10-14  
**Experiment:** `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`  
**Protocol:** `3.0.0-DRAFT`  
**Scope:** Research-only statistical analysis design; documentation only. This proposal does not modify capture, evaluator, scanner, paper/live, database, or service settings.

## 1. Previously approved operator decisions (PR #146)

- **Operational checkpoint:** at least 30 filled source events, 10 distinct symbols, and 5 distinct prospective OOS days. This is a pipeline-quality check, not an edge verdict.
- **First statistical checkpoint:** at least 100 filled source events, 20 distinct symbols, and 20 distinct prospective OOS days, with all three thresholds satisfied.
- **Sole PRIMARY:** `MAE_15m_050` versus its same-source baseline; the other six MAE variants are SECONDARY/exploratory.
- **Early positive verdict:** prohibited before statistical checkpoint; meeting the checkpoint does not imply an edge.

## 2. Proposed estimand and analysis population — NOT YET APPROVED

Each eligible, naturally captured, post-activation **filled source event** is one paired observation. For the sole PRIMARY variant compute `d_i = variant_net_r_normal_i - baseline_net_r_normal_i`, using the frozen common source and cost convention; estimate `mean(d_i)` with event weights equal to one. Report baseline and variant absolute Net E[R], elevated-cost sensitivity, win rate, profit factor, trigger/non-fill proportions, and effective sample distribution independently.

Primary paired inference only includes sources with valid, fully mature, unambiguous finalized baseline and exactly seven consistent finalized variant outcomes, complete candle coverage and non-null paired fields. **Do not silently omit** filled rows with ambiguous, invalid, incomplete, immature or missing outcomes: enumerate reasons and rates in an eligibility-flow table; unresolved integrity violations suspend a positive verdict. Never backfill history. A finalized paired cohort may contain multiple records per symbol/day; do not treat them as independent for uncertainty estimation.

`filled_n` is the number of eligible prospective filled source events for the checkpoints; `paired_finalized_n` is the analysis-ready subset. At minimum both checkpoint coverage and sufficient finalized paired records must be established before a statistical verdict; the precise required finalized minimum must be agreed before freeze (a 100-filled threshold alone does not guarantee 100 analyzable observations).

## 3. Proposed confidence interval — IMPLEMENTATION DETAILS PENDING

**Proposed:** two-sided percentile 95% bootstrap CI for the event-weighted PRIMARY paired mean delta, 10,000 replicates, fixed PRNG seed `20261014`. Resampling must account for repeated observations by symbol and by OOS date. A naive independent-event bootstrap is **not acceptable**.

**Method not yet frozen:** choose and validate an explicit dependence-preserving scheme (e.g., multiway symbol/date cluster bootstrap or a justified conservative block/cluster approach); document exactly how overlapping symbol-day memberships, duplicate sampled units, cluster weights, days without signals and degenerate resamples are handled. Quantify clustering and concentration. Validate the selected algorithm on deterministic synthetic fixtures before operator approval. Do not describe an unspecified 'symbol-and-day bootstrap' as if it were already executable.

Report number of symbols, days, events per symbol/day, concentration and CI instability. If independent clusters or valid resamples are insufficient, verdict is INCONCLUSIVE rather than claiming precision. No new packages are required in the production virtual environment; perform analysis in an isolated environment.

## 4. Proposed multiple comparisons

Only `MAE_15m_050` is confirmatory. The other six predeclared variants are descriptive/exploratory, with clearly labeled unadjusted estimates and uncertainty intervals, and **no independent positive-edge or confirmatory significance claims**. If any formal inferential secondary claim is desired, preregister and approve a suitable family-wise error-rate procedure (e.g. Holm adjustment) *before seeing outcomes*; otherwise secondary p-values must not be used for positive selection. Do not promote a secondary variant to PRIMARY using the same holdout.

## 5. Proposed decision and stopping framework — NOT YET APPROVED

- **Before 30/10/5:** integrity monitoring only.
- **At/after 30/10/5:** one operational review; no positive statistical conclusion.
- **At/after 100/20/20:** perform the first preregistered PRIMARY statistical evaluation *when all required paired records and integrity checks are mature*. Do not run a positive-edge test repeatedly while waiting for more observations.
- **PRIMARY positive relative-improvement candidate:** paired delta point estimate > 0 and lower bound of validated 95% CI > 0, with integrity/coverage and concentration gates satisfied. This supports relative research-proxy improvement only; **not** positive absolute strategy expectancy and **not** live-price executability.
- **PRIMARY negative relative-improvement result:** upper bound of 95% CI < 0 after a valid planned analysis.
- **INCONCLUSIVE:** CI spans/touches zero, insufficient analyzable coverage/clusters, or uncertainty is not trustworthy.
- **No automatic termination** on a negative or inconclusive result. The action following the first statistical checkpoint (close versus extend, future analysis dates/sample bounds, and any sequential-error correction) requires a **separate preregistered operator decision before freeze**. Without that decision, no further confirmatory repeated testing is authorized.

For reporting a **tradeable edge**, additionally require evidence of positive absolute variant Net E[R] after costs, execution realism/price feasibility, and a separately authorized validation; paired relative improvement alone is insufficient.

## 6. Execution-proxy limitation

The `HORIZON_END_CONDITIONAL_MAE_EXIT` uses the completed 5m horizon candle's CLOSE as a research valuation proxy. That close is only known after candle completion and **must not** be represented as a guaranteed executable fill at the same instant/price. A later feasible-order/next-open sensitivity study may be proposed, but cannot be silently substituted into this frozen research hypothesis.

## 7. Approval gates still open

- Specify and test the exact bootstrap/resampling implementation, missingness/ambiguity policy and objective concentration gates.
- Approve full stopping rules, analysis-ready finalized minimum, negative/inconclusive handling and any future looks.
- Agree on whether exploratory uncertainty intervals need multiplicity adjustment or only descriptive status (default here is strictly descriptive).
- Complete the causal executability qualification.
- Separate operator authorizations for frozen payload/fingerprint/timestamp, PostgreSQL migrations 068–070, dedicated service deployment, and activation.

**Fail closed:** `approved_for_activation=false`; no freeze timestamp or activation timestamp; runner guard stays enabled; no production runtime, database, systemd, registry, paper/live, or scanner modifications.

## References

- [Research implementation PR #145](https://github.com/eignat88/trad_bot/pull/145)
- [Approved checkpoint/role decisions PR #146](https://github.com/eignat88/trad_bot/pull/146)
- `docs/research/ME_R_EARLY_MAE_V3_OPEN_DECISIONS.md`
- `docs/research/ME_R_EARLY_MAE_V3_PROSPECTIVE_PROTOCOL_DESIGN.md`
