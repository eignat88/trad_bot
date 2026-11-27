# #324 H1-E — Review Fix B4: Bounded Pending-Retry Processing

**Issue:** #324 · **PR:** #325 · **Branch:** `research/324-h1e-prospective-observer-v1`
**Base HEAD (pre-fix):** `e013588c25d59d275f262cfdb042b693f8b24f03`
**Scope:** ONLY defect B4 + its regression tests.  No feature expansion,
no Phase B/C work, no DB migration (the bound is enforced at the
processing level of the existing durable queue).

---

## 1. Root cause B4

`run_capture_cycle()` bounded NEW source rows with
`CaptureConfig.batch_limit`, but the DUE pending-retry queue was drained
WITHOUT any per-cycle bound: with a large backlog of `DEFERRED` events the
observer could execute an unbounded number of HTF classifications in one
cycle — unbounded load on the HTF provider / Bybit API / global rate
limiter and breaker, and an unpredictable cycle duration.

## 2. Implemented bound and default

`CaptureConfig.retry_batch_limit: int = 50` (independent of
`batch_limit`, which still bounds new source rows):

* **50** is the initial OFFLINE default for bounded work — it is NOT a
  production recommendation or approval of any production value;
* strictly positive `int`: `bool`, `None`, zero, negative and non-`int`
  values are rejected fail-closed (`ValueError` at cycle start);
* due entries are processed in the EXISTING deterministic queue order
  (`(next_attempt_us, ledger_identity)`) — unchanged;
* at most `retry_batch_limit` due entries are acted upon per cycle; every
  unprocessed due entry STAYS in the durable queue untouched;
* the limit NEVER deletes an entry and NEVER transitions anything to a
  terminal status; `max_attempts` / `H1E_RETRY_BUDGET_EXHAUSTED` policy is
  unchanged — the retry budget is consumed only by REAL classifier
  attempts, never by skipped cycles;
* no DB migration: the durable `h1e_pending_retry` table and the B3 DDL
  are untouched.

## 3. Diagnostics (additive `CycleReport` fields)

| Field | Exact semantics |
| --- | --- |
| `pending_due_total` | due pending-retry entries visible at cycle start |
| `pending_processed` | due entries actually processed this cycle (≤ `retry_batch_limit`; includes bounded terminal-budget transitions and stale drops, so all per-cycle queue work is bounded) |
| `pending_remaining` | **EXACT** durable queue size after the batch commit — on success computed from the atomic pending-update set (`pending_map` mirrors every `PendingRetryUpdate` 1:1, so the value equals `store.pending_retries()` after commit; asserted in tests); when the commit FAILS, the documented value is the unchanged pre-cycle queue size |
| `retry_batch_limit_reached` | `pending_due_total > pending_processed` — at least one due entry was left for later cycles |

The pre-existing `CycleReport` contract is untouched: `accounted_count`
(`admitted + rejections + duplicates + conflicts == read_count`) and all
previous fields keep their meaning; the four fields are additive
(covered by `test_cycle_report_keeps_reconciliation_contract_with_b4_diagnostics`).

## 4. Invariants preserved (B4.2, incl. the concurrent edge)

The previously accepted B1–B3 invariants are unchanged and re-verified:
atomic batch (pending state + journal + checkpoint), idempotent
redelivery, no re-classification of terminal identities, fail-closed
identity conflicts, crash/restart recovery of the queue, ASOF pinned to
the original `signal_time`, cursor advancement never removes pending
events.

**Concurrent edge (event in the pending queue AND redelivered by the
source in the same cycle)** — fixed: identities processed by the retry
loop in this cycle are tracked (`retried_this_cycle`) and their source
redelivery is accounted as `DUPLICATE` — never a second classification
and never a spurious self-heal re-queue (previously a redelivery in the
same cycle as its retry could re-queue an already-finalized identity).

## 5. Changed files

