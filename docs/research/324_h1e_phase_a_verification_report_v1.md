# #324 H1-E — Phase A Verification Report

**Branch:** `research/324-h1e-prospective-observer-v1` (Draft PR #325, base `main` @ `03d6fac466e2f9ddd890803649db54b13a451ba3`)
**Environment:** Windows dev host, Python 3.13 venv, offline (no network
calls, no production DB writes, no DDL applied, no services touched)
**Status:** `PHASE_A_OFFLINE_IMPLEMENTATION / NO_DEPLOY / NOT_ACTIVATED / H1_OOS_HOLD`

---

## 1. Deliverables → criteria mapping

| # | Contract | Implementation | Offline tests |
| --- | --- | --- | --- |
| 1 | `H1E_SOURCE_READINESS_V1` | `app/research/me_entry_quality_h1e_v1/source_readiness.py` | `test_me_entry_quality_h1e_readiness_v1.py` (8 tests) |
| 2 | `H1E_PROSPECTIVE_EVENT_CAPTURE_V1` | `capture.py` (+ `contracts.py`) | `test_me_entry_quality_h1e_capture_v1.py` (12 tests) |
| 3 | `H1E_HTF_ASOF_CLASSIFICATION_V1` | `classification.py` (H1-C `run_h1_event` + H1-D `240`/`D` + `H1D_HTF_SNAPSHOT_2`, unchanged) | `…_classification_v1.py` (19), `…_htf_failures_v1.py` (8) |
| 4 | `H1E_PROSPECTIVE_LEDGER_V1` | `ledger.py` + `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` (DO NOT APPLY) | `…_ledger_v1.py` (8) |
| 5 | `H1E_COVERAGE_HEALTH_V1` | `coverage_health.py` | `…_coverage_health_v1.py` (11) |
| 6 | `H1E_OOS_READINESS_V1` | `oos_readiness.py` + `docs/research/324_h1e_oos_readiness_protocol_v1.md` | `…_oos_readiness_v1.py` (7) |

Cross-cutting: `protocol.py` (freeze/exclusions) →
`…_protocol_freeze_v1.py` (11); provenance tamper →
`…_provenance_tamper_v1.py` (8); isolation/purity/determinism →
`…_isolation_purity_v1.py` (6).  Total: 98 tests.

## 2. Exact test results

Commands (from the PR worktree; interpreter = project venv):

```
python -m pytest tests/test_me_entry_quality_h1e_*.py -q
    → 98 passed

python -m pytest tests/test_me_entry_quality_h1*.py tests/test_me_upstream_source_*.py -q
    → 681 passed, 26 skipped
```

The second command is the targeted H1-A/B (#297) + H1-C (#308/#309) +
H1-D (#314/#315) + #305 source regression suite, covering the frozen
contracts H1-E reuses (classification, coverage, provenance, fetch
adapter, capture/identity).  Skips are the pre-existing DB-gated
integration tests (`ME_EQ296_PG_INTEGRATION`-style gates) — no new skips.

Broader offline suite (whole `tests/` minus 5 pre-existing
Windows-incompatible collection modules — `pwd`/`fcntl`/missing tool
module):

```
python -m pytest tests --ignore=<5 pre-existing broken-on-Windows modules> -q
    → 87 failed, 8612 passed, 375 skipped, 94 errors (24:13 min)
```

Full-suite analysis (verified against the PR base commit `03d6fac`):

* the failing/erroring modules are ALL untouched by this PR (DB-gated
  analytics/migration/PostgreSQL suites, paper/LR/SRR modules, the
  pre-existing `sql/migrations/097_…` H2 needle mismatch, …);
* the same module set re-run on a pristine worktree of `03d6fac`
  reproduces **85 failed + 94 errors** with an **identical failure list**;
  the only delta in the PR worktree is exactly the two
  `test_htf_baseline_short_phase2_isolation_v1.py` PR-diff confinement
  tests (`test_no_tracked_file_modified_against_base`,
  `test_new_files_are_confined_to_phase2_surface`);
* those two are, by the repository's own contract
  (`tests/test_htf_baseline_short_phase2_scope_routing_v1.py`,
  `CONFINEMENT_TESTS`), Phase-2-ONLY PR surface checks that are **not run**
  for `NO_PHASE2`-routed PRs ("meaningless — and PR-blocking — outside a
  Phase-2-only PR"); this PR touches zero governed Phase-2 paths (no
  `app/research/htf_baseline_short_phase2_*`, no FROZEN_PATHS, no
  `main-pr-safety.yml`) and therefore routes `NO_PHASE2`;
* raw logs: `reports/324_h1e/{h1e_tests_final,h1_regression_final,full_suite_h1e,failing_modules_on_base}.txt`.

## 3. Scenario matrix (all offline, deterministic)

| Required scenario | Evidence | Result |
| --- | --- | --- |
| repeated delivery / restart | `…_capture_v1.py::test_redelivery_is_idempotent_exactly_once_effect`, `::test_retry_on_interrupted_transaction_yields_identical_outcome` | idempotent DUPLICATE, exactly-once effect, unchanged outcome |
| crash between read/classify/commit | `::test_commit_crash_is_all_or_nothing_then_restart_recovers`, `::test_classifier_crash_between_read_and_commit_leaves_no_effects` | nothing applied on crash; restart from old checkpoint converges |
| 60/60 4H + 1D | `…_classification_v1.py::test_all_nine_combinations_eligible_with_60_of_60` (9 params) | eligible; all nine 4H×1D regimes retained verbatim |
| 59/60 | `::test_59_of_60_is_non_eligible_with_exact_missing_bucket` | non-eligible `H1E_HTF_INCOMPLETE_COVERAGE`, exact missing bucket |
| gap inside window | `::test_gap_inside_series_is_invalid_source` | non-eligible, exact missing bucket |
| close boundary ±1 µs | `::test_close_boundary_inclusive_exact_close_us`, `::test_close_boundary_minus_one_microsecond_is_59_of_60` | inclusive at `close_us`; 59/60 one µs earlier |
| no look-ahead | `::test_future_append_cannot_change_an_earlier_classification`, `::test_classification_asof_is_signal_time_never_captured_at` | ASOF = `signal_time` µs; future rows ignored |
| limiter/breaker/429/10006/timeout | `…_htf_failures_v1.py` (8 tests) | bounded deferral → terminal fail-closed codes; never a market mode |
| tamper detection | `…_provenance_tamper_v1.py` (8 tests) | row/lineage/ASOF/row-count/schema tamper → `ProvenanceTamperError`; replay byte-identical |
| identity collision | `…_capture_v1.py::test_identity_collision_is_isolated_fail_closed` | conflict isolated, original preserved verbatim |
| atomic checkpoint | `…_ledger_v1.py::test_checkpoint_is_atomic_with_the_ledger_insert`, `::test_checkpoint_regression_is_rejected` | all-or-nothing; cursor never regresses |
| pre-freeze / exclusions | `…_protocol_freeze_v1.py` (10 tests) | `signal_time <= freeze_ts` → reject; UNVERIFIED window + SOXSUSDT QA excluded |
| deterministic identity (`PYTHONHASHSEED`) | `…_isolation_purity_v1.py::test_deterministic_identity_across_pythonhashseed` | byte-identical ledger/report JSON across hash seeds |
| no silent skips | `…_capture_v1.py::test_cycle_is_deterministic_and_fully_accounted` | `read == admitted + rejected + duplicates + conflicts` |

## 4. Identity & freeze compliance

* `source_event_key` / `source_identity` / `signal_time` (µs) /
  `lineage_hash` / `source_fingerprint` carried VERBATIM
  (`H1ESourceEvent`, `H1Result` lineage payload); the only recomputations
  are integrity re-derivations that must match (pinned to the frozen #305
  functions by `…_isolation_purity_v1.py::test_mirrored_hash_functions_…`).
* #305 freeze constant `2026-11-24T13:31:53.365952+00:00` preserved
  verbatim (test-pinned at exact µs epoch `1795527113365952`).
* H1-E freeze: `H1E_FREEZE_NOT_MINTED` — Phase A mints none;
  `H1EFreeze.from_owner_approval()` fail-closes on missing approval; the
  capture worker REFUSES synthetic fixture freezes outside offline tests.
* Unverified window `2026-11-24T13:31:53.365952+00:00 →
  2026-11-25T07:14:41.230516+00:00` and the 2026-11-25 SOXSUSDT QA example
  are excluded with explicit persisted reasons; SOXSUSDT is QA-only and
  outside every prospective denominator.

## 5. DDL proposal — additive only, not applied

`sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` — verified by
tests: `*** PROPOSAL ONLY — DO NOT APPLY ***` marker, CREATE-only
statements, sole schema `research_me_entry_quality_h1e`, append-only
immutability triggers, monotonic checkpoint trigger, no reference to
`dds.*` / `market.*` / `research_me_upstream_coverage.*` objects.  No DDL
was executed anywhere.

## 6. Findings & fixes during implementation

| # | Finding | Fix |
| --- | --- | --- |
| F1 | `parse_utc_us` interprets integer timestamps as epoch **ms**; fixture rows passed µs ints → wrong ASOF (0/60 coverage) | fixtures now use UTC-anchored datetimes (µs-exact); contract unchanged |
| F2 | H1-C kept a *diagnostic* regime on non-eligible outcomes; the #324 spec forbids inferring BULLISH/BEARISH/NEUTRAL from invalid data | `ClassificationRecord` exposes regime/BOS ONLY when eligible; diagnostics live in `h1d_diagnostics` |
| F3 | deferred classifications were deduplicated on redelivery (blocking bounded retry) | redelivery of a `DEFERRED` identity re-classifies (append-only journal, attempt counter) |
| F4 | test bug: future-append test compared two different histories | test now appends future candles onto the same history (spec scenario preserved) |
| F5 | pre-existing H1-D purity guard (`test_nothing_outside_the_package_references_h1d`) forbids ANY external reference to H1-D, conflicting with the #324 requirement to integrate the frozen H1-D adapter | **justified test-only amendment**: exactly `app/research/me_entry_quality_h1e_v1` is added as a sanctioned research-only consumer (asserted to be the ONLY one); no production file changed — flagged for reviewer attention |

## 7. Scope compliance checklist

`RESEARCH_ONLY` ✓ · `NO_DEPLOY` ✓ (no systemd/deploy changes) ·
`NO_PRODUCTION_DB_WRITES` ✓ · `NO_DDL_APPLY` ✓ (proposal file only) ·
`NO_LIVE_BYBIT_FETCH` ✓ (injectable transport; offline mocks) ·
`NO_EXISTING_SERVICE_RESTART` ✓ · `NO_BACKFILL` ✓ (strict pre-freeze
rejection; no historical admission) · `NO_OOS_ACTIVATION` ✓ (template
only).  Trading filters / scanner / paper-live / existing OOS / #305
freeze: untouched.

## 8. Remaining blockers (unchanged)

1. H1-E `freeze_ts` minting — Phase C + owner GO (Phase A cannot).
2. #305 `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` (#296) — readiness stays
   `HOLD/UNKNOWN`, never `PASS`.
3. Phase B VPS read-only preflight (real SQL reader + real HTF transport
   through the shared limiter) — separate GO.
4. OOS policy freeze (thresholds, execution model, unchanged control) —
   separate review + GO.
5. DDL application — Phase C decision.
