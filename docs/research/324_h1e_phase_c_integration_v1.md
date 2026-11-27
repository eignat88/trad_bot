# H1-E — Phase C: Integration Implementation (Issue #324)

Phase C integrates the frozen Phase A observer core with deployable adapters
and validates them against an isolated test PostgreSQL. Nothing is merged,
deployed, DDL-applied or activated by this work.

## C.1 PostgreSQL source reader — `app/research/me_entry_quality_h1e_infra_v1/pg_source_reader.py`

- reads ONLY `research_me_upstream_coverage.source_event` (SELECT; no writes anywhere);
- passes `source_event_key`, `source_identity`, `signal_time`, `lineage`, `lineage_hash`
  VERBATIM (no normalization, no re-derivation);
- composite cursor `(captured_at, source_event_key)` with strict `>` boundary;
- bounded batches (`limit`), optional `ceiling_captured_us`;
- least privilege: works with the SELECT-only grant of `deploy/sql/324_h1e_observer_role.sql`;
- exact µs ↔ TIMESTAMPTZ conversion (`us_to_datetime`/`datetime_to_us`, no floats);
- fail-closed validation of every row handed downstream (`admit_source_row`).

## C.2 PostgreSQL ledger store — `app/research/me_entry_quality_h1e_infra_v1/pg_ledger_store.py`

- implements the FROZEN Phase A `H1EStore` contract (`lookup` / `pending_retries` /
  `commit_batch`) against the reviewed DDL proposal;
- ONE transaction per `commit_batch`: events, append-only classification/status/reject/
  conflict journals, durable pending-retry state and the delivery checkpoint commit or
  roll back TOGETHER (no other write path);
- exactly-once effect: idempotent first-capture insert (`ON CONFLICT DO NOTHING`),
  identical redelivery is a no-op (last-journal classification comparison),
  conflicts recorded exactly once per conflict key;
- durable DEFERRED state survives restart (verified); backlog is bounded per cycle;
- fail-closed twice: the store raises the frozen exception classes
  (`NoApprovedActivation`, `ActivationFingerprintMismatch`, `FreezeTsMismatch`,
  `PreFreezeWrite`, `LedgerIdentityConflict`, `CheckpointRegression`) AND the
  database triggers refuse direct-write bypasses (translated back via error map);
- classification records round-trip losslessly (`diagnostics` carries the
  `captured_at_us` journal fact; all report fields map to dedicated columns);
- `fault_inject_commit` hook for atomicity tests (tests only).

## C.3 HTF transport adapter — `app/research/me_entry_quality_h1e_v1/htf_transport.py`

- wraps the frozen H1-D `fetch_htf_history` (the ONLY transport entry; shared
  limiter / retry budget / circuit breaker apply);
- interval mapping `240→4H`, `D→1D`; frozen ASOF window: last candle closed at
  the original ASOF, exactly 60 candles of lookback, NO later candles;
- rows beyond the ASOF close are dropped and never reach the classifier;
- bounded API budget (`request_budget`, `max_requests_per_call`, `HARD_REQUEST_CAP`);
  budget exhaustion defers (`H1EHtfApiBudgetExceeded`), never bypasses limits;
- error map: limiter/rate-limit/breaker/timeout → bounded DEFERRED with explicit
  `H1E_HTF_*` codes; API/cap/row-conflict → terminal non-eligible; nothing is
  retried past `max_deferrals`;
- offline tests use injected fakes — ZERO live fetches.

## C.4 isolated worker + systemd proposal

- `app/research/me_entry_quality_h1e_infra_v1/worker.py`: `main()` runs NOTHING at
  import time and is DISABLED by default (exit 3, no SQL, no API);
- explicit gates, all fail closed: `H1E_WORKER_ENABLE`, `H1E_DEPLOY_GATE`,
  `H1E_DDL_APPLIED`, `H1E_FREEZE_TS_US`, `H1E_ACTIVATION_ID`,
  `H1E_PROTOCOL_FINGERPRINT`, `H1E_APPROVAL_REF`, `H1E_APPROVED_BY`;
- bounded work: `batch_limit=200`, `retry_batch_limit=50`, `max_attempts=3`;
- checkpoint-based resume (restart-safe), SIGINT/SIGTERM finish the current cycle;
- `--health` = read-only probe (SELECT only, no API);
- separate unit `deploy/systemd/trad-bot-h1e-observer.service` (TEMPLATE ONLY,
  separate user/credentials/logging, `SuccessExitStatus=3`), separate env file
  `deploy/env/h1e-observer.env.example` (all gates ship as 0);
- no backfill path and no OOS transition exist in the worker.

## DDL package (proposals — applied ONLY to the test database by CI tests)

- `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` — isolated schema
  `research_me_entry_quality_h1e` (10 tables, append-only triggers, prospective
  guard, checkpoint monotonicity);
- `sql/proposals/324_h1e_prospective_ledger_v1_rollback.sql` — rollback + recovery
  semantics (single transactional DROP SCHEMA after evidence backup; idempotent
  re-apply after a crashed apply);
- `deploy/sql/324_h1e_observer_role.sql` + `324_h1e_observer_recovery.sql` —
  least-privilege role (SELECT on #305 source; writes only inside the H1-E schema;
  `h1e_activation` administrative contour) and recovery contour.

## Test matrix (mandatory 13 rows)

| # | Case | Test |
|---|---|---|
| 1 | source reader pagination | `test_source_reader_pagination_preserves_every_event` |
| 2 | cursor + restart | `test_source_reader_cursor_restart_and_ceiling` |
| 3 | PostgreSQL commit failure → rollback | `test_commit_failure_rolls_back_the_whole_batch` |
| 4 | DEFERRED + restart | `test_deferred_row_survives_restart` |
| 5 | backlog > limit | `test_backlog_larger_than_cycle_limit_is_processed_in_bounded_cycles` |
| 6 | concurrent redelivery exactly-once | `test_concurrent_redelivery_is_exactly_once` (+ `test_redelivery_after_restart_never_duplicates`) |
| 7 | pre-freeze reject (store) | `test_pre_freeze_event_rejected_by_store_and_by_trigger` |
| 8 | freeze/fingerprint fail-closed (DB trigger) | same test + `test_freeze_and_fingerprint_fail_closed` |
| 9 | HTF 59/60 non-eligible | `test_htf_59_of_60_is_non_eligible` |
| 10 | HTF future candle unused | `test_future_candles_never_reach_the_classifier` (+ `test_window_returns_only_candles_closed_at_the_original_asof`) |
| 11 | limiter denied / timeout → bounded defer or terminal | `test_limiter_denied_defers_bounded_then_terminal`, `test_timeout_defers_with_explicit_code`, `test_breaker_and_rate_limit_map_to_explicit_codes`, `test_api_budget_bounds_total_requests` |
| 12 | worker default OFF — no SQL/API | `test_worker_default_off_exits_without_db_or_api` (+ gate matrix) |
| 13 | existing services isolation | `test_existing_services_are_isolated`, `test_systemd_unit_is_disabled_by_default_template` |

## CI / test report

- commit SHA of this branch: recorded in the PR description and the issue comment
  (see `PHASE_C_INTEGRATION_READY_FOR_REVIEW`);
- H1-E suite incl. Phase C: see CI run (`Main PR Safety`, `Bybit Retry Budget CI V1`);
- PG integration tests run against a SCRATCH database on an isolated test
  PostgreSQL and SKIP cleanly when it is unreachable — never against a working DB.
