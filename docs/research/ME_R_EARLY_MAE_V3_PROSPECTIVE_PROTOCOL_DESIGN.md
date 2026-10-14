# ME_R_EARLY_MAE_V3_CLOSED_CANDLE_PROSPECTIVE_PROTOCOL_DESIGN

**Project:** `trad_bot`
**Mode:** RESEARCH-ONLY / DESIGN-FIRST / FAIL-CLOSED
**Status:** OPERATOR-APPROVED DESIGN DRAFT — implementation, freeze, activation, and deployment remain unapproved
**Design synchronization base commit:** `c1b54f56cffd4bfa768967a137a11b677748e36d`
**Experiment ID:** `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`
**Protocol version:** `3.0.0-DRAFT`
**Schema:** `research_me_early_mae_v3`

## 1. Objective

Design a new, independent prospective OOS experiment to test whether early MAE-based exits improve the net expectancy of Momentum Exhaustion Reverse LONG setups when the source population is detected only from completed candles.

This protocol addresses the frozen-source incompatibility identified in `ME_R_LONG_EARLY_MAE_EXIT_OOS_V2` without modifying frozen V2 or the existing `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS`.

This task is protocol design only. No schema is to be migrated, no experiment is to be registered, no production scanner or paper/live process is to be changed, and no natural source is to be backfilled.

## 2. Baseline evidence

Read-only inspection confirmed:

- VPS `/opt/trad_bot` HEAD: `c8ca2f2c79652e8c026e7180372605897cdfff42`.
- VPS `main` working tree: clean.
- `trad-bot-scanner.service`: active.
- `trad-bot-paper.service`: active.
- Frozen V2 remains immutable and isolated.
- V2 has seven registered variants but no natural V2 source cohort to reuse.
- The V2 source adapter requires the V1 scanner identity and rejects a source candle that closes after detection.
- The compatibility audit found post-activation V1 LONG sources using unclosed 5m candles; changing V1 intrabar detection into closed-candle detection changes source population, timing, geometry, and identity.

The local worktree is dirty. The V3 package referenced in the task (`app/research/me_reverse_long_v3/`) is absent from the local worktree and is not treated as an available implementation dependency. Historical references to it are evidence only; the new design must not merge with that experiment.

## 3. Source population recommendation

**Recommendation: Option A — Closed-candle evaluation of V1 rules.**

The new source detector is a research-only implementation of the frozen V1 detection rules, evaluated against an isolated completed-candle context. It is not a modification of production V1 and is not a reuse of V2 source events.

### Option A: Closed-candle V1 rules

- Detection rules: bearish exhaustion after a prior 15m swing high, 5m return near the prior high, bearish and sufficiently small body, and closed-1h RSI confirmation.
- Source identity: `ME_R_LONG_CLOSED_V1`, version `1.0.0-DRAFT`.
- Indicators and candles: completed 5m, 15m and 1h candles only; indicators recomputed from the completed 1h context at detection time.
- Population difference: later and potentially different from V1 intrabar signals because the decision uses completed candle closes.
- Frequency implication: one possible source per completed signal candle; duplicate/repeated evaluation of the same source candle is idempotent.
- Hypothesis compatibility: directly tests early MAE exits on the V1 setup definition without adding a second entry-confirmation condition.
- Reproducibility: source identity is `(protocol_version, source_setup_id)` plus an instrument/candle/source-version uniqueness rule.
- Paper/live independence: the detector is research-only and must not route candidates to paper or live execution.
- Existing test coverage: closed-candle ASOF logic exists for the ME reverse-long close-location experiment, but no dedicated V1 closed-candle detector exists; parity and no-look-ahead tests are required.
- Implementation complexity: medium; the existing context builder and closed-candle guard provide architectural references.

### Option B: Existing closed-candle V2 scanner rules

- Detection rules: V1 rules plus `rsi_delta_3 > 0`.
- Source identity: `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`.
- Indicators and candles: completed 5m, 15m and 1h candles, plus a closed-5m RSI delta calculation.
- Population difference: materially different population because the additional rising-RSI condition filters signals and changes feature semantics.
- Frequency implication: likely lower source frequency and a different distribution of MAE outcomes.
- Hypothesis compatibility: tests a V2-filtered setup rather than the original V1 setup; this is not a like-for-like replacement for the V1 hypothesis.
- Reproducibility: acceptable if isolated in a new schema, but the added filter makes attribution less clean.
- Paper/live independence: possible, but unnecessary coupling to a separate scanner hypothesis.
- Existing test coverage: V2 scanner tests exist, but they do not establish equivalence to V1.
- Implementation complexity: lower code reuse, higher scientific ambiguity.

