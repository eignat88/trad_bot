# SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1

**Parent Issue:** #259
**Status of this document:** IMPLEMENTED / TESTED / DISABLED / AWAITING_OPERATOR_ACTIVATION
**Scope:** implementation + tests + documentation only. No deployment, no
activation, no Paper/Live change.

---

## 1. Purpose

The frozen prospective research result for

`SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`

reports (as of 2026-11-04):

| Metric | Value |
|---|---:|
| Finalized eligible outcomes | 1 719 |
| Symbols | 94 |
| OOS days | 25 |
| Normal Net E[R] | +0.064721R |
| Normal Net PF | 1.2723 |
| IID bootstrap 95% CI | [+0.034548, +0.093340] |
| Day-cluster bootstrap 95% CI | [+0.019565, +0.111798] |
| Symbol-cluster bootstrap 95% CI | [+0.030253, +0.101473] |
| Elevated Net E[R] | −0.035279R |
| Elevated Net PF | 0.8719 |

Research verdict: `POSITIVE_PRIMARY_NORMAL_COST_ELEVATED_COST_NEGATIVE / EXECUTION_FRAGILE`.

**The gap:** all of the above is computed on the MODEL entry
(`reference_price = closest_resistance`). In
`app/scanners/support_resistance.py` the SHORT branch fires when
`level > candles_5m[-1].close` and sets `reference_price = closest_resistance`,
so at detection time the market price may already be BELOW the modeled entry.
`_raw_level_distance_pct` is derived from `entry` and `level`, which are equal
in this branch, therefore it does not evidence entry availability.

This mechanism measures, for every NEW SRR SHORT prospective observation,
whether the model entry is still executable against the real Bybit book at
signal time — and stores the evidence in a separate research table for a
later execution-aligned evaluation.

Until execution alignment is demonstrated the standing verdict is:

**`RESEARCH_POSITIVE / EXECUTION_UNVALIDATED / PAPER_LIVE_NO_GO`**

---

## 2. Architecture

```text
SRR SHORT signal
        |
        v
ProspectiveOOSObserver.observe()          (existing, untouched)
        |
        v
existing observation COMMIT               (own transaction, committed first)
        |
        v
_observe_prospective_with_fill()          (scanner_runner.py — the single
        |                                  integration point for every
        |                                  call site: tradeable, observe-only,
        |                                  expectancy/regime-rejected)
        v
Validate SRR identity                     experiment_id + direction +
        |                                 scanner_name + registry + symbol +
        |                                 signal_time/reference cross-bind
        v
Read-only activation gate                 feature flag + DB activation row +
        |                                 config match + signal_time >
        |                                 activation_ts
        v
API-budget gate                           per-cycle / per-run counter
        |
        v
Atomic claim (PENDING row, COMMIT)        ON CONFLICT DO NOTHING — reserves
        |                                 BEFORE any network call
        v
Bybit Orderbook snapshot                  /v5/market/orderbook via the shared
        |                                 budgeted, rate-limited client
        v
Bid/ask + depth + estimated fill + cost   pure calculation
        |
        v
research.srr_short_fill_feasibility_v1    one row per observation_id
        |
        v
future execution-aligned evaluation       (explicit next stage, NOT this PR)
```

Modules:

| Component | File |
|---|---|
| Capture engine | `app/research/srr_short_fill_feasibility_v1.py` |
| Scanner integration | `scanner_runner.py` → `_observe_prospective_with_fill()` + budget reset in `run_scan_cycle()` |
| Schema | `sql/migrations/082_srr_short_fill_feasibility_v1.sql` |
| Tests | `tests/test_srr_short_fill_feasibility_v1.py`, `tests/test_srr_short_fill_feasibility_migration_082_v1.py` |
| Pure math reuse | `app/research/me_short_a_fill_feasibility_v1.py` (`instrument_constraints`, `_scenario_result` — imported, NOT copied, ME behaviour unchanged) |

**Isolation:** the new contour is read-only with respect to everything
existing. It cannot stop the scanner (all errors are swallowed in the
wrapper), cannot roll back an observation (capture runs strictly after
`observe()` committed), cannot block other OOS experiments, cannot create a
Paper/Live order (only public market endpoints are reachable) and cannot
change trading routing, gates, regime filters or scoring.