| File | Change |
| --- | --- |
| `app/research/me_entry_quality_h1e_v1/capture.py` | `retry_batch_limit` (+fail-closed validation), bounded due-entry processing in deterministic order, `retried_this_cycle` duplicate edge, 4 additive `CycleReport` fields + exact `pending_remaining` semantics |
| `tests/test_me_entry_quality_h1e_durable_retry_v1.py` | Mandatory Test 1 + Test 2 + budget/validation/crash-restart tests (6 new) |
| `tests/test_me_entry_quality_h1e_capture_v1.py` | B4 diagnostics / reconciliation-contract test (1 new) |
| docs | this report + implementation & verification docs updated |

`ledger.py` needed NO change (queue semantics already atomic); no DDL
change; H1-A/B/C/D, #305 source, frozen HTF classification, B3
activation/freeze DDL, trading gates and scanner/paper/live are untouched.

## 6. Mandatory regression tests — results

**Test 1 — backlog (120 due, `retry_batch_limit = 50`):**
`test_backlog_larger_than_retry_batch_limit_is_drained_bounded` — PASS.
Cycles: processed 50 / remaining 70 → processed 50 / remaining 20 →
processed 20 / remaining 0 (asserted exactly).  Verified: no cycle exceeds
the limit; all 120 events processed exactly once (none lost, none
duplicated); no re-classification of already-terminal events (120 total
classifier calls); deterministic processing order = the documented queue
order prefix (asserted per cycle); original ASOF preserved
(`asof_us == signal_time_us` for every event); ledger identities unique;
append-only journals; the limit consumes NO retry budget of unprocessed
entries (their `attempt_count` unchanged — separate test
`test_retry_limit_reached_does_not_consume_budget_of_unprocessed`).

**Test 2 — concurrent retry + source redelivery:**
`test_concurrent_retry_and_source_redelivery_classify_exactly_once` —
PASS.  E1 due in the queue AND redelivered by the source in the same
cycle: exactly ONE classification (classifier calls == 1), redelivery
accounted `DUPLICATE`, no ledger duplicates (one identity, 2 append-only
journal records), retry-state + checkpoint atomic and correct
(`pending_remaining == 0`, checkpoint == `cursor_after`), and a re-run of
the observer performs no further terminal classification (journal
unchanged).  Variants: redelivery while the retry RE-DEFERS → duplicate
without a second pass (`…_redelivery_while_redeferred_…`); crash in the
middle of backlog processing + restart from durable state → zero loss,
zero duplication, exactly one final classification per event
(`test_backlog_crash_restart_loses_nothing_duplicates_nothing`; failed
commit reports the documented pre-cycle `pending_remaining`).

## 7. Test totals

* new B4 targeted: **7 tests, all PASS**
* full H1-E suite `tests/test_me_entry_quality_h1e_*.py`: **134 passed**
* H1-A/B/C/D + #305 regression: **717 passed, 26 skipped**
* B1–B3 suites (durable retry / commit atomicity / activation freeze,
  atomic commit, dual-freeze): PASS within the totals above
* full `tests/` vs baseline: see the verification report §2 — the PR-
  specific delta remains exactly the two `PHASE2_ONLY` PR-diff
  confinement tests; no new unexplained failures (details and raw logs in
  `reports/324_h1e/`)

## 8. Residual risks and limitations

* `retry_batch_limit = 50` is an offline bounded-work default only; any
  production value requires a separate owner decision together with the
  HTF/Bybit rate limits (Phase B/C GO), as does the interplay with
  `retry_backoff_us` / `max_attempts`;
* the limiter is per-observer-cycle and in-process — a global
  cross-instance rate limit is out of Phase A scope;
* `pending_remaining` is exact for the atomic in-memory reference store;
  the SQL implementation must preserve the same post-commit semantics
  (queue rows updated in the same transaction);
* everything remains RESEARCH_ONLY: no deploy, no DDL application, no
  production DB writes, no live Bybit fetch, no backfill, no OOS
  activation, `H1_OOS_HOLD`; merge of PR #325 authorizes nothing.