Option B is rejected for the primary V3 cohort because it would confound the early-MAE question with the V2 RSI-rising filter. Option A is selected to preserve the V1 setup while repairing the temporal boundary.

## 4. Proposed source identity and experiment identity

- Experiment ID: `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`.
- Scanner name: `ME_R_LONG_CLOSED_V1`.
- Scanner version: `1.0.0-DRAFT`.
- Direction: `LONG`.
- Source schema: `research_me_early_mae_v3`.
- Source candle version: `MARKET_CANDLE_CLOSED_5M_V1`.
- Source setup ID: deterministic hash of protocol version, scanner name/version, instrument ID, source candle open time, and a canonical source payload fingerprint.
- Collision behavior: an identical replay returns the existing source; a conflicting payload with the same identity is `SOURCE_IDENTITY_DRIFT` and fails closed.

The proposed ID was checked against local documentation and Python references; no existing exact experiment ID was found. The operator approved this experiment identity for the design, while the final collision check against the production registry and database remains required before freeze.

## 5. Temporal contract

All timestamps are UTC and must be timezone-aware. Source timestamps must come from source data; they must never be fabricated.

### Required fields

| Field | Definition |
|---|---|
| `source_candle_open_at` | Opening time of the completed 5m signal candle. |
| `source_candle_close_at` | Closing time of the completed 5m signal candle. |
| `source_fetch_started_at` | UTC time at which the fetch of required source candles began. |
| `source_available_at` | `source_candle_close_at` plus the configured processing-latency allowance; never earlier than candle close and never later than `source_candle_close_at + 300 seconds`. |
| `detected_at` | UTC time at which the closed-candle detector completed evaluation and produced a source event. |
| `entry_eligible_at` | `max(source_available_at, detected_at, activation_at)`. |
| `entry_filled_at` | Deterministic virtual fill timestamp selected by the entry policy. |
| `baseline_deadline_at` | `entry_filled_at + baseline_max_hold_minutes`. |
| `variant_horizon_end_at` | `entry_filled_at + variant.horizon_minutes`. |

### Look-ahead rules

1. Every 5m, 15m and 1h input candle must satisfy `candle_open_at + timeframe_duration <= detected_at`.
2. Every indicator must be computed only from candles satisfying that rule.
3. `source_candle_close_at <= detected_at`.
4. `source_available_at - source_candle_close_at <= 300 seconds`; a later or incomplete signal is rejected and may not become eligible.
5. A source event may not be evaluated until all required source windows are closed and available.
6. The evaluator may only use candles whose close time is no later than the evaluation ASOF timestamp.
7. No source event may be captured before activation.
8. No historical source event may be inserted into the prospective cohort.

### Data-quality and restart rules

- Missing candles: mark required coverage incomplete; do not synthesize a candle.
- Duplicate candles: reject the affected window as `DUPLICATE_CANDLE_TIMESTAMP`; do not silently select one.
- Unordered candles: sort only when timestamps are unique and contiguous; an unordered or duplicate stream is invalid.
- Gapped coverage: `INCOMPLETE_COVERAGE`; retain diagnostics and never finalize.
- Stale 5m/15m/1h data: a candle is usable only after its close and only if its timestamp is in the expected aligned sequence; stale data is recorded in pipeline diagnostics.
- Fetch boundary crossing: record the fetch interval and reject any candle closing after the ASOF boundary.
- Source arrival after intended entry time: the source remains pending until entry eligibility, but may not be backdated into a pre-availability entry.
- Scanner restarts: source identity is idempotent; duplicate replay repairs missing variant rows only when all frozen fields match.
- Repeated source processing: source and all seven variants are written in one transaction; finalized outcomes are immutable.
- Clock skew: if the local clock is earlier than the latest verified source close time, the run fails closed rather than evaluating a potentially future candle.

## 6. Entry execution comparison

### Model A — `NEXT_OPEN_5M`

The virtual fill uses the first completed 5m candle whose open is at or after `entry_eligible_at`, and therefore strictly after actual signal availability under the approved 300-second source-delay limit. The fill price is the completed candle open. The fill timestamp is that candle's open time only after the candle closes and the full coverage window is verified.

Advantages:

- causally feasible from OHLC data;
- does not claim unavailable first-touch times;
- deterministic and easy to test;
- preserves a conservative signal-to-entry delay;
- comparable across all seven variants.

Limitations:

- may miss an entry-zone touch that occurs inside the first eligible candle;
- may produce a fill outside the source entry zone.

### Model B — `ENTRY_ZONE_TOUCH`

A virtual fill occurs when a completed candle's low/high interval touches the entry zone, with fill at the conservative zone boundary. It is feasible only when the zone touch is known from completed candle data and the entire entry window has complete coverage.

Advantages:

- closer to the prior V2 hypothesis;
- can model entries that occur before the next candle open.

Limitations:

- same-candle entry and MAE/TP/SL ordering is not observable from OHLC;
- it requires conservative ambiguity handling and can create a signal-to-entry timing bias;
- a missed fill and a delayed fill are harder to distinguish without trade-level data.

### Approved policy

Use `NEXT_OPEN_5M` as the sole primary entry policy for V3. Do not combine it with `ENTRY_ZONE_TOUCH` in one paired comparison. A separate `ENTRY_ZONE_TOUCH` experiment would require a new protocol version and a separate cohort.

## 7. Baseline execution and economics

Approved baseline policy: `FIXED_TP_SL_HORIZON_V3`, subject to later freeze and activation approvals.

| Parameter | Approved design value |
|---|---|
| Direction | LONG |
| Entry policy | `NEXT_OPEN_5M` |
| Stop geometry | `entry_price * (1 - 2.5%)` |
| Profit target geometry | `entry_price * (1 + 3.0%)` |
| Baseline maximum hold | 240 minutes |
| Entry expiry | 240 minutes |
| Taker fee | 0.055% per side |
| Normal slippage | 0.05% per side |
| Elevated slippage | 0.10% per side |
| Funding | Not modeled |
| DCA, trailing, breakeven | Disabled |

For every eligible filled source event, define:

```text
risk_distance = entry_price - stop_price
gross_r = (exit_price - entry_price) / risk_distance
net_r = gross_r - round_trip_cost_r
baseline_net_r = baseline exit net_r
variant_net_r = variant exit net_r
delta_net_r = variant_net_r - baseline_net_r
```

`round_trip_cost_r` uses the same fee and slippage model for baseline and variants. The normal cost model is primary; elevated slippage is a preregistered sensitivity analysis, not an outcome filter.

The baseline exit is the first terminal event among stop and target in completed candles, then the timeout close at the baseline deadline. Because OHLC does not reveal intrabar ordering, same-candle stop and target hits use the conservative frozen policy `STOP_FIRST`, with `ambiguous_same_candle = true` retained in the record. Timeout uses the first completed 5m candle close at or after the 240-minute deadline.

All seven variants must be evaluated on the same eligible filled source events. Non-filled, invalid, incomplete-coverage, immature and finalized samples are separate categories and are never mixed in primary expectancy calculations.

## 8. Seven-variant approval

The operator approved the following matrix for the design; it remains unfrozen.

| Variant | Horizon | Threshold | Role |
|---|---:|---:|---|
| `MAE_10m_040` | 10m | 0.40R | SECONDARY |
| `MAE_10m_050` | 10m | 0.50R | SECONDARY |
| `MAE_15m_040` | 15m | 0.40R | SECONDARY |
| `MAE_15m_050` | 15m | 0.50R | PRIMARY |
| `MAE_15m_060` | 15m | 0.60R | SECONDARY |
| `MAE_15m_075` | 15m | 0.75R | SECONDARY |
| `MAE_30m_050` | 30m | 0.50R | SECONDARY |

### Exit semantics

1. Compute MAE from completed 5m candles in `[entry_filled_at, variant_horizon_end_at]`.
2. If the baseline terminates before the horizon, the variant inherits the baseline outcome and is `BASELINE_EXITED_EARLY`.
3. If the MAE threshold is reached during the variant window and baseline SL or TP has not already terminated the position, use `HORIZON_END_CONDITIONAL_MAE_EXIT`: the research exit proxy is the CLOSE of the completed 5m candle at the horizon boundary. Immediate first-touch MAE exit is forbidden.
4. If the baseline stop or target is reached in a candle inside the horizon, the variant inherits that baseline terminal outcome.
5. If threshold, stop, target, and horizon boundaries occur within one candle and ordering cannot be proven from OHLC, mark `ambiguous_same_candle = true`; apply the conservative baseline-first ordering and do not treat the variant as a favorable execution.
6. If required candles are missing or the horizon close is unavailable, mark `INCOMPLETE_COVERAGE`; never fabricate an exit.
7. Variant MAE/MFE windows end at the variant horizon or at the earlier baseline terminal event, never at wall-clock time after maturity.
8. Triggered and non-triggered variants must retain explicit `trigger_result`, exit reason, coverage state, and ambiguity state.