**No Bybit call inside the observer transaction:** `observe()` commits its
own transaction first; capture then performs its own short transactions on
the same dedicated research connection.

---

## 3. Frozen sources and event identity

Frozen inputs (read-only):

| Source | Value |
|---|---|
| Source experiment | `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1` |
| Scanner | `SUPPORT_RESISTANCE_REACTION` (verified against `app/research/adapters/support_resistance.py` AND the frozen `app/research/prospective_registry.json` entry) |
| Direction | `SHORT` |
| Observation row | `research.prospective_observation` |

Before capture the runtime positively confirms ALL of:

1. feature flag `SRR_SHORT_FILL_CAPTURE_ENABLED=1`;
2. caller `scanner_name`/`direction` match the frozen constants;
3. registry + adapter static identity check (cached; any I/O problem ⇒ deny);
4. each candidate observation id resolves to a row with
   `experiment_id = SOURCE_EXPERIMENT_ID`, `direction = 'SHORT'`, the scanned
   `symbol`, a `signal_time`/`reference_price` equal to the candidate that
   produced the id, and a frozen geometry consistent with
   `structural_R = invalidation_price − reference_price` and
   `variant_stop = ref + 2.0·structural_R`,
   `variant_target = ref − 1.5·structural_R`.

The id list returned by `observe()` is therefore **never trusted wholesale**:
ids belonging to other experiments, other directions or other symbols are
ignored. `observation_id` is `UNIQUE` and FK-bound to
`research.prospective_observation(observation_id)`.

`event_key = sha256("SRR_SHORT_FILL_V1|<observation_id>|<symbol>|SHORT|
<signal_time>|<reference_price>")` — deterministic, stable across restarts.

---

## 4. Activation boundary

Two independent authorizations are required, both fail-closed:

1. **Runtime flag:** `SRR_SHORT_FILL_CAPTURE_ENABLED=1` (unset ⇒ no DB access
   at all).
2. **DB activation record** `research.srr_short_fill_activation_v1`:
   seeded `status='DISABLED'`, `activation_ts=NULL`. Capture requires
   `status='RUNNING'` **and** a non-NULL `activation_ts`.

Additional fail-closed configuration checks:

| Variable | Meaning | Default |
|---|---|---|
| `SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS` | allowed signal→quote-request delay; **must be set explicitly when enabled** | none (missing ⇒ deny) |
| `SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS` | optional; if set, must equal the DB `activation_ts` exactly (config/DB mismatch ⇒ deny) | unset |
| `SRR_SHORT_FILL_MAX_PER_CYCLE` | orderbook captures per scanner cycle | 5, hard cap 20 |
| `SRR_SHORT_FILL_MAX_PER_RUN` | orderbook captures per process | 200, hard cap 2000 |

Rules enforced in code and in the DB (`CHECK (signal_time > activation_ts)`):

* only `signal_time > activation_ts` (strict; equality rejected);
* **no backfill** — historical signals can never satisfy the boundary;
* the SRR OOS freeze timestamp (`2026-10-07T08:17:50Z`) is **not** reused as
  the fill boundary — it is a different, separately operator-set timestamp;
* the runtime **only reads** the activation table — restarting the Scanner
  can never move the boundary;
* `activation_ts` is immutable once set (trigger), status transitions are
  `DISABLED → RUNNING|CANCELLED`, `RUNNING → COMPLETED|CANCELLED`, terminal
  states are final, `DELETE` is prohibited;
* **no automatic activation after deploy/merge** — installation alone leaves
  the row `DISABLED`.

---

## 5. Data schema (`research.srr_short_fill_feasibility_v1`)

Two-phase write contract:

1. **Claim** — `INSERT … ON CONFLICT DO NOTHING` (unique on `event_key` and
   `observation_id`), committed **before** the Bybit request. A concurrent
   duplicate loses the claim and performs **no** API call.
2. **Finalize** — a single `UPDATE … WHERE event_key=%s AND
   feasibility_status='PENDING'`. The trigger allows exactly this one
   transition; finalized rows are immutable, `DELETE` is always prohibited.

Columns (grouped):

