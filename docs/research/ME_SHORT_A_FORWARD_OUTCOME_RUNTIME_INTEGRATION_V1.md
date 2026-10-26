# ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1

Issue #208 follow-up — connect the frozen collector to a real, independent,
observe-only runtime.

Status: **IMPLEMENTED, NOT DEPLOYED, NOT ACTIVATED.**
Deployment and activation require a separate, explicitly approved checkpoint —
see `docs/research/ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_DEPLOYMENT_RUNBOOK_V1.md`.

| Area | Touched |
|---|---|
| SCANNER | NO (read-only consumer of what the scanner already persisted) |
| PAPER | NO |
| LIVE | NO |
| DATABASE | YES (migration 079 + separate least-privilege runtime roles) |
| GRAFANA | NO |
| DEPLOYMENT | NO in this change (unit file is source-controlled only) |

---

## 1. What this protocol is

The Issue #208 collector
(`ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1`) ships four
append-only writers but nothing drives them. This protocol is that driver: a
real, long-running, **observe-only** process that, for every eligible
`MOMENTUM_EXHAUSTION` `SHORT` candidate, calls exactly

```
capture_source_signal  ->  record_event_lineage
        ->  capture_post_signal_candle  ->  finalize_outcome
```

and then appends one operational `heartbeat` row.

It never places, simulates or records an order. There is no paper surface, no
live surface, and no scanner-gate surface: the runtime reads
`dds.scanner_setup` and closed 5m klines and writes only the migration 078/079
append-only tables under its own least-privilege role
`trad_bot_me_short_a_runtime`.

---

## 2. Frozen protocol (never retuned)

| Item | Value |
|---|---|
| protocol_version | `ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1` |
| collector protocol | `ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1` |
| scanner | `MOMENTUM_EXHAUSTION` |
| direction | `SHORT` only |
| entry | `reference_price` — frozen A: max of the last five 5m highs |
| stop | `invalidation_price` (absolute, `> reference_price`) |
| target | `target_1` (absolute, `< reference_price`) |
| max hold | **240m** |
| intrabar policy | **STOP_FIRST** |
| candles | **CLOSED 5m** candles only (`candle_close_time <= observed_at`) |
| mode | `OBSERVE_ONLY_NO_ORDERS` |

The runtime identity is `sha256` of the canonical payload
(`runtime_protocol_payload()`) and is stored on every heartbeat row, so an
operator can prove which runtime protocol produced a set of counters. The
registry `app/research/me_short_a_forward_outcome_runtime_registry_v1.json`
carries the same hash and a test asserts they agree.

---

## 3. Shared `event_key` with Phase 1

```
event_key = sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)
```

**This is byte-identical to Phase 1**, because the runtime builds the identity
from the very same fields and the very same value normalization:

| Identity field | Phase 1 (`capture()`) | This runtime |
|---|---|---|
| `signal_time` | `candidate.detected_at` | `dds.scanner_setup.detected_at` |
| `reference_price` | `candidate.reference_price` (float) | `dds.scanner_setup.reference_price` |
| `invalidation_price` | `candidate.invalidation_price` (float) | `dds.scanner_setup.invalidation_price` |
| `target_1` | `candidate.target_1` (float) | `dds.scanner_setup.target_1` |

Phase 1 hashes `Decimal(str(<float produced by the scanner>))`. A numeric
column read back from PostgreSQL can carry trailing zeros
(`Decimal('100.00000000')`) which would hash a *different* string, so the
runtime normalizes every identity price through
`canonical_price()` = `Decimal(str(float(value)))`. This is the only way to be
byte-identical with a key that was minted from a Python `float`, and a test
asserts the equality directly against `event_key()`.

Consequence: `source signal -> Phase 1 snapshot -> outcome` joins on one key,
and `research.v_me_short_a_lineage_status_v1` can report `MISSING_LINEAGE`
honestly whenever the Phase 1 snapshot for that key does not exist.

---

## 4. Data flow

```
dds.scanner_setup (READ-ONLY)          Bybit /v5/market/kline (READ-ONLY)
   MOMENTUM_EXHAUSTION + SHORT              CLOSED 5m candles only
            │                                       │
            ▼                                       ▼
   DbScannerSetupCandidateSource           BybitClosedCandleSource
            │                                       │
            └───────────────┬───────────────────────┘
                            ▼
                 ForwardOutcomeRuntime.run_cycle()
                            │
        ┌───────────────────┼───────────────────────┐
        ▼                   ▼                       ▼
 capture_source_signal  record_event_lineage   capture_post_signal_candle
        │                   │                       │
        └───────────────────┴───────────┬───────────┘
                                        ▼
                               finalize_outcome
                                        │
                                        ▼
                                   heartbeat
```

### 4.1 Candidate intake (no scanner change)

