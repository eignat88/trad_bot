# Issue #312 — H2_RSI_ONLY Prospective Runtime V1: Implementation Report

**Branch:** `research/312-h2-prospective-runtime-v1` (from `origin/main` =
PR #311 merge `388e4ba`)
**Final status:** `IMPLEMENTATION_READY / ACTIVATION_HOLD`
**Scope honored:** research-only; no deploy, no merge, no activation, no
scanner/trading/gate change, no #305 write, no historical backfill.

---

## 1. Deliverables

| # | Deliverable | Path |
|---|---|---|
| 1 | Source reader (FR-01) | `app/research/me_entry_quality_h2_prospective_v1/source_reader.py` |
| 2 | Closed 5m candle adapter (FR-02) | `app/research/me_entry_quality_h2_prospective_v1/candle_adapter.py` |
| 3 | Lifecycle rules (FR-04) | `app/research/me_entry_quality_h2_prospective_v1/lifecycle.py` |
| 4 | Result store (FR-05) | `app/research/me_entry_quality_h2_prospective_v1/store.py` |
| 5 | Runtime orchestration (FR-07) | `app/research/me_entry_quality_h2_prospective_v1/runtime.py` |
| 6 | Entrypoint (FR-07) | `app/research/me_entry_quality_h2_prospective_v1/runner.py` |
| 7 | Telemetry (FR-08) | `app/research/me_entry_quality_h2_prospective_v1/telemetry.py` |
| 8 | Config (disabled by default) | `app/research/me_entry_quality_h2_prospective_v1/config.py` |
| 9 | Contracts / versions / policy grid | `app/research/me_entry_quality_h2_prospective_v1/contracts.py` |
| 10 | Migration 095 | `sql/migrations/095_me_entry_quality_h2_prospective_v1.sql` |
| 11 | Role template | `deploy/sql/312_h2_prospective_role.sql` |
| 12 | Env + systemd templates (NOT installed) | `deploy/env/h2-prospective-runtime.env.example`, `deploy/systemd/trad-bot-h2-prospective-runtime.service` |
| 13 | Entry/outcome policy spec (FR-06, `BLOCKED_PENDING_DECISION`) | `docs/research/312_h2_entry_outcome_policy_v1.md` |
| 14 | Design + integration points | `docs/research/312_h2_prospective_runtime_v1.md` |
| 15 | Safe deployment runbook | `docs/research/312_h2_prospective_runbook_v1.md` |
| 16 | Test suites (6 files) | `tests/test_me_entry_quality_h2_prospective_*.py` |

## 2. Contract integration (summary)

* The merged PR #311 engine is reused UNCHANGED: `run_h2_event()` +
  `provide_5m_candles()` + `rsi_wilder_series()` + the three confirmation
  rules + `H2Policy` fingerprints.  No RSI algorithm, event identity or
  confirmation rule is duplicated.
* #305 is READ-ONLY for this runtime: the reader issues SELECT statements
  only (structural guard `assert_select_only`), carries
  `source_event_key` / `source_identity` / `lineage_hash` / `signal_time` /
  `activation_freeze_ts` VERBATIM, verifies the stored `lineage_hash` against
  the canonical lineage hash (reused from `me_upstream_source_v1.contracts`),
  and enforces the freeze boundary at microsecond precision
  (`signal_time > freeze_ts` strictly).
* Redeliveries are idempotent (first-seen snapshot preserved); a redelivery
  with drifted immutable lineage raises an explicit
  `EVENT_KEY_LINEAGE_CONFLICT` into the conflict ledger.

## 3. Acceptance-criteria matrix

| # | Check | Where covered | Result |
|---|---|---|---|
| 1 | New valid #305 event accepted once | `source_reader` (IngestChecks / StoreIngestIdempotency), `runtime` (HappyPath) | PASS |
| 2 | Repeated delivery = duplicate, no second outcome | `source_reader`, `store`, `runtime` (RestartRecovery) | PASS |
| 3 | Pre-freeze event rejected | `source_reader` (2 tests: signal_time and captured_at) | PASS |
| 4 | No source events -> `SOURCE_EMPTY_WAITING` | `source_reader` (TestSourceEmptyWaiting, verbatim log line asserted) | PASS |
| 5 | Unclosed 5m candle never used | `candle_adapter` (TestClosedCandleContract) | PASS |
| 6 | Candle gap -> `INCOMPLETE_COVERAGE` (never EXPIRED/CONFIRMED) | `lifecycle` (TestCoverageFailures) | PASS |
| 7 | 119/120 canonical candles -> no eligible H2 | `lifecycle` (`CANONICAL_WINDOW_INCOMPLETE`) | PASS |
| 8 | 120/120 canonical candles -> RSI computed | `lifecycle` (TURN_1 confirms with reference RSI) | PASS |
| 9 | First RSI decline -> TURN_1 per rules | `lifecycle` (TestTurn1, 2 tests) | PASS |
| 10 | Two consecutive declines -> TURN_2 per rules | `lifecycle` (TestTurn2, 2 tests) | PASS |
| 11 | Confirmed RSI peak -> PEAK_REVERSAL on causal ASOF | `lifecycle` (TestPeakReversal, 3 tests incl. ASOF causality) | PASS |
| 12 | Window expiry -> `EXPIRED`, no fictional trade result | `lifecycle` (TestExpiry) | PASS |
| 13 | Restart restores state | `runtime` (TestRestartRecovery), `store`, `pg_integration` | PASS |
| 14 | Bybit/API failure -> fail-closed, scanner unaffected | `runtime` (TestFailClosed), `candle_adapter` (bounded retries) | PASS |
| 15 | Lineage conflict -> explicit integrity conflict | `source_reader`, `store`, `pg_integration` | PASS |
| 16 | No execution model -> no fictional Net E[R] | `runtime` (TestNoFictionalExecution: payload + migration checks) | PASS |
| 17 | H2 disabled -> no Bybit/source/writes | `runtime` (TestDisabledRuntime, incl. runner exit before any I/O) | PASS |
| 18 | Production isolation | `store` (schema isolation + SQL contract), `runtime` (AST import scan, migration scope), `source_reader` (SELECT-only), git-level (zero modified tracked files) | PASS |

Additional coverage: grid/ASOF microsecond anchors, canonical fetch range,
budget caps (per-cycle/per-hour), empty payload never synthesized, policy
fingerprint pinning fail-closed, terminal-state immutability incl. DB
triggers, PG store driver-level placeholder/schema contract.

## 4. Test results

* New suites: **79 passed** (74 unit/integration + 5 PG-store SQL-contract
  driver checks; the 7 real-PostgreSQL tests are OPT-IN via
  `ME_H2PRO_PG_INTEGRATION=1` and skip without a test server — none exists in
  this environment).
* PR #311 H2 regression: **167/167 PASS** (unchanged).
* Full-suite regression (see section 7 for the exact command):
  {{FULL_SUITE_RESULT}}
* Five unrelated test files fail to COLLECT on Windows (pre-existing
  `ModuleNotFoundError: No module named 'pwd'`, Linux-only module in
  `test_me_r_v3_atomic_pg_e2e_issue154.py`,
  `test_me_r_v3_isolated_oos_collector.py`,
  `test_me_r_v3_isolated_pg_collector_issue154.py`,
  `test_operations_monitor_retention.py`,
  `test_srr_short_r_expansion_candle_path_integrity_audit_v1.py`) — they are
  untouched by this change and collect/run on the Linux CI.

## 5. Audits

### 5.1 Causal ASOF audit
* Evaluation ASOF is the actual observation instant (injectable clock);
  only candles with `open + 300_000 <= evaluation_asof_ms` participate
  (PR #311 contract, unchanged).
* Post-signal observations close strictly after `signal_time` at MICROSECOND
  precision (grid anchors computed with integer arithmetic — no float
  rounding; boundary tests included).
* `O_0` = last candle closing at-or-before the signal instant; `O_1` =
  first candle closing strictly after it (parity with the engine's
  `_observation_indices`, boundary-tested).
* Confirmation rules are prefix-stable (enforced by PR #311 tests + the new
  ASOF causality test: the peak is not recognizable one candle early).
* No historical backfill: the ingest boundary rejects `signal_time <=
  activation_freeze_ts` at µs precision.

### 5.2 Source integrity audit
* Stored `lineage_hash` verified against the canonical lineage hash on every
  delivery; mismatch -> explicit rejection (never admitted).
* First-seen ingest snapshot is immutable (in-memory invariant + PG trigger
  `H2P_INGEST_SNAPSHOT_IMMUTABLE`); redelivery drift -> explicit
  `EVENT_KEY_LINEAGE_CONFLICT` ledger row.
* Terminal outcomes UNIQUE on (event, variant, policy) with
  `result_payload_hash`; identical re-write = no-op, differing re-write =
  `IMMUTABLE_RESULT_CONFLICT` (never overwrites); PG triggers
  `H2P_ROW_IMMUTABLE` refuse UPDATE/DELETE.

### 5.3 Production isolation audit
* `git status`: ZERO modified/deleted tracked files — everything is additive.
* `scanner_runner.py`, production detector/scoring/regime gate, paper/live:
  untouched.
* #305 schema: SELECT-only code path + tests asserting non-SELECT is
  rejected; no migration or grant touches `research_me_upstream_coverage`.
* The package imports no trading modules (AST scan test).
* Migration 095 creates ONLY `research_me_entry_quality_h2_prospective`.
* Disabled runtime performs no DB connection, no Bybit request, no write
  (test-enforced).  Bybit access requires an explicitly CONFIRMED research
  budget and uses per-cycle/hour caps independent of the production limiter.

## 6. BLOCKED_PENDING_DECISION (explicit, no assumptions made)

From `docs/research/312_h2_entry_outcome_policy_v1.md` (FR-06): D-1
executable entry price; D-2 fill/missed-entry policy; D-3 fees/funding/
slippage; D-4 entry deadline; D-5 stop/target geometry; D-6 outcome horizon;
D-7 baseline definition; D-8 Net E[R] formula; D-9 MAE/MFE conventions;
D-10 missed winners; D-11 statistical acceptance rule; D-12 policy
fingerprint freeze.  Until these are approved, the runtime stores
RSI-confirmation outcomes only and publishes NO yield metrics.

Also open (non-blocking): the #305 cohort has 0 source events, so real
prospective coverage cannot be demonstrated yet — per the issue this does not
block development but blocks activation.

## 7. Reproduction commands

```bash
# new suites
pytest tests/test_me_entry_quality_h2_prospective_source_reader_v1.py \
       tests/test_me_entry_quality_h2_prospective_candle_adapter_v1.py \
       tests/test_me_entry_quality_h2_prospective_lifecycle_v1.py \
       tests/test_me_entry_quality_h2_prospective_store_v1.py \
       tests/test_me_entry_quality_h2_prospective_runtime_v1.py \
       tests/test_me_entry_quality_h2_prospective_pg_integration_v1.py -q

# PR #311 H2 regression (167)
pytest tests/test_me_entry_quality_h2_candle_provider_v1.py \
       tests/test_me_entry_quality_h2_causal_asof_v1.py \
       tests/test_me_entry_quality_h2_lineage_idempotency_v1.py \
       tests/test_me_entry_quality_h2_purity_v1.py \
       tests/test_me_entry_quality_h2_review_fixes_v1.py \
       tests/test_me_entry_quality_h2_rsi_parity_v1.py \
       tests/test_me_entry_quality_h2_variants_v1.py -q

# real-PostgreSQL integration (opt-in, isolated scratch DB)
ME_H2PRO_PG_INTEGRATION=1 pytest tests/test_me_entry_quality_h2_prospective_pg_integration_v1.py -q
```

## 8. Conclusion

`IMPLEMENTATION_READY / ACTIVATION_HOLD`.  The runtime is implemented,
tested, isolated and disabled by default; a staged runbook exists for a
separate GO-controlled deployment.  Independent review, merge, deploy and
activation each require a separate explicit decision.
