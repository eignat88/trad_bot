# ME_R_LONG Early MAE V2 — Deployment Verification Report

**Status:** `NOT DEPLOYED / MERGE BLOCKED / CAPTURE NOT ACTIVATED`; natural capture has not been validated.

## Verification scope

This report records the required checks for the prospective V2 pipeline. It does not claim VPS deployment until the reviewed merge, migration and service verification are completed.

## Required checks

| Check | Result |
|---|---|
| V1 scanner detection unchanged | PASS (static source verification) |
| V1 LONG paper direction remains BLOCKED | PASS (configuration preserved) |
| Independent V2 research schema | PASS (migration 064) |
| No mandatory `dds.paper_trade` FK | PASS |
| Frozen protocol version and seven variants | PASS |
| Source idempotency | PASS |
| Baseline/variant fee-slippage parity | PASS (shared frozen cost function) |
| Missing/duplicate/incomplete candle fail-closed | PASS (unit tests) |
| No historical/pre-freeze clean cohort rows | PASS (activation guard) |
| Paper/live isolation | PASS (unit/static tests) |

## Deployment status

`NOT DEPLOYED` until all of the following are independently completed and reviewed:

1. Merge the feature branch to `main`.
2. Apply `sql/migrations/066_me_long_early_mae_exit_prospective_v2.sql` to production PostgreSQL.
3. Deploy the standalone research runner with a dedicated PostgreSQL role and connection.
4. Verify the first natural post-freeze source event and its seven variants.
5. Confirm no paper/live side effects through VPS audit.
6. Update status to `RUNNING / CLEAN_OOS_ACCUMULATION` only after confirmed natural outcome pipeline.
