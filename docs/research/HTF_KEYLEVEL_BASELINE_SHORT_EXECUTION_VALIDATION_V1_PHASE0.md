# HTF_KEYLEVEL_BASELINE_SHORT_EXECUTION_VALIDATION_V1 — Phase 0 audit and protocol gate

Issue: #170. Branch: `research/htf-keylevel-baseline-short-execution-validation-v1`.
Source experiment: `HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE`; direction **SHORT only**.
Source capture boundary: **signal_time > 2026-10-06T14:00:00Z**. This is not a new execution freeze.

## Verified from main at 1aa25454b0042aa9436765a14c9a600adb442d8b

- `app/research/htf_keylevel_point_b.py` implements a causal HTF key-level detector, with 1H key-level context and closed 5m execution/observation candles. Its baseline is produced from the touch/reaction lineage; it must **not** inherit Point-B entry confirmation.
- `sql/migrations/058_htf_point_b_prospective_registration.sql` registers the baseline as **BASELINE_CONTROL**, `position_sizing=shadow_only`, with `fee_assumption='none; observational control only'`. Entry, invalidation and target are detector references; they are **not verified executable fills**.
- `app/research/prospective_evaluator.py` evaluates path and the STOP_FIRST intrabar collision policy. This source does not by itself prove exchange execution chronology, completeness, fees or overlapping-trade eligibility.

## User-provided, as-of 2026-10-19 checkpoint (not rerun against production DB)

Baseline SHORT finalized N=7,309; symbols=94; OOS dates=14; gross AvgR=+0.2605; gross PF=1.6264; naive per-observation bootstrap CI [+0.2327,+0.2894]. The gross cohort is a research reference only, **not net P&L or cash profitability**. Do not use these results to fit geometry, select fees or tune an execution protocol.

## Proposed independent execution protocol — NOT YET FROZEN / NOT YET ACTIVATED

A separate approval/immutability checkpoint is required before execution simulation is labeled confirmatory. Until verified, all output is **discovery/audit only**.

1. **Source / identity:** read-only post-freeze BASELINE SHORT `prospective_observation` and final `prospective_outcome`. Deduplicate by observation ID and stable source identity. Confirm detector `signal_time` is known **after** qualifying 5m candle close (careful: candle timestamps are opens), and document the exact first tradable time.
2. **Entry:** no filled trade at already-known signal close by assumption. Use *next eligible actual* 5m candle opening quote as proxy, only if the corresponding candle is present/complete. A bar-open is a simulation proxy, not a guaranteed Bybit fill. If exact eligible candle or chronology is missing => `SOURCE_INCOMPLETE`, not an extrapolated fill.
3. **Stops/targets:** source structural SHORT invalidation and detector target must be directional (`stop > filled entry > target > 0`), else reject with reason. Changing R after modeled fill must be explicit; never silently retain source normalization for a different entry. Confirm whether the prospective `target_1` matches 2R from the actual baseline reference; do not silently force 2R when source geometry differs.
4. **Paths:** replay strictly chronologically over complete contiguous 5m bars through 240m after modeled fill. For SHORT: `bar.high >= stop` is SL; `bar.low <= target` is TP. Same candle => **STOP_FIRST**. If fill-bar high/low conflicts with fill timing, mark ambiguous/reject or use conservative stop-first; never claim a certain TP. Explicitly implement timeout close semantics and boundary candle eligibility.
5. **Trading friction:** obtain account/exchange-specific verified taker/maker rates; until verified, treat previous research convention **0.055% taker/side + 0.05% slippage/side** as an *illustrative stress scenario*, not an observed rate. Also test elevated **0.10% slippage/side**, with `fee + slip` assessed entry and exit and translated to R using fill-based price risk. Include funding (if applicable) and tick/lot/min-notional constraints or explicitly mark these as unavailable and no cash verdict.
6. **Concurrency:** reconstruct occupancy per position using execution entry/exit timestamps and not observation count. Model one-position-per-symbol, cooldown (document chosen assumption), max 10 simultaneous positions as research assumptions only. Deterministic chronological order for ties; count dropped signals; portfolio equity/risk/margin/drawdown, and stress cap.
7. **Robustness:** day/symbol concentration; leave-one-day-out; cluster/bootstrap day and symbol, overlap effect, volatile-regime sensitivity. Fourteen calendar dates are weak regime coverage regardless of high signal N.
8. **Financial examples:** balances $100/$1,000/$5,000 and risk fractions 0.25%/0.5%/1% *only after* tradable min size, leverage/capacity, stop distance and after-cost fills are verified. Otherwise suppress dollar projection.

## Hard blockers before any executable verdict

- Candle-by-candle as-of availability and full path coverage cannot yet be certified from this document.
- Exchange fee schedule / spread, tick and lot sizes / min notional are not independently verified.
- Confirmatory protocol must be frozen **before** a fresh untouched execution holdout; source OOS data already inspected cannot become a new execution-validation holdout retroactively.
- `is_final` from source path evaluator does not establish simulation entry/fill eligibility.

## Next implementation gates (separate reviewed commits)

- [ ] Write read-only source integrity extractor: as-of timestamp, observation IDs, risk and identity checks, missingness histogram, zero DB writes.
- [ ] Implement pure execution simulator on supplied historical candle rows with conservative fill/STOP_FIRST/timeouts; unit tests for negative risk, gaps, collision, short sign and next-bar sequencing.
- [ ] Implement costs in R, realistic portfolio overlap and cash eligibility checks.
- [ ] Produce trade-level CSV, deterministic report MD+JSON, code SHA/input snapshot/assumptions and uncertainty.
- [ ] Independently freeze protocol and, if credible, gather a **new prospective execution holdout** for confirmatory edge; no paper/live enablement.

## Safety

Research-only; do not touch scanner, paper, evaluator runtime, config, OOS registry, DB migration, systemd, or production data. No deployment, PR merge or main changes as part of the Phase 0 audit. Status: **PHASE_0_AUDIT_STARTED / NET_EDGE_NOT_VALIDATED**.