* **Identity:** `event_key`, `observation_id`, `experiment_id`,
  `source_experiment_id`, `scanner_name`, `symbol`, `direction`,
  `signal_time`, `activation_ts`, `reference_price`, `invalidation_price`,
  `structural_r`, `execution_r`, `variant_stop`, `variant_target`.
* **Timing:** `captured_at`, `capture_started_at`, `capture_finished_at`,
  `book_time_ms`, `book_age_ms`, `signal_to_capture_ms`, `api_request_ms`.
* **Quote:** `bid`, `ask`, `spread_bps`, `reference_to_bid_bps`,
  `depth_bid_10bps_usdt`.
* **Scenarios:** `requested_notional_usdt`, `requested_qty`, `executable_qty`,
  `min_order_qty`, `qty_step`, `min_notional`, `estimated_fill`,
  `entry_slippage_bps`.
* **Cost:** `observed_roundtrip_cost_pct`, `observed_cost_r`,
  `frozen_normal_cost_r`, `frozen_elevated_cost_r`,
  `extra_cost_r_vs_frozen_normal`, `cost_class`.
* **Verdict:** `quote_quality_status`, `feasibility_status`,
  `eligible_for_execution_stats`, `failure_reason`.
* **Evidence:** `exchange_constraints` (jsonb), `scenario_results` (jsonb),
  `source_diagnostics` (jsonb), `raw_snapshot` (jsonb — the raw orderbook).

Append-only trigger: no `UPDATE` except the `PENDING → finalized`
transition with all identity columns unchanged; no `DELETE` ever.

---

## 6. Cost model (SRR-adapted, single-counted)

Frozen geometry:

```text
structural_R = invalidation_price − reference_price
execution_R  = 2.0 × structural_R
variant_stop = reference_price + execution_R
variant_target = reference_price − 1.5 × structural_R
```

Observed quote-side round-trip cost of the model entry vs the executable
SHORT entry — each component counted exactly once:

```text
cost_pct = (reference − estimated_fill) / reference   # entry adverse deviation
         + (ask − bid) / mid                          # exit crosses the spread
         + 2 × FEE_PER_SIDE                           # taker fees, both sides
cost_r   = cost_pct × reference / execution_R         # frozen R normalization
extra_r  = cost_r − 0.0021 × reference / execution_R  # vs frozen NORMAL
```

* `cost_class ∈ {NORMAL, ELEVATED, ABOVE_ELEVATED}` classifies `cost_pct`
  against the frozen 0.21% / 31% round-trip thresholds.
* The formulas `gross − 0.21R = net_normal` and `gross − 0.31R =
  net_elevated` are **NOT** re-applied here on top of `cost_r` — that would
  double-count. `observed_cost_r` already contains fees, spread and entry
  deviation once.
* **No executable Net R is produced by this module.**
  `source_diagnostics.executable_net_r_computed = false` is stored on every
  row. `net_r_normal − extra_slippage_r` is explicitly NOT a valid
  executable Net R: changing the entry changes the TP/SL hit trajectory and
  the risk profile. A full execution-aligned outcome requires recomputing
  the candle path with the executable entry under the frozen geometry —
  an explicit **next stage** of this research (Stage 2), not part of this
  capture PR.

---

## 7. Quality statuses and eligibility

`quote_quality_status` (signal-time alignment, independent of book age):

| Value | Meaning |
|---|---|
| `FRESH` | `signal_to_capture_ms ≤ SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS` and the book itself is valid |
| `DELAYED` | quote valid but the signal→request delay budget was exceeded |
| `STALE_BOOK` | book older than 10 s at capture |
| `FUTURE_BOOK` | book timestamp more than 1 s in the future |
| `INVALID_BOOK` | missing/crossed/zero bid or ask |
| `NO_QUOTE` | the request failed entirely |

`feasibility_status` (base scenario = SMALL 100 USDT, mirrors onto the row):
`PENDING`, `OK`, `INSUFFICIENT_DEPTH`, `STALE_BOOK`, `FUTURE_BOOK`,
`INVALID_BOOK`, `BELOW_EXCHANGE_MIN`, `ERROR`.

`eligible_for_execution_stats = TRUE` requires **all** of:

* `quote_quality_status = 'FRESH'`,
* `feasibility_status = 'OK'`,
* exchange constraints **known** (`constraints.known = true`).

