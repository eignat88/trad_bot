# #324 H1-E — Prospective H1 4H/1D Classification Observer V1
## Phase A Implementation (OFFLINE ONLY)

**Status:** `PHASE_A_OFFLINE_IMPLEMENTATION / NO_DEPLOY / NOT_ACTIVATED / H1_OOS_HOLD`
**Spec:** `docs/research/324_h1e_prospective_observer_phase_a_spec_v1.md`
**Branch:** `research/324-h1e-prospective-observer-v1` (Draft PR #325)
**Parent:** #297 (H1 Trend Alignment) / #295 (ME Entry Quality)
**Source:** #305 `research_me_upstream_coverage.source_event` (subject to #296 lineage/readiness gate)
**HTF classification:** frozen H1-A/B (#307), H1-C (#309), H1-D (#315), merge SHA `03d6fac466e2f9ddd890803649db54b13a451ba3`

---

## 0. Scope and hard limits

Phase A is an **isolated, offline implementation only**.  Concretely:

| Limit | Enforcement |
| --- | --- |
| `RESEARCH_ONLY` | new code lives only in `app/research/me_entry_quality_h1e_v1/` + research docs/tests/proposals |
| `NO_DEPLOY` / `NO_EXISTING_SERVICE_RESTART` | no systemd units, no service edits, no runtime flags touched |
| `NO_PRODUCTION_DB_WRITES` / `NO_DDL_APPLY` | the ledger DDL is a **proposal file** (`sql/proposals/…`); no code executes DDL or connects to a production DB |
| `NO_LIVE_BYBIT_FETCH` | the HTF transport is injectable; Phase A uses offline mock transports only |
| `NO_BACKFILL` | strict `signal_time > H1-E freeze_ts`; pre-freeze rows become explicit rejections |
| `NO_OOS_ACTIVATION` | `H1E_OOS_READINESS_V1` emits a pre-registration **template** only — no outcomes, no evaluation |

Nothing in this phase changes trading filters, the scanner, paper/live
execution, existing OOS contours, or the #305 freeze
(`2026-11-24T13:31:53.365952+00:00`).

## 1. Package map

`app/research/me_entry_quality_h1e_v1/` — all modules pure or injectable
(zero network, zero production DB, zero clock reads):

| Module | Contract | Contents |
| --- | --- | --- |
| `protocol.py` | frozen identity | `H1E_PROSPECTIVE_OBSERVER_V1` descriptor + `protocol_fingerprint()`; preserved #305 facts (freeze literal, UNVERIFIED window `2026-11-24T13:31:53.365952+00:00 → 2026-11-25T07:14:41.230516+00:00`, SOXSUSDT 2026-11-25 QA exclusion); `H1E_FREEZE_NOT_MINTED` |
| `contracts.py` | immutable events | `H1ESourceEvent` (preserved fields VERBATIM), `H1EFreeze` (owner-approved or synthetic-fixture), fail-closed `admit_source_row()`, mirrored #305 hashing, ledger identity `(source_event_key, protocol_fingerprint)` |
| `source_readiness.py` | `H1E_SOURCE_READINESS_V1` | readiness report: `PASS/HOLD/BLOCK`, unsupported → `UNKNOWN`→`HOLD` (never PASS) |
| `capture.py` | `H1E_PROSPECTIVE_EVENT_CAPTURE_V1` | bounded deterministic worker; monotonic composite cursor `(captured_at_us, source_event_key)`; at-least-once delivery / exactly-once effect; atomic ledger+checkpoint commit; collision fail-closed; fixture-freeze refusal |
| `classification.py` | `H1E_HTF_ASOF_CLASSIFICATION_V1` | ASOF 4H/1D over 60 closed candles via frozen H1-C `run_h1_event()` + H1-D `240`/`D` adapter + `H1D_HTF_SNAPSHOT_2` provenance; typed HTF failures → explicit non-eligible codes; bounded deferral |
| `ledger.py` | `H1E_PROSPECTIVE_LEDGER_V1` | append-only ledger reference semantics (executable model of the DDL proposal): unique identity, conflict isolation, atomic commit, derived status, monotonic checkpoint |
| `coverage_health.py` | `H1E_COVERAGE_HEALTH_V1` | five-dimension reconciliation (`PASS/HOLD/GAP/CONFLICT`), grace lag vs persistent gap |
| `oos_readiness.py` | `H1E_OOS_READINESS_V1` | pre-registration template only (cohorts, nine 4H×1D combinations, unchanged control); outcome computation structurally absent |

Supporting artifacts:

* `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` — **DO NOT APPLY** additive DDL proposal for schema `research_me_entry_quality_h1e`;
* `docs/research/324_h1e_prospective_ledger_ddl_v1.md` — DDL rationale (retention, recovery);
* `docs/research/324_h1e_oos_readiness_protocol_v1.md` — OOS readiness pre-registration proposal;
* `docs/research/324_h1e_phase_a_verification_report_v1.md` — verification report.

## 2. Identity (preserved, never rebuilt)

Per the spec, H1-E carries the #305 identity **verbatim**:

```
source_event_key  — deterministic #305 key (SHA256("ME_UPSTREAM_SOURCE_V1|" + normalized_identity))
source_identity   — "<scanner>|<symbol>|<direction>|<entry_timeframe>|<signal_candle_open_ms>"
signal_time       — exact microsecond instant (signal_time_us); classification ASOF
lineage_hash      — canonical SHA-256 of the stored lineage JSON
source_fingerprint, activation_freeze_ts (#305), captured_at (timing columns only)
```

* H1-E **never** rebuilds identity from mutable presentation fields.  The
  only recomputations are integrity re-derivations that must MATCH the
  stored values (`source_event_key` ↔ `source_identity`, `lineage_hash` ↔
  canonical lineage JSON) — a mismatch is an explicit
  `H1E_REJECT_*` rejection / `H1E_EVENT_KEY_LINEAGE_CONFLICT` conflict.
* Ledger identity is the pair `(source_event_key, h1e protocol
  fingerprint)` — a separate namespace
  (`ME_ENTRY_QUALITY_H1E_V1`), disjoint from #305, V3/OOS-2 and every
  existing OOS ledger.
* Timestamp precision policy (frozen H1-C): µs preserved losslessly;
  ms views are floored toward the past.  Event time, source `captured_at`,
  observed/recorded time and classification/fetch timestamps stay distinct
  columns.

## 3. Temporal authority and freeze

* **#305 freeze** `2026-11-24T13:31:53.365952+00:00` is preserved as an
  immutable constant; H1-E never writes it.
* **H1-E freeze** is a SEPARATE activation (`h1e_activation`) minted only
  in Phase C with a documented owner GO.  **Phase A mints no freeze**
  (`H1E_FREEZE_NOT_MINTED`); `H1EFreeze.from_owner_approval()` fail-closes
  on missing approval references, and `H1EFreeze.synthetic_fixture()` is
  labelled `SYNTHETIC_TEST_FIXTURE` and refused by the capture worker
  unless `allow_fixture_freeze=True` (offline tests).
* Admission is STRICT: `signal_time_us > freeze_ts_us`; `signal_time <=
  freeze_ts` → `H1E_REJECT_PRE_FREEZE` (persisted reason, never admitted).
* Preserved exclusions (fail-closed, explicit reasons):
  * the #305 UNVERIFIED window `2026-11-24T13:31:53.365952+00:00 →
    2026-11-25T07:14:41.230516+00:00` → `H1E_REJECT_UNVERIFIED_WINDOW`;
  * the historical **2026-11-25 SOXSUSDT** example →
    `H1E_REJECT_SOXSUSDT_QA_EXAMPLE` — QA fixture only, excluded from every
    future H1-E prospective denominator.

## 4. Causal HTF classification (no look-ahead)

* ASOF = the ORIGINAL `signal_time` (µs).  `captured_at` / delivery time
  never leaks into classification (pinned by test).
* Admissible candles: `close_us <= signal_time_us` — INCLUSIVE at the exact
  close boundary (frozen H1-C/H1-D contract).  Boundary is pinned at ±1 µs
  by tests: exact close → 60/60; one microsecond earlier → 59/60.
* 60 consecutive closed 4H (`interval=240`) and 60 closed 1D
  (`interval=D`) candles per event; no interpolation, no synthetic fill,
  no fallback to newer market states.
* Reused UNCHANGED: H1-D causal-ASOF coverage
  (`H1D_CAUSAL_ASOF_COVERAGE_V1`), H1-C `run_h1_event()`, H1-D
  `classify_event()` and `H1D_HTF_SNAPSHOT_2` provenance
  (`build_snapshot`/`snapshot_from_dict`/`replay_classification`).
* Fail-closed non-eligible results (explicit codes, evidence retained):
  `H1E_HTF_LIMITER_DENIED`, `H1E_HTF_RATE_LIMITED` (429 / retCode 10006),
  `H1E_HTF_BREAKER_DENIED`, `H1E_HTF_TIMEOUT`, `H1E_HTF_API_ERROR`,
  `H1E_HTF_RETRY_BUDGET_EXHAUSTED`, `H1E_HTF_INCOMPLETE_COVERAGE` (59/60
  with exact missing buckets), `H1E_HTF_INVALID_SOURCE` (malformed /
  duplicate / off-grid / invalid OHLCV / gaps), `H1E_SNAPSHOT_TAMPER`,
  `H1E_CLASSIFIER_ERROR`.
* **Never infer BULLISH/BEARISH/NEUTRAL from invalid data**: regime/BOS
  outputs exist only for eligible classifications; non-eligible outcomes
  carry diagnostic regimes in `h1d_diagnostics` exclusively.
* All nine valid 4H×1D combinations are retained verbatim — never merged,
  never averaged (regime fixtures verified for all nine).
* Deferral: retryable failures defer within a bounded budget
  (`RetryPolicy`: bounded attempts/cycle, injected `sleep` backoff,
  bounded deferrals) and then fail closed terminally.  Bounded work only —
  no busy retry.

## 5. Event-level capture semantics

`H1E_PROSPECTIVE_EVENT_CAPTURE_V1` (injectable reader/store/classifier):

* **Cursor** — monotonic composite `(captured_at_us, source_event_key)`,
  capture order (never `signal_time`) so late-committed events with earlier
  signal times stay in scope; batch boundaries can never drop a row; reader
  order violations fail closed.
* **Delivery** — at-least-once delivery with exactly-once effect: every
  read row is accounted (`read == admitted + rejected + duplicates +
  conflicts`, no silent skips); identical redelivery → idempotent
  `DUPLICATE` (first-capture snapshot kept verbatim); different preserved
  facts on the same identity → `H1E_EVENT_KEY_LINEAGE_CONFLICT`, isolated
  in the conflict journal, never an overwrite.
* **Atomicity** — ledger effects + checkpoint advance in ONE transaction
  (`H1EStore.commit_batch`); an interrupted transaction applies NOTHING;
  the next cycle re-reads from the previous checkpoint and converges to the
  identical outcome (crash between read/classify/commit covered by tests).
* **Freeze** — fail-closed admission gate (see §3); rejected rows are
  durable reject-ledger records with deterministic reason codes.

## 6. Ledger proposal (`H1E_PROSPECTIVE_LEDGER_V1`)

`sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` — **PROPOSAL
ONLY, DO NOT APPLY**.  Additive-only DDL for the isolated schema
`research_me_entry_quality_h1e` (never `dds.*`, never `market.*`, never
`research_me_upstream_coverage.*`):

`h1e_activation` (separate freeze, immutable via trigger),
`h1e_event` (immutable first-capture record; unique
`(source_event_key, h1e_protocol_fingerprint)`),
`h1e_classification` (append-only classification attempts),
`h1e_status_event` (append-only status journal — status is DERIVED),
`h1e_reject_event`, `h1e_conflict_event`, `h1e_provenance_snapshot`,
`h1e_checkpoint` (single row, monotonic trigger, same transaction as the
batch), `h1e_coverage_audit`.

Append-only is enforced by immutability triggers
(`H1E_LEDGER_IMMUTABLE`); checkpoint regression raises
`H1E_CHECKPOINT_REGRESSION`.  Rationale (retention / recovery):
see `324_h1e_prospective_ledger_ddl_v1.md`.

## 7. Coverage & health (`H1E_COVERAGE_HEALTH_V1`)

Five strictly separated dimensions, each `PASS/HOLD/GAP/CONFLICT`
(`UNKNOWN` resolves to `HOLD`, never `PASS`):

1. `SOURCE_CAPTURE_COVERAGE` — #305 capture evidence + blind intervals
   (unsupported evidence stays `UNKNOWN`/`HOLD`; #296
   `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` rule preserved);
2. `OBSERVER_DELIVERY_COVERAGE` — every eligible source event processed;
   within grace → `HOLD (COVERAGE_LAG)`, beyond grace → `GAP
   (COVERAGE_GAP)`; conflicts → `CONFLICT`;
3. `HTF_DATA_COVERAGE` — 60/60 candle coverage of the payloads actually
   used;
4. `CLASSIFICATION_ELIGIBILITY` — accepted / deferred / rejected /
   non-eligible strata accounting;
5. `OOS_STATISTICAL_READINESS` — pre-registered sample-size/diversity
   thresholds (never PASS before accumulation).

Overall verdict precedence `CONFLICT > GAP > HOLD > PASS`; a single-event
success never produces a blanket PASS.

## 8. OOS readiness (`H1E_OOS_READINESS_V1`) — proposal only

`324_h1e_oos_readiness_protocol_v1.md` + `oos_readiness.py`: cohorts
`H1_BULLISH` / `H1_BEARISH` / `H1_NEUTRAL` plus all nine 4H×1D
combinations; **unchanged baseline control** paired on identical
`source_event_key` (control never re-fit); pre-registered thresholds
(`TBD_FROZEN`), fees/slippage/TP-SL/timeout placeholders, follow-up
windows, `MAE_R` / `MFE_R` / missed winners / time-to-peak and
accepted/rejected + failure strata.  The module computes NO outcomes and
must not before a separate owner GO.

## 9. Verification

See `324_h1e_phase_a_verification_report_v1.md` for exact commands and
counts.  Summary: 98 new offline tests (redelivery, crash/restart, 60/60,
59/60, ±1 µs close boundary, nine regimes, limiter/429/10006/breaker/
timeout, tamper detection, atomic checkpoint, DDL contract, isolation,
PYTHONHASHSEED determinism) plus the H1-A/B/C/D + #305 regression suites.

## 10. Known blockers / owner GO decisions (unchanged by this PR)

1. **H1-E `freeze_ts`** — Phase A mints NONE.  Minting requires Phase C
   isolated deploy readiness + separate owner GO (Phase C is NOT prepared
   here).
2. **#305 upstream coverage** — remains `UPSTREAM_SOURCE_COVERAGE_UNPROVEN`
   (#296 rule); `SOURCE_CAPTURE_COVERAGE` stays `UNKNOWN/HOLD` until an
   independent upstream verification exists.
3. **Phase B (VPS read-only preflight)** — real read-only SQL source
   reader, real HTF transport binding through the shared limiter, live
   coverage run — NOT prepared here (separate owner GO).
4. **OOS policy freeze** — thresholds, execution model and the unchanged
   control definition must be separately reviewed and frozen before any
   accumulation or evaluation.
5. **DDL application** — the ledger schema is a proposal; applying it is a
   Phase C decision (`NO_DDL_APPLY` now).

**Status:** `H1E_PHASE_A_OFFLINE_IMPLEMENTATION / NO_DEPLOY / NOT_ACTIVATED
/ H1_OOS_HOLD`.  Merge of the implementation PR authorizes nothing beyond
review.