`MAE_15m_050` is the approved PRIMARY because it balances a short horizon against a moderate adverse move. Its role is a preregistered primary comparison, not evidence of edge before prospective OOS results.

The horizon-end CLOSE is a research execution proxy. It is not necessarily an executable live price and may differ materially from the intrabar threshold-touch price. Before implementation, causal and executability verification must confirm that the proxy can be evaluated only from data available at the completed horizon boundary and must document its limitations relative to any possible real execution.

## 9. Persistence architecture

Create an independent PostgreSQL schema `research_me_early_mae_v3`. Do not reuse `research_me_early_mae_v2`, `dds.paper_trade`, `research.research_signal`, or existing ME Reverse Long V3 observations.

### Proposed tables

#### `protocol`

- `protocol_version TEXT PRIMARY KEY`
- `experiment_id TEXT NOT NULL`
- `scanner_name TEXT NOT NULL`
- `scanner_version TEXT NOT NULL`
- `source_candle_version TEXT NOT NULL`
- `direction TEXT NOT NULL CHECK (direction = 'LONG')`
- `frozen_at TIMESTAMPTZ`
- `activation_at TIMESTAMPTZ`
- `protocol_json JSONB NOT NULL`
- `protocol_fingerprint TEXT NOT NULL`
- `registered_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `approved_by TEXT`

Constraints: unique experiment ID and fingerprint; freeze and activation may be null only before operator approval.

#### `variant`

- `protocol_version TEXT REFERENCES protocol`
- `variant_id TEXT`
- `horizon_minutes INTEGER CHECK (horizon_minutes > 0)`
- `threshold_mae_r NUMERIC CHECK (threshold_mae_r > 0)`
- `is_primary BOOLEAN NOT NULL DEFAULT false`
- Primary key: `(protocol_version, variant_id)`
- Exactly seven rows must exist before source capture is enabled.
- Exactly one primary row is required: `MAE_15m_050`.
- All other variant rows must have `is_primary = false`.

#### `source_event`

- `source_event_id BIGSERIAL PRIMARY KEY`
- `protocol_version TEXT REFERENCES protocol`
- `source_setup_id TEXT NOT NULL`
- `instrument_id INTEGER`
- `symbol TEXT NOT NULL`
- `scanner_name TEXT NOT NULL`
- `scanner_version TEXT NOT NULL`
- `source_candle_version TEXT NOT NULL`
- `direction TEXT NOT NULL CHECK (direction = 'LONG')`
- `source_candle_open_at TIMESTAMPTZ NOT NULL`
- `source_candle_close_at TIMESTAMPTZ NOT NULL`
- `source_fetch_started_at TIMESTAMPTZ NOT NULL`
- `source_available_at TIMESTAMPTZ NOT NULL`
- `detected_at TIMESTAMPTZ NOT NULL`
- `entry_eligible_at TIMESTAMPTZ NOT NULL`
- `entry_filled_at TIMESTAMPTZ`
- `entry_price NUMERIC`
- `stop_price NUMERIC`
- `target_price NUMERIC`
- `risk_distance NUMERIC`
- `baseline_deadline_at TIMESTAMPTZ`
- `source_status TEXT NOT NULL`
- `coverage_status TEXT NOT NULL`
- `invalid_reason TEXT`
- `source_features JSONB NOT NULL DEFAULT '{}'::jsonb`
- `protocol_fingerprint TEXT NOT NULL`
- Unique constraints:
  - `(protocol_version, source_setup_id)`
  - `(protocol_version, instrument_id, scanner_name, direction, source_candle_open_at)`
- Check constraints:
  - `source_candle_close_at <= detected_at`
  - `source_available_at <= detected_at`
  - `entry_filled_at >= entry_eligible_at`
  - positive risk distance when filled
  - valid terminal statuses only

Invalid source records are retained for diagnostics when they demonstrate a real activation, identity, geometry, or temporal violation. Synthetic historical records are forbidden.

#### `variant_outcome`

- `protocol_version TEXT`
- `source_event_id BIGINT REFERENCES source_event`
- `variant_id TEXT`
- `horizon_minutes INTEGER`
- `threshold_mae_r NUMERIC`
- `candle_source_version TEXT`
- `variant_status TEXT NOT NULL`
- `trigger_result TEXT`
- `mae_r NUMERIC`
- `mfe_r NUMERIC`
- `window_low NUMERIC`
- `window_high NUMERIC`
- `variant_exit_at TIMESTAMPTZ`
- `variant_exit_price NUMERIC`
- `variant_exit_reason TEXT`
- `variant_gross_r NUMERIC`
- `variant_net_r NUMERIC`
- `delta_net_r NUMERIC`
- `ambiguous_same_candle BOOLEAN NOT NULL DEFAULT false`
- `coverage_reason TEXT`
- `evaluated_at TIMESTAMPTZ`
- Primary key `(protocol_version, source_event_id, variant_id)`
- Exactly seven rows are required for each eligible filled source.

#### `pipeline_run`

- `pipeline_run_id BIGSERIAL PRIMARY KEY`
- `protocol_version TEXT`
- `started_at TIMESTAMPTZ`
- `finished_at TIMESTAMPTZ`
- `status TEXT`
- `sources_seen INTEGER`
- `sources_inserted INTEGER`
- `fills_created INTEGER`
- `variants_written INTEGER`
- `errors INTEGER`
- `diagnostics JSONB`

### Lifecycle and atomicity

- Protocol registration is immutable after approval.
- Source insertion and all seven variant rows are one transaction.
- Baseline and seven paired outcomes are one transaction.
- Restart recovery processes only non-terminal records.
- Finalized outcomes are immutable.
- Rejection reasons include `PRE_ACTIVATION`, `SCANNER_IDENTITY_MISMATCH`, `UNCLOSED_SOURCE_CANDLE`, `SOURCE_UNAVAILABLE`, `INVALID_GEOMETRY`, `INCOMPLETE_COVERAGE`, `DUPLICATE_CANDLE_TIMESTAMP`, `SOURCE_IDENTITY_DRIFT`, and `CLOCK_SKEW`.
- Coverage states include `PENDING`, `COMPLETE`, `INCOMPLETE`, and `MATURE`.
- Source publication lifecycle: `DETECTED`, `ENTRY_PENDING`, `ENTRY_FILLED`, `ENTRY_NOT_FILLED`, `INVALID`, `FINALIZED`.
- Terminal states require all seven variant outcomes and complete source coverage.
- Restart recovery must be idempotent and must never backfill historical source events.

## 10. Freeze and activation design

Before operator approval:

- no `frozen_at` assignment;
- no `activation_at` assignment;
- no production capture;
- no evaluator startup;
- no source publication;
- no schema migration in this task.

After protocol approval:

1. Store a frozen protocol JSON payload.
2. Compute a deterministic SHA256 fingerprint over canonical JSON.
3. Obtain an operator-approved UTC freeze timestamp.
4. Separately obtain an operator-approved UTC activation timestamp.
5. Enable only post-activation source eligibility.
6. Reject historical source insertion.
7. Verify the runtime fingerprint on every startup and before each write batch.
8. Verify the code commit, schema version, environment, and service configuration.
9. Fail closed on any invariant mismatch, missing timestamp, incomplete variant tuple, clock skew, or paper/live route.
10. Require an explicit service opt-in and a dedicated systemd unit; the evaluator must never be enabled implicitly.

Frozen V2 timestamps, fingerprints, schemas, source rows, and outcomes remain unchanged.

## 11. Statistical validation design

Prospective checkpoints:

- minimum eligible filled observations;
- minimum distinct symbols;
- minimum OOS days;
- complete source coverage rate;
- seven-variant outcome completeness;
- symbol concentration;
- market-regime concentration;
- missing-data and ambiguous-candle rates;
- non-fill rate;
- evaluator uptime and restart recovery.

Recommended initial operational checkpoint: 30 eligible filled source events across at least 10 distinct symbols, at least 5 distinct OOS days, no unresolved identity or coverage errors, and seven complete variant rows for every eligible source. This checkpoint remains pending operator approval.

Recommended later statistical verdict checkpoint: at least 100 eligible filled source events across at least 20 distinct symbols and 20 distinct OOS days, with a documented concentration analysis and preregistered multiple-comparison handling. This checkpoint and the multiple-comparison methodology remain pending operator approval. These are proposed operational criteria, not claims of statistical power or positive edge.

Reporting:

- baseline Net E[R];
- variant Net E[R];
- paired Delta Net E[R];
- Net Profit Factor;
- win rate;
- MAE trigger rate;
- non-fill rate;
- confidence intervals for paired deltas;
- sensitivity to elevated slippage;
- multiple-comparison risk across seven variants.

Primary inference must be paired by source event and must exclude non-filled, invalid, immature, and incomplete-coverage samples. A separate sensitivity report may include elevated slippage. No positive edge may be claimed before natural OOS data is collected.

The preregistered primary estimand is paired `Net Delta E[R]` for `MAE_15m_050` against the shared baseline. The other six variants are exploratory secondary analyses. Sample-size checkpoints, statistical stopping rules, and multiple-comparison adjustment methodology remain open pending operator approval.

## 12. Integration design

Proposed isolated components:

- research-only closed-candle V1 detector;
- scanner runner entrypoint with explicit opt-in;
- source adapter restricted to the new scanner identity;
- PostgreSQL repository using the new schema;
- dedicated evaluator;
- dedicated systemd unit `trad-bot-me-mae-v3-evaluator.service`;
- structured journal logging and diagnostics;
- optional Grafana research metrics fed only from the new schema.

The implementation must not modify:

- production V1 or V2 scanners;
- production direction gates;
- paper trading;
- live trading;
- cooldown or open-position limits;
- frozen V2;
- the existing ME Reverse Long V3 experiment;
- other prospective research experiments.

The V3 runner must be independently disableable and must fail closed if the operator opt-in flag, protocol fingerprint, activation boundary, or schema state is inconsistent.

## 13. Testing strategy

Deterministic tests:

- closed-candle temporal validation;
- correct 5m/15m/1h ASOF inputs;
- closed-candle V1 parity against the reference detector;
- no look-ahead;
- activation and fingerprint guards;
- stable source identity;
- duplicate source replay;
- restart recovery;
- entry timing under `NEXT_OPEN_5M`;
- entry coverage;
- no fill;
- early baseline stop or target;
- MAE threshold crossing;
- MAE horizon exit;
- ambiguous same-candle events;
- seven-variant atomicity;
- incomplete coverage;
- paired outcome consistency;
- evaluator recovery after process restart;
- paper/live isolation;
- ME Reverse Long V3 isolation;
- frozen V2 invariance.

Natural E2E acceptance checklist:

- the dedicated evaluator starts only with explicit opt-in;
- the first source is post-activation;
- the source candle closes before detection;
- all required indicators are from closed candles;
- one source row and seven variant rows are created atomically;
- no pre-activation row is eligible;
- no paper or live trade, position, balance, direction gate, cooldown, or production risk control changes;
- restart replay creates no duplicate identity;
- finalized rows are immutable;
- incomplete or ambiguous rows are never finalized;
- the new cohort is separate from frozen V2 and ME Reverse Long V3.

## 14. Deliverables

- Protocol design: `docs/research/ME_R_EARLY_MAE_V3_PROSPECTIVE_PROTOCOL_DESIGN.md`.
- Architecture decision record: `docs/research/ME_R_EARLY_MAE_V3_ARCHITECTURE_DECISIONS.md`.
- DRAFT frozen payload: `docs/research/ME_R_EARLY_MAE_V3_PROPOSED_FROZEN_PAYLOAD.json`.
- Implementation plan: `docs/research/ME_R_EARLY_MAE_V3_IMPLEMENTATION_PLAN.md`.
- Verification plan: `docs/research/ME_R_EARLY_MAE_V3_VERIFICATION_PLAN.md`.
- Open decisions: `docs/research/ME_R_EARLY_MAE_V3_OPEN_DECISIONS.md`.

## 15. Design verdict

`DESIGN_APPROVED_FOR_IMPLEMENTATION_PLANNING_ONLY`

The operator approved the source, experiment identity, entry timing, 300-second source-delay limit, baseline economics, `STOP_FIRST`, fail-closed coverage, deduplication, `HORIZON_END_CONDITIONAL_MAE_EXIT`, seven-variant matrix, `MAE_15m_050` primary, paired statistical framing, and research-schema isolation. The protocol payload remains a draft and must not be frozen, activated, deployed, registered, backfilled, or claimed as accumulating OOS data. Implementation, migration, deployment, freeze, activation, and unresolved open decisions require separate approvals.
