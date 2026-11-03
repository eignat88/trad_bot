# Issue #253 — Staged rollout, rollback and post-deploy validation runbook

**Status of this document:** PREPARED, **NOT EXECUTED**.
Nothing in this file has been run against `trad-bot-01`. Every step below
requires a separate **operator GO**; this Issue-#253 PR ships code, tests and
documentation only (CODE + TESTS + PR ONLY — NO PRODUCTION DEPLOY).

Companion documents:

* `docs/issue253_bybit_consumer_inventory_v1.md` — consumer inventory (Stage A)
* `docs/issue253_oos_limiter_safety_analysis_v1.md` — OOS safety (Stage E)
* `docs/issue253_bybit_api_quota_model_v1.md` — quota model and assumptions (Stage F)

---

## 0. What the pre-deploy baseline already tells us

Fixed 30-minute window, 2026-11-03 (`trad-bot-01`):

| Consumer | `BYBIT_API_CALL` records | `ret_code=10006` |
|---|---:|---:|
| Scanner | 1221 | 3 |
| Research evaluator | 1100 | 2 |
| HTF Phase-2 V1 | 192 | 0 |
| SRR evaluator | 20 | 0 |
| Paper | 6 | 0 |
| **TOTAL** | **2539** | **5** |

Derived facts that drive the plan below:

* average load = 2539 / 1800 s ≈ **1.41 records/s** — far below `G = 8 rps`;
* **peak = 16 records in one calendar second**, i.e. above both `G = 8`
  and `B = 12`. Enabling the limiter therefore **will** defer/deny work at
  peaks. This is intended behaviour, but it means *throughput at peak
  seconds will be lower than the current unregulated baseline* — the
  rollout must be judged on `BYBIT_API_SKIP`, queue wait and consumer
  backlog, not on "no errors";
* telemetry records are **not** proven unique wire calls, and the timestamp
  of a result record is **not** the send time. All comparisons below are
  therefore *like-for-like against this same telemetry*, never against a
  reconstructed request count.

---

## 1. Staged activation order

Ordered by (a) blast radius if something is wrong, (b) load, (c) how well
the consumer's failure handling is proven. Activation itself is a
**per-consumer** config edit (`global_bybit_rate_limiter_enabled: true`
plus `global_limiter_contract_path`) — that is exactly why the Issue #253
contract exists: parameters are pinned once, activation stays staged.

### Stage 0 — pre-flight only (no activation, no restart)

1. Confirm the deployed code revision and the config files in force
   (`config.yaml`, `scanner_canary.json`, any `--config` used by a timer).
2. Record the **effective** limiter config of every consumer (Issue #231
   logs `global_limiter=%s allow_env=%s state_path=%s` for the Scanner):
   all enabled consumers must print the SAME `state_path` and the same
   `G/B/max_wait` (they now come from one `limiter_contract*.json`).
3. Confirm shared-state directory ownership (multi-user, group-writable):
   `install -d -g tradbot -m 2770 /var/lib/trad_bot/state` — pre-create,
   never chmod an existing directory.
4. Snapshot the current state of all services (unit status, last cycle
   timestamps) and the current OOS checkpoint/coverage rows for the
   consumers that will be activated in the next stage.
5. Fix the pre-deploy telemetry baseline (the table above + per-endpoint
   split + calendar-second peaks) into a file for the final comparison.

**Stop here and wait for operator GO before Stage 1.**

### Stage 1 — Paper (6 records / 30 min)

Smallest consumer, no OOS finalization at stake, exercises state-file
creation, permissions, `acquire → prepare_send → record_result` and
telemetry end-to-end with essentially zero throttling pressure.

### Stage 2 — SRR evaluator (20 records / 30 min)

First *research* consumer at negligible load. Its fail-closed handling
(`SOURCE_FETCH_ERROR → SOURCE_UNVERIFIABLE`, `finalization_eligible=False`)
is already proven, so a limiter denial cannot finalize an outcome.

### Stage 3 — HTF Phase-2 V1 observer (192 records / 30 min ≈ 0.11 rps)

The observer's contract is "advance nothing, emit nothing" on
`SOURCE_BLOCKED`, and the Bybit candle source raises on deny/empty/gap —
the safest research consumer to move to a shared budget.

### Stage 4 — Research evaluator (1100 records / 30 min ≈ 0.61 rps)