`DbScannerSetupCandidateSource` issues **read-only SELECTs** against
`dds.scanner_setup` joined to `dds.instrument`. It never calls
`app.scanners`, so scanner logic, filters, scoring and the ME SHORT direction
gate are untouched. Candidate rows that cannot be represented honestly (broken
identity, missing prices) are counted in `skipped_invalid_candidate` and
surfaced by `setup_id`, never repaired and never silently dropped.

#### 4.1.1 Strict keyset pagination from the immutable freeze

Intake is a **keyset** scan on `(detected_at, setup_id)`:

```
WHERE scanner_name=%s AND direction=%s
  AND detected_at > <freeze_ts>
  AND (detected_at > <cursor_detected_at>
       OR (detected_at = <cursor_detected_at> AND setup_id > <cursor_setup_id>))
ORDER BY detected_at ASC, setup_id ASC
LIMIT <page_size>
```

| Guarantee | How |
|---|---|
| starts strictly at the **new immutable `freeze_ts`** | `detected_at > freeze_ts` is bound **inside** every query; `fetch_page` refuses to run without a `freeze_ts` |
| a pre-freeze backlog never blocks the selection | the freeze predicate is in SQL, so historical rows are never read back, never occupy a page and can never push a post-freeze row out of one |
| identical timestamps are never lost | the tie-breaker `setup_id` makes the order total; the composite cursor is expanded to `detected_at > ? OR (detected_at = ? AND setup_id > ?)` — a bare `detected_at > ?` would drop the siblings sharing the boundary row |
| batches larger than one page are fully observed | pages are drained to exhaustion inside one cycle; `page_size` (default 500) is a batching parameter, never a cap |
| nothing is lost at a page boundary | the cursor points at the **last raw row of the page**, including rejected rows, and advances only after the whole page has been handled |
| a repeated run is idempotent | the cursor resets to the freeze and every writer is `ON CONFLICT DO NOTHING`, so a re-run re-reads but never re-writes and never duplicates |
| resume after a failure loses nothing | a surfaced database error **stalls** the scan at that exact page: the cursor does not move, the same rows are retried next cycle, `errors`/`last_error` show it |

### 4.2 `source_signal_present` reflects the table, not this INSERT

`record_event_lineage` receives `source_signal_present` from a **READ-ONLY
confirmation** against `research.me_short_a_source_signal_v1`, not from the
return value of `capture_source_signal`. `INSERT ... ON CONFLICT DO NOTHING`
returns "no fresh row" both when the row already exists and when the insert
failed, and the capture tables are append-only — so a wrong value is written
once and can never be repaired.

| Situation | Old behaviour | Now |
|---|---|---|
| re-run / `ON CONFLICT DO NOTHING` | `source_signal_present=false`, `link_status=PENDING`, bogus `MISSING_LINEAGE` | row confirmed present → `source_signal_present=true`, `link_status=LINKED` |
| partial recovery (source signal written, process died before the lineage row) | same false `source_signal_missing` | confirmed present → correct linkage |
| the insert genuinely did not land | a permanent `PENDING`/`MISSING_LINEAGE` | **no lineage row is written at all**; the candidate is retried (`HANDLER_RETRY`), the keyset cursor does not move |
| the confirmation query fails (e.g. least-privilege role without `SELECT`) | flattened into "absent" | `DatabaseAccessError` is surfaced in `errors`/`last_error` and the candidate is retried |

The same rule governs `_phase1_lookup`: a database error raises
`DatabaseAccessError` and the candidate is retried instead of being recorded
as `phase1_linked = FALSE`, which would be a permanent `MISSING_LINEAGE`.
`source_observation_ids` is parsed from whatever the driver returned
(`jsonb` comes back as a list from psycopg and as text from pg8000, the
documented fallback in the runner).

### 4.3 `recent_high_window` — no lookahead, no imputation

The five CLOSED 5m candles behind `reference_price` are only attached when

* every one of them closed at or before `signal_time`, **and**
* `max(highs) == reference_price` exactly.

Otherwise nothing is attached and the gap is named explicitly
(`missing_fields` / `source_quality_flags`, e.g.
`recent_high_window_reference_mismatch`). The frozen A geometry is never
"fixed" silently. This matters because the scanner evaluates
`max(candles_5m[-5:])` at detection time; if the trigger candle was still
forming, its high cannot legally enter a no-lookahead window.

### 4.4 CLOSED 5m candles

`BybitClosedCandleSource` drops any candle with `close_time > observed_at`
before it reaches the collector, so a forming candle can never be observed.
Every captured provenance row records `is_closed`, `candle_close_time <=
retrieved_at`, and the candle-basis touch flags
(`high_touches_stop`, `low_touches_target`, `high_touches_reference`) — a
**candle** touch, never a fill.

