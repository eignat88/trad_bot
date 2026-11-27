# #324 H1-E — Prospective H1 4H/1D Classification Observer V1

**Stage:** PHASE A — OFFLINE IMPLEMENTATION ONLY. **Draft PR, no deploy, no VPS migration, no live Bybit calls, no activation.**

**Parent:** #297 (H1 Trend Alignment) / #295 (ME Entry Quality). **Source:** #305 independent pre-scoring `research_me_upstream_coverage.source_event` (subject to #296 lineage/readiness gate). **HTF classification:** merged #307 (H1-A/B), #309 (H1-C), #315 (H1-D), merge SHA `03d6fac466e2f9ddd890803649db54b13a451ba3`.

## Objective

Implement an **isolated prospective-only research observer** that eventually records causal 4H/1D regime, BOS and coverage for **new upstream events** after its own approved H1-E freeze. Prepare offline implementation, fault tests, and **DDL proposal** now; do not activate or run against real VPS data yet. Unlike H1-D historical QA, H1-E will be a durable, idempotent prospective event-level record — still NOT a trade signal and NOT an OOS profitability verdict.

## Source and temporal authority

- Read exclusively/primarily the independent #305 source, **not** post-filter `dds.scanner_setup` or V3 event ledger. Reuse original `source_event_key`, `source_identity`, exact microsecond `signal_time`, `lineage_hash`, `activation_freeze_ts`, protocol fingerprint and detector metadata without rebuilding identities from mutable presentation fields.
- Never alter #305 source or freeze: `2026-11-24T13:31:53.365952+00:00`.
- Explicitly preserve #305 **UNVERIFIED** `2026-11-24T13:31:53.365952+00:00` to `2026-11-25T07:14:41.230516+00:00`. The 2026-11-25 SOXSUSDT example is only historical QA, **excluded from all future H1-E prospective denominators**.
- A separate H1-E `freeze_ts` is assigned **only after** deployment/readiness and separate owner GO. This Phase A MUST NOT mint, reset or anticipate a real activation freeze. Reject `signal_time <= freeze_ts` and any event in excluded/unverified windows; persist explicit rejection/coverage reasons. Preserve the distinction between event_time, source captured_at, observed_at, and classification/fetch timestamps.
- Use only HTF candles with `close_us <= source_event.signal_time_us` (inclusive at the exact close boundary, frozen H1-C/H1-D contract), 60 consecutive closed 4H and 60 consecutive closed 1D bars; no look-ahead, no interpolation, no fallback to newer market states.

## Phase A implementation deliverables (all OFFLINE)

### 1. `H1E_SOURCE_READINESS_V1`
Build a pure/read-only readiness report contract with evidence, statuses `PASS/HOLD/BLOCK`, and independent checks for #305 source coverage + #296 lineage obligations, source fingerprint, known blind intervals, captured event identity, and HTF source/limiter availability. Unsupported checks must remain `UNKNOWN/HOLD`, not become PASS.

### 2. `H1E_PROSPECTIVE_EVENT_CAPTURE_V1`
Build an injectable source reader interface and deterministic bounded worker/scheduler **without running it**. Specify cursor semantics (monotonic composite key, stable ordering), at-least-once delivery with exactly-once effect, checkpoint atomicity with ledger insert, retry on interrupted transaction, duplicate idempotency, collision fail-closed and gap reconciliation. Do NOT bind to a production service or use old events as live samples.

### 3. `H1E_HTF_ASOF_CLASSIFICATION_V1`
Integrate frozen H1-D `240`/native `D` adapter, H1-C `run_h1_event()`, provenance and `H1D_HTF_SNAPSHOT_2` via injectable offline transport/provider. Treat limiter denial, 429/10006, API failure, timeout, malformed/duplicate/off-grid/OHLCV candles, 59/60, snapshot tamper and lineage conflict as explicit **non-eligible** results with evidence; never infer bearish/bullish/neutral from invalid data. Retain all nine valid 4H×1D regime combinations.