First high-volume consumer. From here on the shared bucket is genuinely
contended; watch queue wait and backlog closely.

### Stage 5 — Scanner (1221 records / 30 min ≈ 0.68 rps, incl. in-scanner OOS evaluator)

Highest load and the largest symbol fan-out. Activate last so every earlier
stage has already validated the shared state under real traffic.

### Stage 6 — remaining timers / CLIs that Issue #253 aligned

`me_r_v3` isolated OOS collector, analytics CLI, outcome CLIs, shadow/v2d
runners, market-data CLI — activate only after Stages 1–5 are clean, each
individually, in ascending observed load.

### Explicitly NOT part of this rollout

* **HTF V2 — NO_GO.** Successful completion of Issue #253 is not an
  activation signal for HTF V2; its own gate (Issue #225,
  `API_BUDGET_NOT_CLEARED`) still applies.
* PR #227 (DRAFT / DO NOT MERGE) — not merged, not deployed by this work.
* Any change to `global_limiter_g_rps` / `burst` / `max_wait_s` — the
  shipped `limiter_contract.v1.json` values are used as-is until a separate
  measurement-backed change is approved.

### Per-stage entry checklist (all must be green before activating)

- [ ] effective config of the stage's consumers read back and matching the contract;
- [ ] service states recorded (unit status, last successful cycle);
- [ ] shared `state_path` identical for every already-enabled consumer;
- [ ] `BYBIT_API_SKIP`, limiter queue wait and breaker transitions at zero/baseline;
- [ ] source freshness / last candle timestamp unchanged vs. the previous cycle;
- [ ] OOS checkpoint and coverage rows for this consumer unchanged since the snapshot.

### Per-stage exit checklist (60 minutes of Stage-N telemetry)

- [ ] no new `ret_code=10006` / HTTP `429` attributable to this stage;
- [ ] `BYBIT_API_SKIP` count and `reason=` breakdown recorded;
- [ ] limiter queue-wait p95 recorded (must stay well under `max_wait_s`);
- [ ] no breaker transition, or every transition explained by a real provider signal;
- [ ] consumer backlog / cycle duration within the pre-deploy envelope;
- [ ] source freshness and OOS checkpoint/coverage identical to pre-stage;
- [ ] no research row finalized with a shorter source window than before.

**Rollback criteria (any one ⇒ roll this stage back immediately):** a breaker
that will not close, `BYBIT_API_SKIP` growing cycle over cycle, any consumer
backlog that keeps growing, any OOS checkpoint advancing without its source,
or any new `10006`/`429` that correlates with the activation.

---

## 2. Rollback plan

Rollback is always **one consumer at a time**, newest stage first.

1. **Code version** — the deployed revision is recorded per stage (Step 0.1).
   Code rollback is `git revert`/redeploy of the previous artifact; it is
   only ever needed if a defect is found, and it must be reviewed like any
   other production change.
2. **Configuration version** — keep a copy of every config file *before* the
   stage (`config.yaml.bak-issue253-stageN`, `scanner_canary.json.bak-...`).
   Rollback = restore that copy, i.e. set
   `global_bybit_rate_limiter_enabled: false` (and/or remove
   `global_limiter_contract_path`) for the consumer being rolled back.
3. **Service restart** — performed by the operator, one consumer at a time,
   using the existing service unit. *This automation never restarts,
   reloads or edits a service.*
4. **Stop criteria** — see the per-stage exit criteria above.
5. **Post-rollback verification** — the consumer runs again with the limiter
   OFF, its cycle times and telemetry return to the pre-stage envelope, and
   OOS checkpoint/coverage rows are unchanged.

### Hard prohibitions during any rollout or rollback

* **Never manually edit, truncate or delete an active limiter state file**
  (`/var/lib/trad_bot/state/global_bybit_limiter_v1.json` and its `.lock`).
  The checksum-validated state fail-closes on corruption; the correct fix
  is a configuration/code rollback, not state surgery. If the state is
  genuinely unrecoverable, stop the affected consumers first and treat it
  as an incident — do not "repair" it in place.
* **Never change a research freeze**: freeze timestamps, eligibility rules,
  registries and existing OOS rows are out of scope for this rollout.
* **Never** change production PostgreSQL, backfill/replay, flip
  paper/live gates, scoring or signal generation as part of this work.
* **Never** enable two consumers with different `global_limiter_*`
  parameters on one state path — the `config_fingerprint` guard will
  fail-close the second one; fix the configuration instead of bypassing it.

