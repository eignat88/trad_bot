# Architecture Decision Record — ME_R_LONG Early MAE Exit V2

## Decision

Implement `ME_R_LONG_EARLY_MAE_EXIT_OOS_V2` as an autonomous prospective research pipeline in the independent `research_me_early_mae_v2` schema.

The pipeline captures natural `MOMENTUM_EXHAUSTION_REVERSE_LONG_V1` LONG candidates from `dds.scanner_setup` after a fixed activation boundary, creates deterministic virtual fills, evaluates seven frozen early-exit variants, and persists paired baseline/variant outcomes.

The existing paper-coupled V1 observer and `dds.me_r_long_early_exit_observation` remain unchanged.

## Context

The V1 direction is BLOCKED in the production paper direction gate, so `paper_runner.py` never opens a matching paper trade. The V1 scanner nevertheless continues producing valid LONG candidates in `dds.scanner_setup`.

The previous V1 observation design is coupled to `dds.paper_trade` through `trade_id`; therefore the blocked direction produces no data. This violates the research requirement for a source independent from paper execution.

## Consequences

### Positive

- Natural post-freeze candidates are captured independently of paper gates.
- Exactly seven variants are evaluated for each eligible virtual fill.
- Virtual execution uses deterministic boundary prices and completed candles.
- Restart recovery and idempotent replay are supported by transactional SQL.
- No paper/live balances, positions or exits are touched.

### Negative / constraints

- Within-candle SL/TP order is not observable from 5m OHLC. The frozen conservative `STOP_FIRST` rule is applied and ambiguity is persisted.
- A missing or duplicated 5m candle blocks finalization for that event.
- Runtime wiring remains explicit and requires deployment review.
- Natural capture cannot be considered proven until the first post-deployment event and outcome pipeline are observed.

## Alternatives rejected

1. **Reuse V1 paper-coupled table.** Rejected because blocked V1 LONG never produces paper trades, and the table has a mandatory FK to `dds.paper_trade`.
2. **Backfill historical setups.** Rejected because the clean prospective cohort must contain no pre-freeze events.
3. **Use `reference_price` as fill.** Rejected because it is an informational reference, not an executable price.
4. **Modify the V1 scanner to emit a research marker.** Rejected because production scanner detection must remain unchanged.
5. **Enable V1 paper execution to collect data.** Rejected because the task explicitly forbids enabling paper/live execution.

## Review boundary

The pipeline is research-only. It must not be deployed with any change to:

- scanner detection;
- direction gates;
- paper/live engines;
- production balances or risk controls.
