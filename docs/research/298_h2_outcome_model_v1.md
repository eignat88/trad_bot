# #298 / #312 — H2 Delayed-Entry & Paired Outcome Model v1 (D-1..D-12)

**Status:** `PREPARED_EXECUTABLE_MODEL / OWNER_DECISION_REQUIRED` — research
only.  This document and the package
`app/research/me_entry_quality_h2_outcome_v1/` are a **prepared executable
model only**.  H2 remains `IMPLEMENTATION_MERGED / ACTIVATION_HOLD /
OUTCOME_MODEL_BLOCKED_PENDING_DECISION` (issue #298); **no metric may be
published**, no accumulation is enabled, and nothing here claims production
readiness.

**Purpose:** make the twelve open decisions of
`docs/research/312_h2_entry_outcome_policy_v1.md` §3 (D-1..D-12) executable
WITHOUT choosing any of them.  Every decision is a named parameter of
`OutcomePolicy` with the three states
`PENDING_OWNER_DECISION` (default) / `PRELIMINARY_CANDIDATE_NOT_FROZEN` /
`FROZEN` (only via an explicit approved fingerprint), and the model REFUSES
to emit any yield / Net E[R] / win-rate / MAE-MFE aggregate while any
decision is pending or the policy fingerprint is unapproved
(`NOT_READY_PENDING_DECISION` — never a number).

---

## 1. Package layout (all-new files; nothing existing is modified)

| module | responsibility |
| --- | --- |
| `policy.py` | `OutcomePolicy` (D-1..D-12 slots), `Decision` three-state vocabulary, fingerprints, `freeze(approved_fingerprint)` |
| `costs.py` | D-3 cost model hooks (fees / funding / slippage) with mandatory data-source labels |
| `types.py` | plain-data inputs (`CandleBar`, `TradePrint`, `ConfirmationInfo`, `SourceEventInput`) and result records (`LegOutcome`, `EventPairOutcome`, `AggregateOutcome`), honesty labels |
| `entry_model.py` | D-1/D-2/D-4 executable delayed-entry resolution |
| `exit_model.py` | D-5 geometry, D-6 exit rule + tie-break, D-9 MAE/MFE path sampling |
| `paired.py` | D-7 paired baseline-vs-variant evaluation, D-10 missed-winner classification |
| `aggregate.py` | D-8 declared-denominator Net E[R], D-11 acceptance-rule ordering, D-12 idempotency/conflict registry |

Purity: stdlib only (`dataclasses`, `hashlib`, `json`, `math`, `typing`);
no SQL, no systemd, no network, no DB, no clock.  A purity/isolation test
asserts that **no production module references
`me_entry_quality_h2_outcome`** and that the package is never wired into
production.

## 2. Execution semantics (fixed mechanics, parameterized choices)

* **Confirmation close is never executable.**  It is carried only as
  `CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE` (verbatim the
  merged H2 engine's label).  The baseline entry price is the ORIGINAL
  price basis (`SOURCE_REFERENCE_PRICE_VERBATIM_NOT_EXECUTABLE` semantics,
  executable in the model only as the D-7 baseline).
* **Entry timing.**  The entry may occur only at an admissible price class
  STRICTLY AFTER the confirmation instant in candle-sequence order — the
  confirming candle is never an entry source
  (`ENTRY_TIMING_POLICY`).  On a continuous 5m series the immediately next
  candle's open instant equals the confirming close instant exactly (same
  µs timestamp, strictly later sequence position); that boundary open is
  the FIRST admissible entry class.  Anything else must be strictly after
  at µs precision.  Alternative candidates are listed under D-1 below.
* **Closed-candle-only path evaluation.**  Only candles with
  `close_time_us <= evaluation_asof_us` participate (inclusive at the close
  boundary, strict at µs).  A fill not yet resolvable from closed candles
  is `ENTRY_PENDING_NOT_RESOLVABLE_YET` — never guessed.
* **Entry-candle wick policy.**  The candle containing an intra-candle
  entry is excluded from the wick path (pre-entry intra-candle action is
  not separable in OHLCV data); it participates when the entry is at (or
  before) its open (`ENTRY_CANDLE_PATH_POLICY`).
* **Exit prices.**  Stop/target exits occur AT THE LEVEL; a horizon exit
  occurs at the last close at-or-before the horizon end
  (`EXIT_PRICE_POLICY`).  Intra-candle event times are recorded at the
  bar-open bound (`INTRACANDLE_TIME_BOUND_POLICY`).
* **States.**  Entry: `ENTERED` / `MISSED_ENTRY` / `ENTRY_EXPIRED` /
  `ENTRY_PENDING_NOT_RESOLVABLE_YET` / `NO_CONFIRMATION` / `INVALID_INPUT`.
  Exit: `EXIT_STOP` / `EXIT_TARGET` / `EXIT_HORIZON` /
  `EXIT_STOP_TARGET_TIE_AVERAGE` / `EXIT_NONE` (explicitly UNRESOLVED when
  the D-6 rule is `STOP_OR_TARGET_ONLY` and nothing was touched — never a
  fabricated return).
* **Idempotency.**  Results are keyed by
  (`source_event_key`, `variant_id`, `policy_fingerprint`); identical
  redelivery is a no-op, a differing rewrite raises
  `IMMUTABLE_OUTCOME_CONFLICT`.

## 3. D-1..D-12 → concrete code slots

| # | decision | code slot | status | candidates (no default chosen) |
| --- | --- | --- | --- | --- |
| D-1 | executable entry price class | `OutcomePolicy.d_1_entry_price_class` → `EntrySpec` (`policy.py`), enforced in `entry_model.resolve_entry` | **OWNER_DECISION_REQUIRED** | (a) `NEXT_CANDLE_OPEN` — simple, fully observable from OHLCV, but assumes an open fill; (b) `FIRST_TRADE_AT_OR_AFTER_OPEN` — most realistic, requires a trade-print source with its own data-source label; (c) `LIMIT_AT_LEVEL` — models a resting order incl. gap-through/lock-limit semantics, but needs an explicit level and gap-through policy |
| D-2 | fill probability / missed-entry policy | `d_2_fill_missed_entry_policy` → `FillPolicy` (`gap_through_policy`, `missed_entry_stat_policy`), `entry_model` produces `MISSED_ENTRY` for gap-through / lock-limit / no-liquidity | **OWNER_DECISION_REQUIRED** | (a) `ZERO_NET_RETURN_IN_DENOMINATOR` — conservative, keeps every event; (b) `BASELINE_NET_AS_FORGONE_RETURN` — charges the forgone baseline return, sensitive to baseline noise; gap-through: `FILL_AT_OPEN` vs `MISSED_ENTRY_GAP_THROUGH` |
| D-3 | fees / funding / slippage | `d_3_cost_model` → `CostModel` (`costs.py`) with `fee_model`, `slippage_model`, `funding_model` and MANDATORY `*_data_source` labels | **OWNER_DECISION_REQUIRED** | (a) fixed bps fee + fixed bps slippage + fixed funding per 8h — simplest, needs a fee schedule source; (b) spread-based slippage (`spread_bps`, half-spread per side) — needs a spread snapshot source; (c) volume-impact slippage — most realistic, needs volume data (hook exists as an extension point; not implemented) |
| D-4 | entry deadline / expiry | `d_4_entry_deadline` → `EntryDeadline(max_delay_ms)`, `entry_model` emits `ENTRY_EXPIRED` | **OWNER_DECISION_REQUIRED** | (a) fixed ms after confirmation (e.g. 30m); (b) N candles after confirmation; (c) price-conditioned deadline (expire when price has moved beyond a threshold) — richer but introduces another parameter |
| D-5 | SL/TP geometry | `d_5_sl_tp_geometry` → `SlTpGeometryPolicy` (`mode`, optional explicit distances), `exit_model.build_geometry` | **OWNER_DECISION_REQUIRED** | (a) `SOURCE_VERBATIM` — same absolute levels for both legs (fair on levels, unfair on distances); (b) `REBASED_AT_ENTRY` — same distances around each leg's entry (fair on risk, moves levels); (c) `RE_DERIVED_EXPLICIT_DISTANCES` — explicit distances in the policy (transparent, but the distances themselves need an owner decision) |
| D-6 | outcome horizon & exit rule | `d_6_exit_rule` → `ExitRule(exit_rule, horizon_minutes, same_candle_tie_break)`, `exit_model.simulate_leg` | **OWNER_DECISION_REQUIRED** | exit rule: (a) `STOP_OR_TARGET_THEN_HORIZON`, (b) `STOP_OR_TARGET_ONLY` (untouched horizon stays UNRESOLVED — honest but leaves unresolved legs), (c) `FIXED_HORIZON_ONLY`; same-candle tie-break: (a) `STOP_FIRST_CONSERVATIVE`, (b) `TARGET_FIRST`, (c) `AVERAGE_STOP_TARGET_RETURN` (all three implemented) |
| D-7 | baseline definition | `d_7_baseline_definition` → `BaselineSpec(mode)`, `paired._baseline_leg` | **OWNER_DECISION_REQUIRED** | (a) `ORIGINAL_SIGNAL_INSTANT_ORIGINAL_PRICE_BASIS_SYMMETRIC_EXIT_MODEL` (implemented: same events, same exit/cost/geometry conventions); (b) a candle-executed baseline at the original instant under the SAME entry class (stricter symmetry of execution, diverges from the "original price basis" wording); (c) an external benchmark cohort — would break paired comparability |
| D-8 | Net E[R] denominator | `d_8_net_er_denominator` → `DenominatorSpec(definition)`, `aggregate._in_denominator` | **OWNER_DECISION_REQUIRED** | (a) `ALL_SOURCE_EVENTS` (keeps `MISSED_ENTRY`/`ENTRY_EXPIRED`/`NO_CONFIRMATION`/`INCOMPLETE_COVERAGE` in the denominator — the anti-survivorship option); (b) `ALL_CONFIRMED_EVENTS`; (c) `ALL_FUNDABLE_EVENTS`.  Narrower denominators still REPORT every excluded event (counts, never silent drops) |
| D-9 | MAE/MFE conventions | `d_9_mae_mfe_conventions` → `MaeMfeSpec(path_sampling, normalization)`, `exit_model._mae_mfe` | **OWNER_DECISION_REQUIRED** | path: (a) `CLOSE_ONLY_CLOSED_CANDLES_V1` (smooth, misses intra-candle extremes), (b) `WICK_PATH_INTRA_CANDLE_V1` (conservative extremes, bar-level tie-break needed); normalization: (a) `PCT_ENTRY_PRICE`, (b) `R_MULTIPLE_OF_STOP_DISTANCE` |
| D-10 | missed winners | `d_10_missed_winner_rule` → `MissedWinnerRule(definition, min_net_r, reporting)`, `paired.evaluate_pair` | **OWNER_DECISION_REQUIRED** | (a) baseline net R > 0 (threshold 0); (b) baseline net R > explicit threshold; (c) percentile-based definition — requires the cohort first (weakest pre-registration) |
| D-11 | statistical acceptance rule | `d_11_statistical_acceptance_rule` → `StatisticalAcceptanceRule(min_paired_n, evaluation_cadence, decision_rule, declared_before_outcomes_inspected)`, ordering enforced by `aggregate.OutcomeSet` | **OWNER_DECISION_REQUIRED** | (a) fixed min paired N + paired mean net-R difference vs zero; (b) sequential test with alpha spending; (c) CI-width-based stopping.  The rule MUST be declared before outcomes are inspected — a late declaration raises `AcceptanceRuleOrderError` and every aggregate refuses |
| D-12 | policy freeze | `d_12_policy_freeze` → `FreezeRecord`, `OutcomePolicy.freeze(approved_fingerprint)` | **OWNER_DECISION_REQUIRED** | (a) single fingerprint over D-1..D-11 (implemented); (b) per-decision approvals with separate fingerprints; (c) versioned policy registry.  `FROZEN` is unreachable except through `freeze()` with the exact approved fingerprint |

**No default is chosen for any decision.**  `OutcomePolicy()` starts with
all twelve slots at `PENDING_OWNER_DECISION`; the candidate values above
are review input only.

## 4. Refusal contract (tested)

1. `evaluate_pair(...)` returns `NOT_READY_PENDING_DECISION` with **no
   numbers on either leg** while any of D-1..D-7/D-9/D-10 is pending;
2. `OutcomeSet.aggregate()` returns `NOT_READY_PENDING_DECISION` with every
   metric field `None` unless **all** D-1..D-12 are specified AND the
   policy is `FROZEN` via an approved fingerprint;
3. an acceptance rule declared AFTER outcomes were registered poisons the
   run (`AcceptanceRuleOrderError`; aggregates refuse);
4. outcomes evaluated under a different `policy_fingerprint` are refused;
5. non-entered events (`MISSED_ENTRY`, `ENTRY_EXPIRED`, `NO_CONFIRMATION`,
   `INCOMPLETE_COVERAGE`) are never silently dropped — they stay in the
   `ALL_SOURCE_EVENTS` denominator or are reported as explicit exclusions.

## 5. Test coverage

`tests/test_me_entry_quality_h2_outcome_{policy,entry,paired,aggregate,causal_purity}_v1.py`
cover: the three-state decision vocabulary, fingerprint/freeze mechanics,
all three entry price classes, expiry/pending/missed states, gap-through /
lock-limit / no-liquidity handling, symmetric pairing, all three geometry
modes, all three exit rules and all three same-candle tie-breaks, MAE/MFE
close-only vs wick conventions, cost hooks and funding accrual, D-8
denominator accounting and D-2 return assignment, D-10 missed winners,
D-11 ordering, D-12 idempotency/conflicts, ASOF strictness at µs, future
append invariance, determinism and package purity/production isolation.

## 6. Explicit closing statement

**This is a prepared executable model only.**  It is research code with no
deployment, no database, no service and no wiring of any kind.  The H2
track remains **`IMPLEMENTATION_MERGED / ACTIVATION_HOLD`** (issue #298):
nothing in this package enables accumulation, activation, or a production
readiness claim, all twelve decisions remain `OWNER_DECISION_REQUIRED`,
and **no metric may be published** — no yield, no Net E[R], no win rate, no
MAE/MFE aggregate, no "H2 improves X" conclusion — until the decisions are
made and frozen through a separately approved fingerprint and the #298
gates G1..G8 are resolved and evidenced on their own terms.