---

## 3. Post-deploy validation runbook (≥ 60 minutes, after operator GO)

Collect for **at least 60 minutes per activated stage** (longer for Stages
4–5), from the same telemetry source as the pre-deploy baseline.

### 3.1 Metrics to collect

| # | Metric | Source | Compare against |
|---|---|---|---|
| 1 | Aligned telemetry per consumer (`service=` tag) | `BYBIT_API_CALL` lines | pre-deploy table (§0) |
| 2 | Calendar-second peak (count of records per wall second) | telemetry | pre-deploy peak **16/s** |
| 3 | Rolling 5-second peak | telemetry | baseline re-computed the same way |
| 4 | Calendar-minute peak | telemetry | baseline |
| 5 | Rolling 5-minute load | telemetry | baseline |
| 6 | Provider `ret_code=10006` and HTTP `429` | telemetry / logs | baseline **5 / 30 min** |
| 7 | `BYBIT_API_SKIP` with `limiter=`/`reason=` breakdown | telemetry | baseline 0 (limiter was off for these consumers) |
| 8 | Retry recovery (attempt_index ≥ 2 that still succeeded) | telemetry | baseline |
| 9 | Limiter queue wait (`queue_wait_ms=` on SKIP + p50/p95 of granted waits) | telemetry | — |
| 10 | Breaker transitions (`breaker OPEN level=…`, recovery) | `global_rate_limiter` logs | baseline 0 |
| 11 | Consumer backlog / cycle duration | consumer logs | pre-stage snapshot |
| 12 | Source freshness (last candle close time per symbol) | consumer logs / DB read-only | pre-stage snapshot |
| 13 | OOS coverage and checkpoint integrity (row counts, `is_final` counts, checkpoint values) | read-only queries | pre-stage snapshot |

### 3.2 Comparison rules

* Compare **like telemetry with like telemetry**: same window length, same
  consumer set, same aggregation. Never compare a record count against a
  reconstructed wire-call count.
* A change in `BYBIT_API_SKIP` with **unchanged** backlog, freshness and
  checkpoint/coverage is an acceptable steady-state outcome — the limiter is
  doing its job.
* A change in `BYBIT_API_SKIP` **plus** growing backlog, stale source, or any
  checkpoint advancing without its source is a **rollback trigger**.
* Any new `10006`/`429` that correlates with the activation is a rollback
  trigger regardless of the other metrics.

### 3.3 Secrets hygiene

Telemetry and limiter logs carry only allowlisted scalars (endpoint,
caller/service tag, status, ret_code, `X-Bapi-Limit*` values, durations,
queue wait). No API key, signature, authorization header or request payload
may be added to any log line while collecting this data.

---

## 4. HTF V2 — ~120 req/hour assessment (procedure only, NOT run)

**HTF V2 remains NO_GO.** The following is the procedure to be executed
only under a separate operator decision, after Issue #225 is cleared.

120 req/hour = **0.033 rps average** — mathematically negligible against
`G = 8`. An average is **not sufficient evidence** (Issue #253 §11). The
assessment must additionally quantify:

1. **Burst shape** — worst-case requests in any 1-second and 5-second
   window during V2 warmup (page-by-page history fetch can issue a burst of
   consecutive calls).
2. **Warmup / catch-up** — the full initial history pull, its duration and
   its requests-per-second profile, replayed against the *existing*
   consumers' concurrent load.
3. **Retries** — V2's retry budget on top of the first-attempt count
   (`bybit_max_attempts`, rate-limit retry caps, timeout retries), i.e. the
   worst-case multiplier, not the nominal one.
4. **Reserve for existing consumers** — the headroom that must remain for
   Scanner + Research + HTF V1 + SRR + Paper at their **observed peak**
   (16 records/s), not their average.
5. **Provider-side limits** — the per-endpoint order-cap and the
   undocumented market-endpoint caps identified in
   `docs/issue253_bybit_api_quota_model_v1.md`; V2 must be checked against
   the endpoints it actually uses.
6. **Measured soak** — a staged V2 activation with the §3 metrics collected
   for ≥ 60 minutes, zero induced `10006`/`429`, unchanged OOS
   checkpoint/coverage for the existing consumers.

Only the combination of (1)–(6) may be presented as an HTF V2 budget
assessment. A mathematical average headroom alone is explicitly rejected.