A fresh book alone never proves signal-time alignment: `book_age_ms` and
`signal_to_capture_ms` are recorded separately, and `DELAYED` rows are stored
but excluded from execution statistics. Unknown instrument constraints are
never "confirmed executable"; insufficient depth never gets an artificial
price (`estimated_fill = NULL`).

Per-scenario detail (SMALL/MEDIUM/LARGE) lives in `scenario_results`; the
row-level verdict reflects the SMALL scenario.

---

## 8. API budget and Scanner protection

* One `GET /v5/market/orderbook` per accepted event, plus
  `GET /v5/market/instruments-info` on cache miss (1 h per-symbol cache).
* All calls go through the existing `BybitClient._public_get`
  (`BYBIT_RETRY_BUDGET_HARDENING_V1`): ≤ 3 HTTP attempts per logical
  request, bounded per-attempt timeout, overall retry deadline
  (`bybit_retry_deadline_s`, default 60 s) and the **shared global rate
  limiter** — no bypass, no second retry layer, no infinite retries.
* Explicit budgets: `SRR_SHORT_FILL_MAX_PER_CYCLE` (reset once per
  `run_scan_cycle`) and `SRR_SHORT_FILL_MAX_PER_RUN` (reset only on process
  restart), both hard-capped (20 / 2000). Every denial is counted in
  `stats()["budget_denied"]` and logged once per cycle — coverage loss is
  observable, never silent.
* Duplicate protection: fast-path `event_key/observation_id` lookup →
  atomic `PENDING` claim → only the claim winner calls Bybit.
* Error/timeout/rate-limit outcomes are recorded as a row with
  `feasibility_status='ERROR'`, `quote_quality_status='NO_QUOTE'` and a
  typed `failure_reason` — never retried automatically, never raised.

**Scanner latency estimate:** a successful capture costs 1 orderbook GET +
(at most) 1 instruments GET ≈ 1 round-trip each (orderbook ≈ 100–300 ms
typical), synchronous in the cycle. Worst case is bounded by the shared
client deadline (60 s) only during persistent transport failure, and the
per-cycle budget caps how often that can happen (default ≤ 5, hard ≤ 20 per
cycle). Every capture stores `api_request_ms`, so the real latency
distribution is measured from the first activated row onward. If the
measured `api_request_ms` + `signal_to_capture_ms` profile shows the
synchronous capture pushing cycle latency beyond the scanner interval or
systematically delaying quotes past the delay budget, the sanctioned
alternative is a bounded dedicated worker with the SAME reservation-first
contract; it may not be introduced with a delayed quote substituted for a
missing signal-time snapshot.

---

## 9. Limits of historical reconstruction

* Impossible by construction: `signal_time > activation_ts` is enforced in
  Python and by a DB `CHECK`; there is no backfill code path.
* Rows written before an operator raises `SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS`
  do not exist (the flag cannot be on without the parameter).
* A crash between claim and finalize leaves a visible `PENDING` row: it is
  diagnosable (audit query below), never auto-retried (no duplicate API
  calls), and may be finalized manually by an operator (the only UPDATE the
  trigger permits) or left as-is.
* Rows with `eligible_for_execution_stats = false` are retained as evidence
  and excluded from execution statistics — coverage losses are counted, not
  hidden.

---

## 10. READ-ONLY audit commands