### 4. `H1E_PROSPECTIVE_LEDGER_V1`
Design **append-only research-only** schema/DDL as a proposal (SQL file/docs, **DO NOT APPLY**) with original source event identity + lineage, H1-E protocol version/fingerprint, activation freeze, signal_time µs, source/capture/classification timing, 4H/1D regime+BOS+coverage, eligibility and diagnostic codes, snapshot hashes, status and immutable decision. Unique identity per (source_event_key, H1 protocol/fingerprint), explicit conflicting-payload isolation, atomic checkpoint/ledger semantics. Consider status transitions without overwriting immutable evidence (append-only journal/events; current status derived). Separate namespaces/tables from #305, production, existing OOS. Explain retention and deterministic recovery. Document SQL dialect from existing project only after inspecting related migrations; do not invent schema names/columns as deployed facts.

### 5. `H1E_COVERAGE_HEALTH_V1`
Design reconciliation with immutable windows, grace lag, unmatched #305 events, rejected, duplicate, conflict, failed HTF coverage, processing delays, shared limiter denial, provider unavailability and source blind intervals. Distinguish:
`SOURCE_CAPTURE_COVERAGE`, `OBSERVER_DELIVERY_COVERAGE`, `HTF_DATA_COVERAGE`, `CLASSIFICATION_ELIGIBILITY`, `OOS_STATISTICAL_READINESS`.
Define `PASS/HOLD/GAP/CONFLICT` based on auditable evidence; no blanket PASS on single-event success.

### 6. `H1E_OOS_READINESS_V1`
Write **proposal only**, no evaluation: pre-registration template for cohorts `H1_BULLISH`, `H1_BEARISH`, `H1_NEUTRAL` and 4H×1D combinations vs **unchanged baseline control** on identical `source_event_key`, sample size / diversity thresholds, fees/slippage and execution model, followup windows, MAE_R/MFE_R, missed winners, time_to_peak, accepted/rejected and failure strata. No outcome computation, no tuning, no activation. Require separately reviewed/frozen policy and GO for accumulation/evaluation.

## Offline tests and PR completion criteria

- Pure/injectable fake #305 source and HTF providers; tests use **ZERO network and ZERO production DB**.
- New immutable freeze only in synthetic fixtures; pre-freeze and boundary events rejected, historical SOXSUSDT not admitted.
- 4H/1D 60/60 vs 59/60; close boundary ±1 µs; future append cannot change an earlier classification; 9 regimes; missing/gap/conflict; invalid OHLCV; invalid source lineage/fingerprint.
- Deterministic identity across `PYTHONHASHSEED`; duplicate delivery and restarts, out-of-order events, checkpoint crash between read/classify/commit, no silent skips, unchanged outcome.
- API 429/10006, shared limiter denial, retry budget/breaker deferral, timeout; bounded work and backoff (only mock transport); fail closed with reason.
- Provenance snapshot tamper fails closed; offline replay deterministic.
- Check **no imports/wiring** from scanner/paper/evaluator, no production DDL execution, no existing service or environment modifications, no systemd, no activation flags enabled.
- Targeted offline and existing H1-A/B/C/D regression tests PASS; document exact commands/counts; record any baseline failures honestly.
- PR contains code, tests, research-only DDL proposal, architecture/identity/freeze/coverage/rollout documentation and explicit **Phase B/C hold**.

## Delivery format for development agent

1. Inspect actual #305 tables/columns and #296 policy **from repository code/tests** before implementing interfaces. Clearly mark any VPS-only details `UNKNOWN` and propose read-only confirmation queries; do not claim live schema confirmed.
2. Commit all Phase A changes **only** on `research/324-h1e-prospective-observer-v1`; keep scope limited to new research-only files/tests/docs/migration-proposal; any change to existing production file must be separately justified and approved.
3. Post a PR review checklist: changed files, tests, identities, scenario matrix, unverified gap accounting, immutable freeze rule, additive-only schema, and all outstanding owner GO decisions.
4. Never perform Phase B/C/D/E or merge without separate explicit owner authorization.

## Status transitions

`PHASE_A_IMPLEMENTATION_PENDING → PHASE_A_OFFLINE_TESTED → PHASE_A_REVIEWED → (SEPARATE OWNER GO) PHASE_B_VPS_READ_ONLY_PREFLIGHT → (SEPARATE OWNER GO) PHASE_C_ISOLATED_DEPLOY/FREEZE → PROSPECTIVE_ACCUMULATION → OOS_EVALUATION → VERDICT`.

**Current status:** `H1E_PHASE_A_SPEC_ONLY / NO_DEPLOY / NOT_ACTIVATED / H1_OOS_HOLD`. Merge of the implementation PR would not authorize deployment or accumulation.