### 4.5 Controlled finalization

`finalize_outcome` is called only once the 240m horizon is settled on closed
candles (`max(candle_close_time) >= horizon_end`). Before that the event stays
open and `skipped_unmatured` counts it. After maturation:

* complete + ASOF-clean → `MATCHED_FINALIZED` with the frozen **STOP_FIRST**
  terminal path (`STOP_HIT` / `TARGET_HIT` / `TIMEOUT_240M`, ambiguity = stop
  at exactly `-1R`);
* anything missing → `SOURCE_INCOMPLETE` with **no economics at all**.

`exit_fill_semantics` stays `MODELED_UNKNOWN` and `partial_fill_status` stays
`UNKNOWN_NOT_INDEPENDENTLY_CAPTURED` — both `CHECK`-pinned in the database.

### 4.6 Restart safety without mutable state

Pending events are derived from the append-only tables
(`me_short_a_event_lineage_v1` LEFT JOIN
`me_short_a_outcome_observation_v1` where the outcome is absent). A restart
therefore resumes exactly where the previous process stopped, with no
checkpoint file and no mutable state row.

---

## 5. Lifecycle

### 5.1 Activation lifecycle (read-only for the runtime)

| State | Meaning |
|---|---|
| `READY_TO_START` | migration 078 seeded state; nothing is captured |
| `RUNNING` | set by the separate activation checkpoint with an immutable `freeze_ts` |
| `COMPLETED` / `CANCELLED` | terminal |

The runtime **reads** this row and never writes it. Capture additionally
requires `ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1`; everything else is
fail-closed and the process exits `3` without writing evidence.

### 5.2 Runner lifecycle

| State | Meaning |
|---|---|
| `IDLE` | constructed, not started |
| `RUNNING` | cycling |
| `STOPPING` | SIGTERM/SIGINT received, finishing the current cycle |
| `STOPPED` | clean exit |
| `FAILED` | the cycle itself raised; counted and reported |

Every state transition is logged and recorded on the next heartbeat row.

---

## 6. Idempotency

* Every collector write is `INSERT ... ON CONFLICT (event_key) DO NOTHING`
  (or `(event_key, candle_open_time)`), so a retry can only ever add a row that
  did not exist.
* The runtime additionally keeps a bounded in-memory seen-set
  (`SEEN_IDS_LIMIT = 4096`) purely to avoid pointless work; it is advisory and
  never the source of truth.
* Concurrency is safe by idempotency, not by a lock: two runners racing on the
  same events produce the same evidence exactly once. The `runner_id` and
  `generation` recorded on each heartbeat make such a race visible.

---

## 7. Heartbeat / diagnostics

`research.me_short_a_forward_outcome_runtime_heartbeat_v1` is an **append-only**
log (`UPDATE`/`DELETE` are blocked by a trigger *and* by revoked privileges).
One row per cycle. It carries the runner lifecycle state, the observed
activation snapshot, `config_fingerprint`, `runtime_protocol_hash`, the open
event count and the monotonic counters:

`cycles_completed`, `candidates_seen`, `signals_captured`,
`lineages_recorded`, `candles_captured`, `outcomes_finalized`,
`outcomes_matched`, `outcomes_incomplete`, `skipped_not_activated`,
`skipped_pre_freeze`, `skipped_idempotent`, `skipped_invalid_candidate`,
`skipped_lookahead`, `skipped_unmatured`, `errors`, `last_error`,
`last_event_time`.

**No prices, no outcomes, no `event_key`, no `symbol`** ever enter the
heartbeat table — it is operational metadata, not research evidence.

`research.v_me_short_a_forward_outcome_runtime_status_v1` derives the current
state from the latest row per `runner_id` and adds
`heartbeat_age_seconds` / `is_stale` (stale after 300 s), so a crashed runner
is visible without any mutable state table.

---

## 8. Strictly forward-only after the new freeze

1. Migration 079 + the grants file are reviewed and applied (out of scope
   here).
2. A separate, explicitly approved activation checkpoint transitions the
   migration 078 row `READY_TO_START -> RUNNING` with a new immutable
   `freeze_ts`, and creates the operator flag file.
3. From that instant the runtime captures only events with
   `signal_time > freeze_ts`. Everything older is counted in
   `skipped_pre_freeze` and never written.

`CHECK (signal_time > freeze_ts)` on every capture table means a historical
backfill is impossible even if this code were bypassed. There is **no
backfill** path in the runtime and none is planned.

---

## 9. Separate least-privilege DB roles

| Role | Privileges |
|---|---|
| `trad_bot_me_short_a_runtime` | `INSERT` on the four capture tables and the heartbeat log; `SELECT` on the activation row, on the lineage/outcome tables (pending-event derivation) and on Phase 1 (**read-only** linkage lookup) |
| `trad_bot_me_short_a_runtime_audit` | `SELECT` on the heartbeat log and the runtime status view |

