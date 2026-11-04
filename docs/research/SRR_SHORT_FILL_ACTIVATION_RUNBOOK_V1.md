# SRR SHORT Fill Feasibility — Activation Runbook (V1)

**Status until operator decision:**
`MERGED_BASELINE / HARDENING_PENDING / CAPTURE_DISABLED / PAPER_LIVE_NO_GO`

* Contour: `SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1` (Issue #259).
* Baseline: PR #260 merged into `main` (evidence schema `082`, capture
  module, P1 full-latency eligibility).
* This runbook covers the pre-activation hardening (F1 latency
  protection, F3 coverage ledger `083`, F4 forward protocol) — **the
  capture is disabled by default and this document activates nothing by
  itself.**
* Not in scope here, ever: deployment to any VPS, capture activation,
  Paper/Live changes, frozen SRR outcome changes, scanner gates/scoring,
  Issue #261 (AC7/ME) fixes.

---

## 1. Safe configuration (all values are fail-closed)

| Environment variable | Required | Default | Hard cap | Safe starting value |
|---|---|---|---|---|
| `SRR_SHORT_FILL_CAPTURE_ENABLED` | to arm capture | **unset (off)** | — | keep **unset** until step A5 |
| `SRR_SHORT_FILL_MAX_QUOTE_DELAY_MS` | yes, when armed | none (refuses) | — | `5000` |
| `SRR_SHORT_FILL_MAX_PER_CYCLE` | no | `5` | `20` | `5` |
| `SRR_SHORT_FILL_MAX_PER_RUN` | no | `200` | `2000` | `≥ 1000` (F4 §12.3.6) |
| `SRR_SHORT_FILL_MAX_CYCLE_LATENCY_MS` (F1) | no | `5000` | `60000` | `5000` |
| `SRR_SHORT_FILL_MAX_CAPTURE_MS` (F1) | no | `2000` | `60000` | `2000` (must be ≤ cycle window) |
| `SRR_SHORT_FILL_EXPECTED_ACTIVATION_TS` | recommended | unset | — | exact ISO-8601 of the chosen `activation_ts` |

Rules enforced by the module itself (any violation → capture refuses,
`config_denied`, coverage row `POLICY_DENIED`):

* delay budget must be a positive integer when armed;
* `MAX_CAPTURE_MS ≤ MAX_CYCLE_LATENCY_MS`, both positive;
* `MAX_PER_*` values are clamped to their hard caps;
* expected activation timestamp must parse and match the DB record.

Shared client settings (unchanged, no bypass): `bybit_retry_deadline_s`
(default 60 s) and the global Bybit rate limiter — SRR capture uses the
same budgeted `_public_get` as everything else (≤ 2 GETs per accepted
event: instruments on cache miss + orderbook).

## 2. Pre-activation checklist (all must pass)

- [ ] Migration `082_srr_short_fill_feasibility_v1.sql` applied → seed
      row `status='DISABLED'`, `activation_ts IS NULL`.
- [ ] Migration `083_srr_short_fill_coverage_v1.sql` applied → table
      `research.srr_short_fill_coverage_v1` exists (audit ledger only).
- [ ] `SRR_SHORT_FILL_CAPTURE_ENABLED` is **unset** in the Scanner
      service environment.
- [ ] Offline test suites green:
      `tests/test_srr_short_fill_feasibility_v1.py`,
      `tests/test_srr_short_fill_feasibility_migration_082_v1.py`,
      `tests/test_srr_short_fill_pre_activation_hardening_v1.py`.
- [ ] Forward-protocol parameters (§1 table) fixed in writing and the
      chosen values recorded for the activation `notes`.
- [ ] F4 decision criteria (doc §12) accepted as frozen — only tightening
      allowed at activation, never weakening.

## 3. Activation (operator-only, never automatic)

A1. Apply migrations `082` + `083` through the normal reviewed migration
    path on the target environment (source-controlled; **never applied by
    the PR itself**).
A2. Verify the seed: `SELECT experiment_id, status, activation_ts FROM
    research.srr_short_fill_activation_v1;` → `DISABLED`.
A3. Freeze the boundary in one reviewed statement:

    ```sql
    UPDATE research.srr_short_fill_activation_v1
    SET status = 'RUNNING',
        activation_ts = '<operator-chosen UTC timestamp>',
        notes = 'Issue #259 forward protocol activation; delay=<ms>;
                 per_cycle=<n>; per_run=<n>; cycle_latency_ms=<ms>;
                 capture_ms=<ms>; criteria=doc §12 frozen'
    WHERE experiment_id = 'SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'
      AND status = 'DISABLED';
    ```

    The trigger guarantees `activation_ts` can never change afterwards.
A4. Set the runtime env from §1 (flag stays unset for now) and restart
    ONLY the Scanner service. There is no code path that activates
    capture on deploy.
A5. Arm: set `SRR_SHORT_FILL_CAPTURE_ENABLED=1` (+ delay budget, latency
    budgets, expected timestamp) and restart the Scanner once more.
    From this moment every new SRR SHORT observation is attempted
    (claim → bounded quote → finalize) with F1/F3 accounting on.

## 4. First-hour verification (read-only, doc §10)

1. Activation row still `RUNNING` with the intended `activation_ts`.
2. First evidence rows: `SELECT … FROM
   research.srr_short_fill_feasibility_v1 ORDER BY captured_at DESC LIMIT 10;`
   — `signal_to_quote_ms` populated, `source_diagnostics.latency` present.
3. Coverage denominator query (doc §10 «F3 coverage») — denominator >
   0, `unaccounted` explained, dispositions sum sensibly.
4. F1 latency query (doc §10) — `worker_timed_out` should be `false`;
   `quote_phase_ms` distribution sane.
5. No writes outside the three research tables (doc §10 double-check).

## 5. Ongoing monitoring and stops

* Monitoring cadence: daily during the first week (coverage, latency,
  budget pressure, stuck `PENDING` claims).
* Stop conditions: doc §13 (budget/latency denials, ERROR rate, cycle
  effects, identity doubt, out-of-scope writes, coverage ledger
  failures).
* Rollback: doc §14 — flag unset (immediate), or `status='CANCELLED'`
  (permanent), data deliberately retained (append-only).
* Verdict discipline: only doc §12 (GO / NO-GO / INCONCLUSIVE, frozen
  superset of registry `validation_criteria`); Stage-2 recomputation
  only — **never** derive executable Net R by subtracting costs from the
  frozen Net E[R].

## 6. Additional API-load estimate (hardening delta)

* **Bybit calls: +0.** F1 wraps the existing ≤ 2 GETs in a bounded
  worker; F3 is DB-only. Request volume, retry policy, rate limiter and
  budgets are byte-identical to the merged baseline.
* Scanner wall time: bounded by ≈ `MAX_CYCLE_LATENCY_MS` per cycle
  (plus ms-level DB work), independent of Bybit slowness.
* Database: same scanner connection; per denied attempt ≤ 1 extra
  `SELECT` (already present) + 1 `INSERT`/`COMMIT` into the coverage
  ledger; while the flag is armed but the DB record is not `RUNNING`,
  each SRR SHORT observation costs exactly one such coverage row (small,
  bounded by observation rate; intentional — rejections must not be
  silent). Evidence writes are unchanged (claim + finalize).

## 7. Explicit non-goals of this runbook

No VPS deployment, no capture activation by the PR, no Paper/Live
touching, no frozen SRR outcome modification, no scanner gate/scoring
change, no Issue #261 (AC7/ME failed-counter) fix — that stays a
separate issue on `main`.
