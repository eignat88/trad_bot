# ME_R_EARLY_MAE_V3 Architecture Decision Record

**Status:** OPERATOR-APPROVED DESIGN DRAFT
**Mode:** RESEARCH-ONLY / FAIL-CLOSED / NO FREEZE OR ACTIVATION
**Design synchronization base commit:** `c1b54f56cffd4bfa768967a137a11b677748e36d`

## Decision 1 — Use a new experiment and schema

Create `ME_R_LONG_EARLY_MAE_EXIT_OOS_V3` in `research_me_early_mae_v3`.

Frozen V2 `ME_R_LONG_EARLY_MAE_EXIT_OOS_V2` remains immutable and isolated. Its source, outcome, variant, and pipeline tables are not reused. The existing ME Reverse Long V3 experiment is a separate research experiment and is not merged with this MAE cohort.

Reason: changing V1 intrabar capture to closed-candle capture changes the source population, signal timing, geometry, and identity.

## Decision 2 — Source design is Option A

Use an independent research-only closed-candle implementation of the frozen V1 rules.

Option B, the existing closed-candle V2 scanner, is rejected for the primary cohort because it adds `rsi_delta_3 > 0`. That condition is a separate hypothesis and would confound early MAE attribution.

The new detector is not production V1. It may reuse the closed-candle ASOF context pattern but must have its own scanner identity and source schema.

## Decision 3 — Temporal boundary is completed candles only

Every 5m, 15m, and 1h input must be closed before `detected_at`. Indicators are recomputed from completed candles. A source candle is never accepted if its close time is after detection. The maximum source delay is 300 seconds after signal-candle close; later or incomplete signals are rejected.

`source_available_at` is derived from actual source data and a measured or configured processing latency. It is never fabricated to make a source eligible.

## Decision 4 — Primary entry is NEXT_OPEN_5M

Use the first completed 5m candle open at or after source eligibility.

ENTRY_ZONE_TOUCH remains documented as an alternative but is not used in this paired protocol. A separate protocol version would be required to test it.

Reason: NEXT_OPEN_5M is causally feasible from OHLC data, avoids unsupported first-touch timestamps, and is deterministic under restart.

## Decision 5 — Conservative OHLC ambiguity policy

OHLC data cannot prove intrabar ordering. Same-candle stop/target or threshold/terminal conflicts are marked ambiguous and use the conservative baseline-first or stop-first ordering. No result may claim an unavailable first-touch execution time.

## Decision 6 — Independent persistence

Use `research_me_early_mae_v3` with immutable protocol registration, source identity constraints, atomic seven-variant writes, idempotent restart recovery, explicit coverage states, and terminal-state immutability.

No production table, paper trade table, or existing research table is reused.

## Decision 7 — Freeze and activation remain unapproved

The operator approved the design decisions recorded here, but no frozen timestamp, activation timestamp, schema migration, service start, source publication, or evaluator run is authorized. Freeze and activation are separate operator decisions after implementation verification and final review.

## Decision 8 — MAE exit is horizon-end conditional

Evaluate MAE over the complete 10m, 15m, or 30m variant window. If the threshold is reached and baseline SL/TP has not already terminated the position, use the completed 5m CLOSE at the horizon boundary as the research execution proxy through `HORIZON_END_CONDITIONAL_MAE_EXIT`. Immediate first-touch exit is forbidden. The horizon-end CLOSE is not necessarily executable and requires explicit causal verification before implementation.

## Decision 9 — PRIMARY and statistical roles are fixed

Exactly seven variants are approved. `MAE_15m_050` is the sole PRIMARY. The remaining six variants are exploratory SECONDARY analyses. The preregistered primary comparison is paired Net Delta E[R] against the shared baseline; no edge claim is permitted before prospective evidence.

## Decision 10 — Integration is opt-in and isolated

The V3 detector, scanner runner, source adapter, repository, evaluator, and dedicated systemd unit are independently disableable. The implementation must not touch production direction gates, paper/live execution, cooldown, position limits, frozen V2, or the existing ME Reverse Long V3 experiment.

## Rejected alternatives

1. Modify frozen V2 source semantics. Rejected because frozen V2 is immutable.
2. Reuse V2 source and outcome tables. Rejected because the source cohort would be contaminated.
3. Backfill historical production setups. Rejected because the prospective cohort must begin after activation.
4. Use `reference_price` as an executable fill. Rejected because it is not an executable price.
5. Enable paper/live trading to collect data. Rejected by protocol restrictions and production safety.
6. Combine NEXT_OPEN_5M and ENTRY_ZONE_TOUCH in one paired comparison. Rejected because it would create two incompatible execution populations.
7. Use immediate first-touch MAE exits. Rejected because the approved research proxy is horizon-end conditional and OHLC cannot establish causal first-touch executability.