* `UPDATE` / `DELETE` / `TRUNCATE` are granted to **nobody** and revoked
  explicitly.
* The closed frozen OOS cohort (`research.prospective_observation`,
  `research.prospective_outcome`) receives **no privilege at all**.
* The runtime authenticates with its own credentials file
  (`ME_SHORT_A_RUNTIME_DB_*`); the shared application `.env` is never loaded,
  so scanner/paper/live credentials cannot be reused.

---

## 10. Isolation guarantees (what is NOT touched)

* `research.me_short_a_fill_feasibility_v1` (Phase 1) — read-only
  `SELECT` for the shared-`event_key` lookup; never inserted, never updated,
  never retro-fitted (`source_observation_ids` stays as it is).
* Closed frozen OOS cohort — no privilege, no writes, no reopen.
* Scanner logic, scanner gates, the ME SHORT direction gate — untouched; the
  runtime never imports `app.scanners`.
* Paper and live — no surface at all.
* No historical backfill; no deploy; no SQL writes on the VPS; no service
  restart as part of this task.

---

## 11. Tests

```
python -m pytest -q tests/test_me_short_a_forward_outcome_runtime_v1.py
python -m pytest -q tests/test_me_short_a_forward_outcome_runtime_pagination_and_lineage_v1.py
python -m pytest -q tests/test_me_short_a_forward_outcome_runtime_migration_079_v1.py
```

Covered: runtime identity ↔ registry hash; shared `event_key` byte-parity with
Phase 1 including the numeric round-trip normalization; fail-closed
configuration; activation state matrix; runner lifecycle and diagnostics;
idempotency (repeat candidate, replayed cycle, bounded seen-set, `ON CONFLICT`
contract); CLOSED-5m-only candle handling and forming-candle exclusion;
lookahead rejection; the full 240m `STOP_FIRST` path
(`STOP_HIT` = `-1R` with ambiguity, `TARGET_HIT`, `TIMEOUT_240M`,
`SOURCE_INCOMPLETE` with zero economics, single-shot finalization, restart
resume from the append-only tables); Phase 1 linkage asserted only when a
snapshot really exists; read-only scanner source; explicit missing-field
reporting; append-only heartbeat with no evidence columns; isolation (no
Phase 1 / frozen-OOS / `dds.` writes, no order or paper/live surface, no
scanner imports, LONG excluded); and the SQL/deployment contract (migration
079, grants, systemd unit, runner exit codes, docs and runbook, CI wiring).

Plus the two runtime-blocker regressions
(`test_me_short_a_forward_outcome_runtime_pagination_and_lineage_v1.py`):

* **keyset pagination** — `>1700` historical rows plus `>500` new rows; pages
  of 500; identical `detected_at` values including at a page boundary; page
  partition with no loss and no duplication; a repeated run and a fresh
  runner both idempotent; resume after a mid-page failure and a permanently
  stalled page keeping the cursor in place; rejected rows counted without
  stalling intake; the SQL asserted to use the composite keyset and never
  `OFFSET`;
* **`source_signal_present`** — partial recovery and `ON CONFLICT DO NOTHING`
  both report the row as present and link; an unconfirmed source signal is
  retried instead of recorded as `source_signal_missing`; permission errors on
  the confirmation and on `_phase1_lookup` are surfaced and retried, never
  flattened into `MISSING_LINEAGE`; `source_observation_ids` parsed from list,
  tuple, scalar, bytes and JSON-text shapes.

---

## 12. Deployment readiness — **separate from the fact of implementation**

**Implementation: COMPLETE** (runtime module, real sources, runner entrypoint,
systemd unit, migration 079, separate least-privilege roles, tests, docs and a
deployment/rollback runbook — all source-controlled in this PR).

**Deployment readiness: NOT READY / NOT STARTED — intentionally.**

| Step | State |
|---|---|
| Migration 079 reviewed and applied on VPS | **not done** (forbidden in this task) |
| Runtime grants applied on VPS | **not done** (forbidden in this task) |
| Dedicated runtime credentials created | **not done** (forbidden in this task) |
| Activation checkpoint `READY_TO_START -> RUNNING` + new `freeze_ts` | **not done** (separate explicit approval) |
| Operator flag file `me_short_a_forward_runtime_enabled` | **not done** (operator action) |
| Service started / restarted | **not done** (forbidden in this task) |
| Paper / live activation | **forbidden** |
| Scanner / direction gate change | **forbidden** |
| Post-deploy health check | **not applicable yet** |

Until every step above is executed as a separate, explicitly approved change,
the runtime is fail-closed and records nothing.
