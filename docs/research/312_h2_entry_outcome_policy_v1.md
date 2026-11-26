# Issue #312 — H2 Entry & Outcome Policy Specification (FR-06)

**Status:** `BLOCKED_PENDING_DECISION` — SPECIFICATION ONLY, no execution
model is implemented, approved or implied.
**Scope:** research-only.  Until this policy is approved and frozen
separately, the H2 prospective runtime stores RSI-confirmation outcomes only
and publishes **no** final yield metrics.

---

## 1. Principle (hard rule)

An RSI confirmation is **NOT a trade**.  The merged H2 engine (PR #311)
already enforces this: a confirmed delayed entry records the earliest
causally executable instant (the close of the confirming 5m candle) but the
entry price stays `UNDETERMINED_NO_EXECUTION_MODEL`; the confirmation-candle
close is carried only as `CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE`.
**The close of the confirming candle must never be treated as an executable
price** (explicitly forbidden by #312 / FR-06).

The database enforces it too: `variant_result.entry_candidate_price` has a
`CHECK (entry_candidate_price IS NULL)` constraint and the schema contains no
yield column of any kind.

## 2. What the runtime stores today (safe subset)

Per (`source_event_key`, `variant_id`, `policy_fingerprint`):

* original signal instant (µs), activation freeze timestamp, lineage hash;
* the pure H2 state (`CONFIRMED_DELAYED` / `EXPIRED` / …) and reason;
* RSI at confirmation (recomputed closed-candle RSI) and the verbatim source
  feature snapshot (`rsi_14`, `rsi_delta_3`, confirmation flag);
* the confirmation candle OPEN time and the confirmation instant (µs) and
  the resulting **entry delay**;
* the non-executable reference close price (explicitly labeled);
* candle provenance (source kind/label/payload hash/row count) and coverage.

No fill, no position, no PnL, no expectancy is ever recorded.

## 3. BLOCKED_PENDING_DECISION — open decisions (no assumptions made)

Each item below requires an explicit project decision.  The runtime is not
allowed to substitute assumptions for any of them.

| # | Decision | Why it is open |
|---|---|---|
| D-1 | **Executable entry price** — when after the confirmation may a SHORT be opened, and which price class is admissible (next-candle open, first trade at/after open, VWAP band, limit at level, …)? | No approved execution model exists.  The confirmation close is explicitly inadmissible. |
| D-2 | **Fill probability / missed-entry policy** — what happens when the market never trades at the admissible price (gap-through, lock-limit, no liquidity)?  Is the event a `MISSED_ENTRY` outcome, and how does it enter the statistics? | Without it, any yield metric would silently drop non-fills (survivorship bias). |
| D-3 | **Fees, funding and slippage model** — per-side fee bps, funding accrual for the held interval, slippage model (fixed bps / spread-based / volume-impact), each with its data source. | Net E[R] is undefined without an explicit cost model. |
| D-4 | **Entry deadline / expiry of the delayed entry** — how long after the confirmation may the entry still be executed before it expires (`ENTRY_EXPIRED`)? | The H2 observation-window expiry governs confirmation, not execution. |
| D-5 | **Stop / target geometry for the delayed entry** — reused verbatim from the source event (source invalidation/target), re-based at the delayed entry, or re-derived?  How are MFE/MAE normalized? | Baseline-vs-H2 comparison is meaningless if geometry differs. |
| D-6 | **Outcome horizon & exit rule** — fixed horizon (e.g. 240m), stop/target touch, or the source event's own exit semantics; how are overlapping exits (stop+target in one candle) resolved? | Needed for any return distribution. |
| D-7 | **Baseline definition** — the exact comparator cohort: the same source events entered at the ORIGINAL signal instant with the original price basis, evaluated under the SAME execution model as D-1..D-6. | Without a symmetric baseline, "improvement" is unfalsifiable. |
| D-8 | **Net E[R] formula** — mean of per-event net returns over which denominator (all source events / confirmed only / fundable only), and how `MISSED_ENTRY`/`INCOMPLETE_COVERAGE`/`EXPIRED` events are counted. | Prevents cherry-picking the denominator post-hoc. |
| D-9 | **MAE/MFE conventions** — path sampling (closed candles only vs intra-candle wick), normalization (% of entry price, ATR, or R), and the observation horizon. | Wick-vs-close choice materially moves MAE/MFE. |
| D-10 | **Missed winners accounting** — definition of a "missed winner" (an `EXPIRED`/`MISSED_ENTRY` event that would have been profitable under the baseline model) and its reporting rule. | Required to answer the research question honestly. |
| D-11 | **Statistical acceptance rule** — minimum prospective sample size, evaluation cadence and the decision rule for "H2 improves quality" — declared BEFORE outcomes are inspected. | Post-hoc threshold selection invalidates the study. |
| D-12 | **Policy freeze** — the immutable policy fingerprint(s) to be approved (`H2_POLICY_STATUS` is `PRELIMINARY_CANDIDATE_NOT_FROZEN` until then; windows 3/6 remain preliminary candidates). | Activation gate per #298/#312. |

## 4. Evaluation rules that WILL hold once D-1..D-12 are approved

* the baseline and H2 cohorts are the SAME source events (paired comparison);
* every metric is computed only from CLOSED candles with the causal ASOF
  rules already enforced by the engine (no look-ahead, no repainting);
* `EXPIRED`, `INCOMPLETE_COVERAGE`, `MISSED_ENTRY` events stay in the
  denominator as declared in D-8 (never silently dropped);
* fees/slippage/funding per D-3 are applied symmetrically to both cohorts;
* results are reported with the policy fingerprint and algorithm version of
  the exact run (already stored in `variant_result`).

## 5. Interim reporting (allowed now)

Allowed: counts per state, confirmation delays, RSI at confirmation,
coverage status — i.e. exactly what `runtime_health` / `variant_result`
already store.

Forbidden: any Net E[R], return %, expectancy, win rate, MAE/MFE aggregate,
or "H2 improves X" conclusion — until D-1..D-12 are approved and a
prospective sample exists (currently the #305 cohort has **0** source
events, so no prospective coverage can be claimed at all).
