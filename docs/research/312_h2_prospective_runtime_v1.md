# Issue #312 — H2_RSI_ONLY Prospective Runtime V1

**Parent:** #298 · **Issue:** #312 · **Event source:** #305 · **Base:** PR #311 (MERGED)
**Status after development:** `IMPLEMENTATION_READY / ACTIVATION_HOLD`
**Scope:** research-only runtime — deploy, merge and activation require separate GO.

---

## 1. Purpose

An independent, prospective-only research runtime for `H2_RSI_ONLY`.  It reads
original detector events from the #305 cohort
(`research_me_upstream_coverage.source_event`), evaluates the subsequent RSI
weakening on CLOSED 5m candles through the **already-implemented, pure** H2
engine (`app/research/me_entry_quality_h2_v1`, PR #311), and persists the
outcomes per H2 variant into a dedicated research-only schema.

The research question (unchanged from #298): does confirming the momentum
weakening improve SHORT entries relative to the baseline of entering without
confirmation?

**Hard rule:** RSI confirmation is NOT a trade.  No execution model is
approved (see `312_h2_entry_outcome_policy_v1.md`), therefore the runtime
stores RSI-confirmation outcomes only and publishes **no** yield metrics.

---

## 2. Contract integration analysis (pre-development)

This section is the required pre-development analysis of the current #305 and
H2 contracts and the exact integration points used by this runtime.

### 2.1 #305 upstream source (`app/research/me_upstream_source_v1`)

| Contract element | Value used by the H2 runtime |
|---|---|
| Source table | `research_me_upstream_coverage.source_event` (READ-ONLY) |
| Deterministic identity | `source_event_key` (stored, never recomputed) |
| Preserved lineage | `source_identity`, `lineage_hash`, `signal_time`, `activation_freeze_ts`, `captured_at` — carried VERBATIM |
| Prospective boundary | DB CHECK `signal_time > activation_freeze_ts AND captured_at >= activation_freeze_ts` (migration 094) — re-enforced at the H2 runtime boundary at microsecond precision |
| Lineage integrity | `lineage_hash` is the SHA-256 of the canonical JSON of `lineage`; the reader recomputes it and rejects mismatches (criterion 15) |
| Redelivery semantics | first-capture snapshot is immutable; a redelivered `source_event_key` differing on immutable source fields (`immutable_source_hash`, `IMMUTABLE_SOURCE_KEYS`) is an explicit `EVENT_KEY_LINEAGE_CONFLICT`, never a silent overwrite |
| Feature snapshot | `features` JSONB (`rsi_14`, `rsi_delta_3`, `entry_confirmation_rsi_rising`) — verbatim, never recalculated |
| Reference price | `source_reference_price` NUMERIC — verbatim exact decimal preserved (H2 `ORIGINAL_PRICE_PRECISION_POLICY`) |

The runtime **never writes** to `research_me_upstream_coverage.*` and never
modifies `freeze_ts`.  The reader issues `SELECT` statements only.

### 2.2 H2 pure engine (`app/research/me_entry_quality_h2_v1`, PR #311)

The runtime REUSES the merged implementation unchanged — no RSI algorithm,
event identity, or confirmation rule is duplicated:

| Integration point | Reused contract |
|---|---|
| Event identity + freeze guard | `run_h2_event()` internally runs the H1-C `adapt_source_events()` adapter and the prospective-cohort timing guard (`signal_time > activation_freeze_ts` strict, `captured_at >= freeze`) |
| Feature snapshot | `SourceFeatureSnapshot.from_mapping()` (verbatim) |
| Candle validation | `provide_5m_candles()` — UTC 5m grid, closed-only ASOF truncation, gap/duplicate/order checks, unconditional payload-hash verification |
| Provenance | `HtfSourceIdentity` / `build_source_identity()` / `source_payload_hash()` (H1-C contract, serialized as `candle_source`) |
| RSI | `rsi_wilder_series()` — parity with `app.indicators.technical.rsi_wilder`, seeded at the canonical window start (`H2_RSI_WINDOW_POLICY`) |
| Confirmation rules | `turn_1_confirmation`, `turn_2_confirmation`, `peak_reversal_confirmation` (PEAK_RULE_V1, causal lag-1) |
| Variants | `H2_RSI_TURN_1`, `H2_RSI_TURN_2`, `H2_RSI_PEAK_REVERSAL` |
| States | `PENDING_CONFIRMATION`, `CONFIRMED_DELAYED`, `EXPIRED`, `INVALID_SOURCE`, `INCOMPLETE_COVERAGE`, `NOT_READY` |
| Policy identity | `H2Policy.fingerprint()` — SHA-256 of the full parameter set; status is permanently `PRELIMINARY_CANDIDATE_NOT_FROZEN` until a separate freeze gate |
| Execution honesty | `entry_candidate_price=None`, `entry_price_basis=UNDETERMINED_NO_EXECUTION_MODEL`, confirmation close carried as `CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE` |

The pipeline is pure (`(row, rows_5m, source_identity, policies,
evaluation_asof_ms) -> outcome`); the runtime supplies inputs and persists
outputs.  `evaluation_asof_ms` is the **actual observation instant** (wall
clock at evaluation, injectable in tests).

### 2.3 Boundary additions owned by #312 (nothing else is touched)

1. `source_reader` — read-only #305 SELECT + ingest-time freeze/lineage
   checks + idempotent redelivery handling (own schema).
2. `candle_adapter` — bounded acquisition of closed 5m candles via
   `BybitClient.get_klines_range()` under a dedicated research API budget,
   provenance built with the H1-C identity contract; data verification
   delegated to the pure `provide_5m_candles()`.
3. `lifecycle` — deterministic persistence rules mapping pure pipeline states
   to restart-safe lifecycle states.
4. `store` — new schema `research_me_entry_quality_h2_prospective`
   (migration 095).
5. `telemetry` / `runner` — logging, counters, isolated entrypoint.
6. Execution/outcome policy SPEC document (no executable model implemented).

---

## 3. Architecture

```text
ME LONG V2 detector (UNCHANGED)
        |
        v
#305 upstream source capture (UNCHANGED, read-only here)
        |
        v
research_me_upstream_coverage.source_event
        |
        v
H2 SOURCE READER (source_reader.py, SELECT-only, freeze/lineage checks)
        |
        v
CLOSED 5m CANDLE PROVIDER (candle_adapter.py, Bybit get_klines_range,
        |                  dedicated research API budget, fail-closed)
        v
H2 RSI CONFIRMATION ENGINE (PR #311, reused unchanged)
        |
        +-- H2_RSI_TURN_1
        +-- H2_RSI_TURN_2
        +-- H2_RSI_PEAK_REVERSAL
        v
H2 PROSPECTIVE RESULT STORE (store.py + migration 095)
        |
        v
COVERAGE / TELEMETRY / EVALUATION (telemetry.py, runtime.py)
```

Runtime entrypoint: `python -m app.research.me_entry_quality_h2_prospective_v1.runner`
(never `scanner_runner.py`).  Disabled by default; systemd/env templates are
shipped NOT installed.

## 4. Lifecycle rules (FR-04)

State per (`source_event_key`, `variant_id`, `policy_fingerprint`):

* `PENDING_CONFIRMATION` — created at ingest for every policy; evaluation
  attempts recorded append-only;
* `CONFIRMED_DELAYED` / `EXPIRED` — terminal, taken verbatim from the pure
  pipeline (EXPIRED only on VALID data with the window fully observed);
* `INVALID_SOURCE` — terminal (source/feature/timing integrity failure);
* `NOT_READY` — terminal for a policy whose peak rule is not prespecified;
* `INCOMPLETE_COVERAGE` — terminal only when the observation window has fully
  elapsed AND the coverage defect is either permanent (candle gap / missing
  canonical window bucket / missing base candle) or persisted across
  `coverage_finalize_after_attempts` evaluation attempts.  Missing data NEVER
  becomes `EXPIRED` or `CONFIRMED_DELAYED` (criterion 6/7/12).

Transitions are one-way (pending -> terminal); terminal outcomes are written
once (unique key + immutable payload hash) and are never recreated on
redelivery (criterion 2/13).  Every evaluation attempt is journaled
(`evaluation_log`) so state survives restarts and unfinished events are never
skipped.

## 5. Safety constraints (unchanged from the issue)

* research-only, isolated, disabled by default;
* no writes to #305, no historical backfill, no scanner/trading/gate changes;
* bounded Bybit API budget; without a confirmed budget the candle provider
  stays OFF and the runtime reports `BLOCKED_CANDLE_SOURCE`;
* no fabricated candles, no fabricated execution prices, no fabricated
  Net E[R];
* deploy/merge/activation require a separate GO.

## 6. Related documents

* `312_h2_entry_outcome_policy_v1.md` — FR-06 execution/outcome policy spec
  with the explicit `BLOCKED_PENDING_DECISION` list;
* `312_h2_prospective_runbook_v1.md` — safe deployment runbook;
* `reports/issue312_implementation_report.md` — test results and audits.
