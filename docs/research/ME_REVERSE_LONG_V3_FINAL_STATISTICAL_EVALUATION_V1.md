# ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1

**Experiment:** `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS`
**Task:** final statistical evaluation of the prospective OOS experiment (issue #183)
**Mode:** READ-ONLY production VPS/PostgreSQL + reproducible offline analysis
**Audit timestamp (DB extraction):** `2026-10-22T17:11:24Z` (server `now()` on production `trad_bot`)
**Git state:** production VPS `/opt/trad_bot` HEAD = `362c217b90e5e72006f8f10006f201cd8fb5c4db` (clean worktree); GitHub `origin/main` = `224f1d038751e8e8a2dfba6705964c1aff6966fa`
**Report generated:** 2026-10-22 (UTC)

---

## 0. Executive verdict

| Item | Result |
|---|---|
| **Final research verdict (exactly one)** | **`NEW_HYPOTHESIS_REQUIRED`** |
| Integrity verdict | `CLEAN` — 20/20 checks PASS; all 22 operator SQL-audit values reproduced exactly |
| Statistical verdict | `EXPLORATORY_NEGATIVE_PASS_NO_EDGE` (`STATISTICAL_METHOD_NOT_APPROVED` — no confirmatory verdict can be issued) |
| Recommended lifecycle | Keep `prospective_experiment.status = RUNNING` **unchanged**; submit a **separate, operator-approved** closure task if closure is desired; paper/live remain **BLOCKED**; no threshold/protocol changes |
| Master ME OOS tracker suggestion | `CLOSED` (pending operator approval — suggestion only, not applied) |

**One-line summary.** The frozen PASS rule (`exhaustion_magnitude ≤ 0.4`) has **no positive executable edge** in the frozen OOS cohort: PASS Net E[R] = **−0.2817 R** (N=107, elevated cost −0.3216 R, Net PF 0.306), every single-axis exploratory 95% interval lies **entirely below zero**, the result survives every robustness cut, and PASS even underperforms the frozen aggregate CONTROL baseline (−0.2050 R). REJECT is higher than PASS (+0.0780 R, N=29, exploratory), i.e. the frozen threshold direction is contradicted on this cohort. Because no operator-approved negative decision gates or approved inference method were ever frozen for this experiment, this report deliberately does **not** issue `CLOSED_NEGATIVE_RECOMMENDED`; closure must be a separate approved lifecycle task, and any further research requires a **new, separately frozen hypothesis**.

**Why not the other verdicts**

* `CLOSED_NEGATIVE_RECOMMENDED` — requires *approved* negative-closure criteria. The frozen registry entry for V3 contains only sample-size gates (`minimum_n=100`, `minimum_symbols=10`); there are **no** pre-registered economic/CI/stopping gates and no approved statistical method (`STATISTICAL_METHOD_NOT_APPROVED`). Blocker documented (§1.4). Had approved negative gates existed, the evidence would meet them comfortably.
* `INCONCLUSIVE_CONTINUE_OOS` — not warranted: this is not a sample-size-limited result. The pre-registered readiness gates are met and the effect is large, stable and sign-consistent across days, symbols, regimes, cost models and exit-maturity cuts. Continuing OOS accumulation under the frozen protocol has negligible marginal value.
* `PIPELINE_OR_METHOD_FIX_REQUIRED` — not warranted: the frozen pipeline passed every integrity check (20/20), the join is 1:1 with zero orphans/duplicates/contamination, and all operator-audit values reproduce exactly. The methodology gap (no approved inference method) is a *protocol design omission*, not a pipeline defect; it is recorded as a blocker to confirmatory language, not as a violation.

---

## 1. Stage 1 — Frozen protocol verification

### 1.1 Sources located

| Element | Source | Value |
|---|---|---|
| Experiment id / version | `app/research/prospective_registry.json` + `research.prospective_experiment` (DB) | `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS`, version 1 |
| Experiment type | registry + DB | `PROSPECTIVE_EXECUTION_SHADOW` (research-only shadow, no orders) |
| Source scanner / direction | `app/research/me_reverse_long_v3/protocol.py` | `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` / `LONG` |
| **Frozen threshold** | `protocol.py::CANDIDATE_THRESHOLD` + DB `threshold` | **0.4** (explicitly "candidate research split, NOT an independently validated trading gate") |
| FREEZE_TS | registry `freeze_ts` | `2026-10-13T12:37:03Z` |
| DB activation boundary | `research.prospective_experiment.started_at` | `2026-10-13T12:37:03Z` (= freeze; required match per `activation.py::is_v3_evaluator_activated`) |
| Eligibility rule | `protocol.py::is_post_activation` | `signal_time > freeze_ts` (strict; exact freeze instant ineligible; missing/invalid freeze fails closed) |
| Entry rule | registry `entry_rule` | `NEXT_OPEN_5M` — opening price of the first strictly later 5m execution candle (hypothetical shadow fill, not executable proof) |
| Stop / target | `protocol.py` `EXPECTED_STOP_PCT` / `EXPECTED_TARGET_PCT` | 2.5% below / 3.0% above reference entry |
| Max hold | `MAX_HOLD_MINUTES` | 240 minutes (48 × 5m candles) |
| Execution cost model | `protocol.py` | fee 0.055%/side; normal slippage 0.05%/side; elevated 0.10%/side |
| Intrabar policy | observation features `_frozen_intrabar_policy` | `STOP_FIRST` (conservative; ambiguous intrabar never assumed favourable — matches registry rule `AMBIGUOUS_INTRABAR_policy`) |
| PASS semantics | `protocol.py::classify_exhaustion` | `exhaustion_magnitude ≤ 0.4` → `PASS`; `> 0.4` → `REJECT`; missing/invalid/non-finite → `MISSING_FEATURE` (0 rows in cohort) |
| REJECT semantics | same | complement of PASS |
| **CONTROL semantics** | registry hypothesis text | *"Prospectively compare exhaustion_magnitude ≤ 0.4 against > 0.4, **with all eligible signals as CONTROL**"* → frozen CONTROL = **aggregate unfiltered baseline** (all eligible finalized observations of the same cohort) |
| Minimum sample | registry / DB | `minimum_n = 100`, `minimum_symbols = 10` |
| Minimum OOS days | — | **not defined** (registry key absent for V3) |
| Statistical decision gates | — | **not defined** (see §1.4) |

Process rules frozen in the registry (`rules` block): no production changes, no scanner parameter/gate changes, no real/paper orders, shadow-only, discovery data cannot be used for validation, thresholds frozen before `started_at`, no parameter retuning during collection, **`no_early_stop_without_minimum_n: true`**, conservative ambiguous-intrabar policy.

### 1.2 Code ↔ protocol ↔ DB consistency

* `git diff` of `app/research/me_reverse_long_v3/**`, `app/research/prospective_observer.py`, `app/research/prospective_registry.json`, `sql/migrations/067_me_reverse_long_v3_execution_outcome.sql`, `tests/test_me_reverse_long_v3_{protocol,evaluator}.py` between **production VPS HEAD** `362c217` and **origin/main** `5a3d29c` / `224f1d0` is **empty** → frozen V3 code on production is identical to GitHub `main` (PRs #180/#182 touch only unrelated HTF/bybit components).
* DB `research.prospective_experiment` row equals the git registry entry (threshold 0.4, minimum_n 100, minimum_symbols 10, `started_at = freeze_ts = 2026-10-13T12:37:03Z`, status `RUNNING`, primary metric `net_r_normal`, fee/entry/stop/target rules identical).
* Outcome schema `research.me_reverse_long_v3_execution_outcome` (migration 067) contains the frozen finalization CHECK (`candle_count = 48`, non-null economics, `coverage_reason IS NULL` on FINALIZED) and the non-final-no-economics CHECK; production constraints were extracted and match the migration byte-for-byte in meaning (see `12_constraints.csv`).

### 1.3 Protocol fingerprint

`MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS` has **no stored `protocol_fingerprint`** column/value (unlike `ME_R_LONG_EARLY_MAE_EXIT_OOS_V2`, which stores one). The manifest therefore records a **derived** fingerprint — SHA-256 of the canonical frozen payload — and explicitly labels it `DERIVED_NOT_STORED`:

```
derived protocol fingerprint = 67565f04f5cd0801e615b91aa81826b3761d24e63482e0aecc669ff6b9dbf565
```

### 1.4 Blockers / gaps recorded (no action taken)

1. **`STATISTICAL_METHOD_NOT_APPROVED`** — no pre-registered statistical decision gates (no CI gate, no economic gate, no stopping rule beyond `no_early_stop_without_minimum_n`) and no operator-approved inference method exist for this experiment. All intervals in this report are therefore **exploratory**; no confirmatory verdict is issued.
2. **No stored immutable protocol fingerprint** for V3 — immutability evidence is the frozen registry entry + tagless code constants + `freeze_ts`; adequate for research purposes but weaker than the fingerprinted ME-R-MAE-V2 protocol.
3. **`minimum_oos_days` not pre-registered** — the cohort covers 9 UTC OOS days; day-level generalization is correspondingly weak (this is a limitation of the frozen design, not a data error).

---

## 2. Stage 2 — Integrity audit (READ-ONLY)

Data source: production PostgreSQL `trad_bot`, schema `research`, extracted read-only with
`audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/01_extract_frozen_cohort.sql`
(+ `02_extract_supplemental.sql`, `03_extract_constraints.sql`). Snapshot is immutable, SHA-256-hashed in the manifest
(`04_cohort_full.csv` = `04090564a6f1f173dbabc371e9fec62b466813a9e4cbc162bb6e1307b5571ff1`).

**Verdict: `CLEAN` — 20/20 checks PASS.**

| Check | Result | Detail |
|---|---|---|
| Cohort row count | PASS | 137 `prospective_observation` rows for the experiment |
| `observation_id` unique | PASS | 137/137 |
| `(experiment_id, source_signal_id)` unique | PASS | 137/137 (deterministic negative synthetic keys, `identity.py`) |
| Outcome join 1:1 | PASS | 137/137 joined to `research.me_reverse_long_v3_execution_outcome`; **0 orphan outcome rows** |
| Status split | PASS | 136 `FINALIZED`, 1 `PENDING`, 0 ambiguous/invalid states |
| Status values allowed | PASS | within migration-067 CHECK set |
| FINALIZED economics complete | PASS | gross/net-normal/net-elevated/entry/exit/exit_reason non-null for all 136 |
| Non-finalized carries no economics | PASS | PENDING row has NULL economics (constraint-compatible) |
| FINALIZED coverage maturity | PASS | `candle_count = 48` and `coverage_reason IS NULL` for all 136 (48 × 5m = 240 min = frozen max hold) |
| Pre-freeze contamination | PASS | every `signal_time > 2026-10-13T12:37:03Z`; earliest 2026-10-13 12:45:22Z |
| Synthetic source ids | PASS | all negative deterministic keys |
| Classification vs frozen threshold | PASS | `filter_group` = PASS iff `exhaustion_magnitude ≤ 0.4` for all 137 |
| `rule_passed` consistency | PASS | consistent with filter_group for all rows |
| Frozen geometry | PASS | `stop = entry × 0.975`, `target = entry × 1.03`, ordering `0 < stop < entry < target` for all 136 |
| Exit-reason semantics | PASS | TP rows exit at target, SL rows exit at stop, TIMEOUT rows exit exactly at entry+240 min |
| R & cost-model reconstruction | PASS | `gross_r = (exit−entry)/(entry−stop)`; `net_r = (exit−entry−c·(entry+exit))/(entry−stop)` with `c_normal=0.00105`, `c_elevated=0.00155` (fee+slippage per side); max abs deviation < 5e-6 on every row |
| Ambiguous flag review | PASS | **0 rows** flagged ambiguous; frozen `STOP_FIRST` policy applied |
| TP/SL R multiples | PASS | TP_FIRST ⇒ +1.2 R gross (3.0%/2.5%), SL_FIRST ⇒ −1.0 R gross |
| Execution entry after signal | PASS | `execution_entry_at > signal_time` for all rows |
| Outcome ordering | PASS | `outcome_created_at ≥ observation_created_at` |

Additional facts:

* **1 PENDING row:** `observation_id=283308`, `JCTUSDT`, `REJECT`, signal `2026-10-22T15:54:57Z` — the only non-finalized observation; excluded from all statistics.
* `research.prospective_outcome` has **0 rows** for this experiment (and `research.prospective_completed_event` 0 rows). Per the task definition this is **not an error**: the authoritative outcome source for this study is `research.me_reverse_long_v3_execution_outcome`.
* `research.prospective_experiment_direction_state` has no row for V3 (it is used by other experiments); V3 lifecycle lives in `research.prospective_experiment.status = RUNNING`.
* Actual eligible N = **136 finalized** (PASS 107 / REJECT 29), **42 unique symbols**, **9 UTC OOS days**, 57 symbol-days; PASS spans 37 symbols / 9 days, REJECT 15 symbols / 5 days.
* **Known limitation (not provable from stored rows):** completeness of observer capture — that no eligible V2-LONG candidate was *missed* — cannot be verified from stored rows alone, because `source_signal_id` is a deterministic synthetic key and does not join back to `dds.scanner_setup`.

---

## 3. Stage 3 — Statistical analysis

### 3.1 Operator SQL-audit reconciliation

All **22** operator-reported values reproduce exactly (tolerance 5e-4 / exact counts). Full table in
`ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.json → operator_reconciliation`.

| Metric | PASS (computed / operator) | REJECT (computed / operator) |
|---|---|---|
| Finalized N | 107 / 107 | 29 / 29 |
| Gross E[R] | −0.19788 / −0.1979 | +0.16213 / +0.1621 |
| Net E[R] normal | −0.28168 / −0.2817 | +0.07796 / +0.0780 |
| Net E[R] elevated | −0.32158 / −0.3216 | +0.03788 / +0.0379 |
| Net PF | 0.30588 / 0.3059 | 1.16773 / 1.1677 |
| Total Net R | −30.13927 / −30.1393 | +2.26093 / +2.2609 |
| Win rate | 27.10% (29/107) / 27.10% | 51.72% (15/29) / 51.72% |
| TP_FIRST / SL_FIRST / TIMEOUT | 6 / 27 / 74 | 14 / 12 / 3 |

Win rate = share of strictly positive `net_r_normal` (no zero-return rows exist).

### 3.2 PASS / REJECT / frozen CONTROL

| Group | N | Symbols | Days | Gross E[R] | Net E[R] normal | Net E[R] elevated | Net PF | Total Net R | Win rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **PASS** (`≤ 0.4`) | 107 | 37 | 9 | −0.1979 | **−0.2817** | −0.3216 | 0.306 | −30.139 R | 27.10% |
| **REJECT** (`> 0.4`) | 29 | 15 | 5 | +0.1621 | +0.0780 | +0.0379 | 1.168 | +2.261 R | 51.72% |
| **CONTROL** (all eligible) | 136 | 42 | 9 | −0.1211 | −0.2050 | −0.2449 | 0.510 | −27.878 R | 32.35% |
| MISSING_FEATURE | 0 | — | — | — | — | — | — | — | — |

**CONTROL interpretation (frozen):** the registry defines CONTROL as *"all eligible signals"* — an **aggregate unfiltered baseline of the same cohort**. It is **not** an independently collected cohort and **not** a counterfactual control; it contains both compared groups by construction. It is reported as a descriptive baseline only; **no paired Δ vs CONTROL is claimed** (`CONTROL_AVAILABLE_AGGREGATE_ONLY`). Had the frozen design not defined it, this report would have marked `CONTROL_NOT_VALIDATED` rather than substitute an aggregate.

Reading: PASS **underperforms the unfiltered baseline** (−0.2817 vs −0.2050 R): on this cohort the frozen filter removes better-than-average trades and keeps worse-than-average ones.

### 3.3 Uncertainty (EXPLORATORY — `STATISTICAL_METHOD_NOT_APPROVED`)

Resampling: CPython `random.Random`, fixed seeds 20261022–20261028, 20 000 replications each, percentile intervals. Resampling units: observations (reference only), UTC-date blocks (9 clusters), symbol blocks (37 clusters). PASS and REJECT are **disjoint unpaired groups**; no paired delta is identifiable. Day-block and symbol-block intervals are **separate single-axis sensitivity intervals** and are **NOT a calibrated joint multiway 95% CI**; the two-way pigeonhole bootstrap is reported as an explicitly **uncalibrated** sensitivity only.

**PASS Net E[R] (normal cost), point = −0.2817 R (N=107)**

| Interval | 95% low | 95% high | Entirely < 0? |
|---|---:|---:|---|
| IID bootstrap (ignores dependence) | −0.3957 | −0.1639 | yes |
| **Day-block bootstrap** | −0.4820 | −0.0585 | yes |
| **Symbol-block bootstrap** | −0.4549 | −0.1213 | yes |
| Cluster-robust SE by day (normal approx.) | −0.4849 | −0.0785 | yes |
| Cluster-robust SE by symbol (normal approx.) | −0.4476 | −0.1157 | yes |
| Two-way day×symbol pigeonhole (**uncalibrated**) | −0.5990 | +0.0570 | touches 0 |

**PASS − REJECT Net E[R] difference, point = −0.3596 R**

| Interval | 95% low | 95% high |
|---|---:|---:|
| Welch (independent groups) | −0.7626 | +0.0433 |
| IID bootstrap | −0.7519 | +0.0419 |
| Day-block bootstrap | −0.8517 | +0.2954 |
| Symbol-block bootstrap | −0.8224 | +0.1483 |

**REJECT Net E[R], point = +0.0780 R (N=29, exploratory):** day-block bootstrap [−0.5331, +0.4811] — includes zero; REJECT's apparent positivity is not statistically established.

Interpretation within evidence strength:

* PASS has **no positive executable edge**: every single-axis exploratory interval for PASS Net E[R] lies entirely below zero; even the uncalibrated two-way sensitivity barely reaches zero at the upper end.
* PASS − REJECT is negative in point estimate on **all** interval constructions, but no interval excludes zero — the difference is **not** confirmatory (REJECT N=29, 5 days). It is nonetheless the *opposite* sign to the hypothesis (which predicts PASS > REJECT).
* No multiplicity adjustment is applied (nothing confirmatory is claimed); all subgroup/robustness analyses are exploratory and are **not** used to tune or re-select a strategy.

### 3.4 Breakdowns

**By UTC day** (`_by_day.csv`) — PASS daily mean negative on **8/9** days (only 2026-10-19 positive: +0.319 R); worst day 2026-10-16 (PASS −0.4506 mean, −10.364 R total; the cohort's only day where REJECT is also negative).

| UTC date | N | PASS n | PASS mean | PASS total | REJECT n | REJECT mean | All mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-13 | 12 | 12 | −0.1142 | −1.371 | 0 | — | −0.1142 |
| 2026-10-14 | 17 | 7 | −0.8395 | −5.877 | 10 | +0.5241 | −0.0374 |
| 2026-10-15 | 8 | 5 | −0.9118 | −4.559 | 3 | +0.3822 | −0.4265 |
| 2026-10-16 | 31 | 23 | −0.4506 | −10.364 | 8 | −0.8082 | −0.5429 |
| 2026-10-17 | 5 | 5 | −0.1393 | −0.697 | 0 | — | −0.1393 |
| 2026-10-19 | 12 | 12 | +0.3191 | +3.829 | 0 | — | +0.3191 |
| 2026-10-20 | 18 | 15 | −0.0700 | −1.050 | 3 | +0.3869 | +0.0061 |
| 2026-10-21 | 32 | 27 | −0.3321 | −8.968 | 5 | +0.2357 | −0.2434 |
| 2026-10-22 | 1 | 1 | −1.0830 | −1.083 | 0 | — | −1.0830 |

**By symbol** (`_by_symbol.csv`) — **27 of 37** PASS symbols have negative total Net R. Largest PASS losers: `QUSDT` (−4.33 R), `CLUSDT` (−4.22 R, 12 observations), `INJUSDT` (−3.25 R); largest PASS winners: `SNDKUSDT` (+1.82), `SKHYUSDT` (+1.73), `1000PEPEUSDT` (+1.65). Top-3 losing symbols = 39.2% of PASS total net; top-3 winners offset −17.2% of it. REJECT is thin (15 symbols, several with 1–2 trades).

**By market regime** (`_by_regime.csv`)

| Regime | N | PASS n | PASS mean | REJECT n | REJECT mean |
|---|---:|---:|---:|---:|---:|
| HIGH_VOLATILITY | 77 | 52 | −0.2913 | 25 | +0.0873 |
| RANGE | 59 | 55 | −0.2726 | 4 | +0.0195 |

PASS is negative in **both** observed regimes; the negative result is not a single-regime artifact.

**Exit reasons** — PASS: TP 6 / SL 27 / TIMEOUT 74; REJECT: TP 14 / SL 12 / TIMEOUT 3. Frozen R scale: TP = +1.2 R gross (3.0%/2.5%), SL = −1.0 R gross.

### 3.5 Concentration & dependence

* PASS losses are **not** dominated by a few symbols/days (top-3 losers = 39% of a broad −30 R total; 27/37 symbols and 8/9 days negative).
* **Overlapping/correlated setups:** 99 overlapping 240-minute hold-window pairs inside the same symbol (94 of them same-day); **90 of 136 finalized observations sit inside an overlapping window**; max 8 observations per symbol-day, max 12 per symbol, 57 symbol-days. Individual observations are therefore **not independent bets** — the IID interval above is reference-only, and inference leans on day/symbol block resampling (reported separately, not as a joint CI).
* REJECT shares 6 losing symbols with PASS (e.g. `INJUSDT`, `TIAUSDT`), so the two groups are exposed to common symbol-level shocks; a paired interpretation is invalid.

---

## 4. Stage 4 — Robustness

All scenarios in `ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_robustness.csv`. Diagnostic-only exclusions are labeled; **none of these results may be used to design or select a strategy on this OOS sample**.

| Scenario | PASS Net E[R] | REJECT Net E[R] | PASS−REJECT |
|---|---:|---:|---:|
| Baseline | −0.2817 | +0.0780 | −0.3596 |
| Leave-one-day-out (9 runs) | **−0.3576 … −0.2354** (all < 0) | — | −0.6510 … −0.0858 (all < 0) |
| Leave-one-symbol-out (42 runs) | **−0.3086 … −0.2506** (all < 0) | — | all < 0 |
| Exclude top-3 losing symbols (diag.) | −0.2182 | +0.3808 | −0.5990 |
| Exclude top-3 winning symbols (diag.) | −0.3225 | −0.0969 | −0.2256 |
| Elevated costs | −0.3216 | +0.0379 | — |
| Excluding TIMEOUT trades | **−0.6834** (N=33) | +0.1004 (N=26) | — |

Additional robustness observations:

* **Leave-one-day-out:** PASS mean negative under removal of *every* individual day (range [−0.358, −0.235]); PASS−REJECT negative in all 9 runs.
* **Leave-one-symbol-out:** PASS mean negative for all 42 single-symbol removals (range [−0.309, −0.251]).
* **TIMEOUT dependence:** the negative result is *not* a timeout-decay artifact — PASS without TIMEOUT trades is **worse** (−0.683 R over 33 TP/SL trades) than with them (74 TIMEOUT trades contribute −7.59 R, 25% of the total). REJECT's +0.078 is likewise not timeout-driven (+0.100 without timeouts).
* **Elevated trading costs** widen PASS losses (−0.3216 R) and shrink REJECT to +0.0379 R.
* **Small groups:** REJECT (N=29, 15 symbols, 5 days, 14 TP of which several are single-symbol repeats of +1.1147 R) is highly sensitive to single trades — REJECT means are exploratory only. PASS (N=107, 37 symbols, 9 days) exceeds both pre-registered minimums.
* **Day-sign stability:** PASS negative on 8/9 days; REJECT positive on 4/5 observed days but on only 15 symbols.

---

## 5. Stage 5 — Decision

**FINAL RESEARCH VERDICT = `NEW_HYPOTHESIS_REQUIRED`**

Basis:

1. The frozen hypothesis — *PASS (`exhaustion_magnitude ≤ 0.4`) has positive executable edge and outperforms REJECT (`> 0.4`)* — is **not confirmed**: PASS Net E[R] = −0.2817 R (elevated −0.3216 R), Net PF 0.306, and every single-axis exploratory 95% interval is entirely below zero.
2. Pre-registered readiness gates are met (`minimum_n=100` → PASS 107; `minimum_symbols=10` → 37 PASS symbols), so this is **not** an under-sampled/inconclusive outcome; `no_early_stop_without_minimum_n` is respected (evaluation performed at N≥100).
3. The negative result is robust to every permitted robustness cut (§4), and PASS underperforms the frozen aggregate CONTROL baseline (−0.2050 R).
4. The threshold ordering is *inverted* on this cohort (REJECT +0.078 R > PASS −0.282 R) — but REJECT evidence is too thin (N=29, 5 days) to constitute a validated inverse rule, and this OOS sample can never be used to validate a new rule anyway.
5. Formal negative closure language (`CLOSED_NEGATIVE_RECOMMENDED`) is withheld because no approved negative decision gates/method exist (§1.4, `STATISTICAL_METHOD_NOT_APPROVED`) — results are exploratory by construction.

**Recommended lifecycle (NOT applied — no production change performed):**

* Keep `research.prospective_experiment.status = 'RUNNING'` and all observations/outcomes **unchanged**.
* If the operator decides to close: submit a **separate controlled lifecycle task** (e.g. `prospective_experiment.status = 'CANCELLED'` and/or a direction-state `CLOSED_NEGATIVE` row, per the framework naming used for `LR_LONG_GATE_V1`, `SRR_OOS_SCANNER_V1_PROSPECTIVE`) referencing this report — requiring explicit operator approval. Retain full history; no rewrite/backfill.
* **Paper/live remain BLOCKED.** No threshold change (0.4 untouched), no retuning, no new signal backfill into OOS, no observer stop, no systemd/deployment actions.
* Master ME OOS tracker suggestion (not applied): **`CLOSED`** (pending operator approval).
* If research continues: register a **new hypothesis** (new experiment id, new freeze, discovery/validation separation). Candidate directions (research-only, must be frozen before observation): inverted feature relation, different exhaustion scaling, or execution-geometry variants — **each requires its own prospective cohort**; none may be validated on the present OOS sample.

---

## 6. Limitations

1. `STATISTICAL_METHOD_NOT_APPROVED` — no pre-registered decision gates/approved inference method; **all intervals are exploratory**; no confirmatory verdict issued.
2. Day-block and symbol-block bootstrap intervals are single-axis sensitivity intervals, **not** calibrated joint multiway 95% CIs; the two-way pigeonhole bootstrap is explicitly uncalibrated.
3. PASS/REJECT are disjoint unpaired groups; no paired Δ exists; REJECT (N=29, 5 days) is imprecise and treated as exploratory.
4. Only **9** UTC OOS days; no `minimum_oos_days` was pre-registered; day-level generalization is weak.
5. Shadow `NEXT_OPEN_5M` fills are a hypothetical execution model ("not executable proof" per the frozen entry rule); no order book/latency/queue effects are modeled.
6. Observer-capture completeness (missed eligible candidates) cannot be proven from stored rows (synthetic `source_signal_id`).
7. Regime labels come from the observer's `market_regime` field (2 observed regimes only).
8. Multiple observations per symbol overlap in holding time (90/136 inside overlapping windows); effective number of independent bets is far below 136 — addressed only through cluster resampling, not eliminated.

## 7. Deterministic tests / reproducibility

* `tests/test_me_reverse_long_v3_final_statistical_evaluation_v1.py` — **16 passed**: join cardinality 1:1, maturity split 136/1, zero missingness on FINALIZED, no pre-freeze contamination, classification = frozen threshold, integrity audit CLEAN, all operator values MATCH, CONTROL = aggregate unfiltered baseline (contains both groups), fixed-seed intervals byte-stable, **manifest verified before any regeneration (strictly read-only)**, regeneration into a temporary directory byte-identical to committed artifacts, and **negative regression tests**: tampering the frozen extraction SQL or the cohort snapshot under an unchanged manifest is detected as FAIL, a **present** local-only evidence file with a drifted hash also fails verification (absence stays informational), and the original P2 defect (in-place regeneration re-hashing drifted inputs and masking them) is reproduced and confirmed fixed.
* **Integrity order (Codex P2 fix, PR #188):** (1) verify the committed SHA-256 manifest against the files on disk — `python tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py --verify` is strictly read-only and never rewrites the manifest; (2) only then regenerate artifacts, and only into a separate directory (`--out DIR`), so verification can never be satisfied by freshly generated hashes.
* Script rerun determinism: `ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.json` = `42e5697b…e6471a79` before/after re-run (identical); N, Net E[R], PF, CIs and the final research verdict are unchanged by the manifest fix (research results are byte-identical).
* Commands:
  * extraction (VPS, READ-ONLY): `sudo -u postgres psql -d trad_bot -P pager=off -f 01_extract_frozen_cohort.sql` (and `02_…`, `03_…`)
  * verify committed evidence (read-only): `python tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py --verify`
  * analysis: `python tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py` (generation; `--out DIR` to write elsewhere)
  * tests: `python -m pytest tests/test_me_reverse_long_v3_final_statistical_evaluation_v1.py -q`

## 8. Artifacts

| Artifact | Purpose |
|---|---|
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.md` | this report |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.json` | machine-readable results (integrity, descriptive, CIs, robustness, verdict) |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.csv` | immutable eligible snapshot (137 rows, analysis fields) |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_day.csv` | UTC-day breakdown |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_symbol.csv` | symbol breakdown |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_by_regime.csv` | market-regime breakdown |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_robustness.csv` | all robustness scenarios |
| `docs/research/ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1_manifest.json` | SHA-256 manifest, git HEADs, derived protocol fingerprint |
| `tools/research/me_reverse_long_v3_final_statistical_evaluation_v1.py` | reproducible analysis script (stdlib only, fixed seeds) |
| `tests/test_me_reverse_long_v3_final_statistical_evaluation_v1.py` | deterministic reproducibility tests |
| `audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/01_extract_frozen_cohort.sql` | READ-ONLY frozen-cohort extraction SQL |
| `audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/02_extract_supplemental.sql` | READ-ONLY supplemental extraction SQL |
| `audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/03_extract_constraints.sql` | READ-ONLY constraint/index inventory SQL |
| `audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/me_v3_eval_20261022/*.csv` | raw immutable extraction snapshot (SHA-256 in manifest; the two large full-table extracts `06_prospective_outcome_all.csv` / `07_completed_events.csv` and the tarball are hashed but kept **local-only**, not committed to git) |

## 9. Safety compliance

* Production PostgreSQL: **READ-ONLY** (only `SELECT` / `\copy (SELECT …) TO`; no INSERT/UPDATE/DELETE/DDL).
* Research registry / lifecycle: **not modified** (`prospective_experiment.status` untouched).
* Historical observations/outcomes: **not rewritten**; no backfill.
* Frozen protocol/threshold (0.4): **unchanged**.
* systemd: **not restarted**; paper/live: **not enabled**; deployment: **not performed**.
* This evaluation is delivered as research-only files on a separate branch + PR; **no merge to `main`** without separate permission.

---

*Verdict line for trackers:* `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS → NEW_HYPOTHESIS_REQUIRED (exploratory negative: PASS Net E[R] = −0.2817R, N=107, all single-axis exploratory 95% CIs < 0; lifecycle change requires separate operator-approved task).`