```sql
-- Activation state (must be DISABLED before operator activation)
SELECT experiment_id, status, activation_ts, updated_at
FROM research.srr_short_fill_activation_v1;

-- Capture volume / eligibility / quality mix
SELECT quote_quality_status, feasibility_status,
       eligible_for_execution_stats, COUNT(*),
       percentile_disc(0.5) WITHIN GROUP (ORDER BY signal_to_capture_ms)
           AS median_signal_to_capture_ms,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY book_age_ms) AS median_book_age_ms
FROM research.srr_short_fill_feasibility_v1
GROUP BY 1, 2, 3
ORDER BY 4 DESC;

-- Coverage: eligible share per symbol / per day.
-- PENDING rows (stuck claims after a crash) are NOT completed captures and
-- are reported separately, so they cannot inflate the coverage numerator.
SELECT symbol,
       COUNT(*) FILTER (WHERE feasibility_status <> 'PENDING')
           AS finalized_captures,
       COUNT(*) FILTER (WHERE feasibility_status = 'PENDING')
           AS stuck_pending,
       COUNT(*) FILTER (WHERE eligible_for_execution_stats) AS eligible
FROM research.srr_short_fill_feasibility_v1
GROUP BY symbol ORDER BY eligible DESC;

-- API budget pressure (from diagnostics)
SELECT source_diagnostics->'budget' AS budget_at_capture, COUNT(*)
FROM research.srr_short_fill_feasibility_v1
GROUP BY 1 ORDER BY 2 DESC;

-- Stuck claims (crash between claim and finalize)
SELECT event_key, observation_id, symbol, signal_time, capture_started_at
FROM research.srr_short_fill_feasibility_v1
WHERE feasibility_status = 'PENDING';

-- Cost distribution in frozen execution_R units (quote-side estimate ONLY)
SELECT cost_class, COUNT(*), AVG(observed_cost_r) AS avg_cost_r,
       AVG(extra_cost_r_vs_frozen_normal) AS avg_extra_r
FROM research.srr_short_fill_feasibility_v1
WHERE observed_cost_r IS NOT NULL
GROUP BY cost_class;

-- Join fill feasibility with the FROZEN SRR outcome (Stage 2 input);
-- the frozen outcome rows themselves are never modified by this join.
SELECT f.observation_id, f.symbol, f.signal_time,
       f.eligible_for_execution_stats, f.quote_quality_status,
       f.feasibility_status, f.estimated_fill, f.observed_cost_r,
       f.extra_cost_r_vs_frozen_normal,
       o.status AS frozen_outcome_status, o.is_final,
       o.net_r_normal, o.net_r_elevated, o.execution_r
FROM research.srr_short_fill_feasibility_v1 f
JOIN research.srr_short_execution_prospective_outcome o
  USING (observation_id)
ORDER BY f.signal_time;

-- Double-check: frozen SRR outcomes and old observations untouched
SELECT COUNT(*) FROM research.prospective_outcome;          -- unchanged
SELECT COUNT(*) FROM research.prospective_observation;      -- only INSERTs
```

---

## 11. Safe activation order (operator-only, never automatic)

1. Deploy the code + apply migration `082` (source-controlled; **not applied
   by this PR**). Confirm the seed row:
   `SELECT … FROM research.srr_short_fill_activation_v1;` → `DISABLED`.
2. Keep `SRR_SHORT_FILL_CAPTURE_ENABLED` **unset** in the Scanner service.
3. Fix the forward-protocol parameters (Section 12) EXPLICITLY: choose
   `SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS`, `SRR_SHORT_FILL_MAX_PER_CYCLE`,
   `SRR_SHORT_FILL_MAX_PER_RUN` and the expected activation timestamp; they
   are deliberately not hardcoded by this PR.
4. Freeze the boundary in one reviewed statement:

   ```sql
   UPDATE research.srr_short_fill_activation_v1
   SET status = 'RUNNING',
       activation_ts = '<operator-chosen UTC timestamp>',
       notes = 'Issue #259 forward protocol activation'
   WHERE experiment_id = 'SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'
     AND status = 'DISABLED';
   ```

   The trigger guarantees `activation_ts` can never change afterwards.
5. Set the runtime env (flag + delay budget + expected timestamp) and restart
   ONLY the Scanner service. There is no code path that activates capture on
   deploy.
6. Verify with the Section 10 queries after the first captures.

---

## 12. Forward protocol (proposed thresholds — operator fixes them at activation, not this PR)

These are protocol parameters; this PR deliberately does not write them into
production configuration. Proposed starting values below are a *proposal*
for the operator to adopt/adjust and record in `notes`:

| Parameter | Proposed value |
|---|---|
| Minimum eligible sample | ≥ 300 eligible captures (matches the frozen `minimum_n`) |
| Minimum observation window | ≥ 30 calendar days of post-activation signals |
| Minimum coverage | ≥ 70% of created SRR SHORT observations captured; eligible share ≥ 60% of captures |
| Minimum symbol breadth | ≥ 30 distinct symbols with ≥ 1 eligible capture |
| Signal→quote delay budget | `SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS` ≤ 5000 ms (median `signal_to_capture_ms` must be ≤ half of it) |
| API budget | per-cycle ≤ 5, per-run ≥ 1000 (coverage accounting must show budget denials < 5% of events) |

Decision rule (applied only to rows with
`eligible_for_execution_stats = true`, Stage 2 trajectory recomputation):

* **GO** — execution-aligned Net E[R] > 0 with day-cluster bootstrap 95% CI
  lower bound > 0, under NORMAL costs, on the full eligible sample, with
  every coverage/quality threshold above satisfied.
* **NO-GO** — execution-aligned Net E[R] ≤ 0, or its 95% CI upper bound ≤ 0,
  or the positive result only survives excluding a majority of symbols, or
  any coverage threshold is violated after the full window.
* **INCONCLUSIVE** — sample/window/coverage thresholds not yet reached, or
  the CI straddles zero: continue observing, do not change any verdict.

Until a GO is recorded: **`RESEARCH_POSITIVE / EXECUTION_UNVALIDATED /
PAPER_LIVE_NO_GO`**.

---

## 13. Formal stop conditions for this research contour

Stop capture (set the flag to 0 / `status='CANCELLED'`) if any occurs:

1. budget denials exceed 20% of SRR SHORT events for 3 consecutive days;
2. median `signal_to_capture_ms` exceeds the configured delay budget;
3. `ERROR`/`NO_QUOTE` rate exceeds 10% of captures;
4. any measurable effect on Scanner cycle completion (compare
   `CYCLE_COMPLETION` rows pre/post activation);
5. any doubt about identity integrity (rows whose `observation_id` cannot be
   re-joined to `research.prospective_observation`);
6. any write outside the two new tables is detected in an audit.

Stopping is always safe: no trading object depends on this contour.

---

## 14. Rollback plan

1. `SRR_SHORT_FILL_CAPTURE_ENABLED` unset (or set to `0`) + Scanner restart →
   capture stops immediately (first gate, no DB access).
2. `UPDATE … SET status='CANCELLED' …` (allowed from `DISABLED` or `RUNNING`)
   → permanent deactivation; the trigger forbids any later reactivation and
   forbids changing/clearing `activation_ts`.
3. Data: **retained deliberately** — evidence rows are append-only and
   cannot be deleted by triggers; if a clean restart of the contour is ever
   required, a reviewed migration (new number) may archive the table.
4. Migration rollback: none required for correctness (tables are additive);
   dropping them, if ever decided, must be a separate reviewed migration.
5. Frozen SRR evaluator, historical observations/outcomes, Scanner gates,
   Paper and Live require **no** rollback — they were never touched.

---

## 15. Remaining risks / requirements before activation

* Stage 2 (execution-aligned trajectory recomputation) does not exist yet —
  without it no executable Net R can be claimed.
* Synchronous capture adds up to (budget per cycle) × (1–2 public GETs) to
  the cycle; must be observed via `api_request_ms` after activation.
* Delay-budget and coverage thresholds must be fixed by the operator at
  activation (deliberately absent from production config).
* `PENDING` claim rows after a crash need an operator follow-up (documented,
  not auto-retried by design).
* Migration `082` must be applied through the normal reviewed migration
  path on each environment; it is NOT applied by this PR.

---

## 16. Test evidence

* `tests/test_srr_short_fill_feasibility_v1.py` — 76 offline tests: flag off,
  missing/mismatched activation, pre-activation boundary, wrong
  experiment/direction/scanner/symbol/geometry, multiple observation ids,
  duplicate event, concurrent claim, scanner restart, PostgreSQL failure,
  finalize failure, Bybit failure / rate-limit / timeout, empty / stale /
  future / crossed book, future signal_time, delayed quote, insufficient
  depth, unknown constraints, quantity rounding, min notional, no partial
  fill, `execution_R` normalization, no double counting, budget limits,
  wrapper routing/isolation/ordering, no order-submission surface.
* `tests/test_srr_short_fill_feasibility_migration_082_v1.py` — 14 static
  contract tests for migration 082 (DISABLED seed, immutability triggers,
  append-only evidence, status enums, column contracts, no trading object).
* All tests use mock Bybit clients and fake connections — no network, no
  PostgreSQL, no real orders.
