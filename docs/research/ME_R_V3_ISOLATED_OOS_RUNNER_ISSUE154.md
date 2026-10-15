# Issue #154 — isolated prospective V3 runner (PR review draft)

**Status: CODE PROPOSED / NOT DEPLOYED / NOT ACTIVATED.**

Scope: separate PostgreSQL database `trad_bot_me_r_v3_oos`. The former
`trad_bot_runner_152_it` integration database and the production `trad_bot`
database are out of scope. Existing scanner, paper and research evaluator
services remain unchanged. No live/paper order execution.

## Deployment gates

1. Review and test this PR, especially SQL validation, PostgreSQL-17 integration,
   closed candle provenance, dedupe, outcome integrity and restart behavior.
2. Merge to `main` and reconcile the VPS checkout **without** overwriting its
   diverged local changes. Do not run `git reset --hard`.
3. Explicitly authorize SQL 071. Verify the connection points to
   `trad_bot_me_r_v3_oos`; migration SQL itself rejects any other DB.
4. Run SQL 071 in the separate database only and verify `market.candle`
   and `dds.instrument` plus grants.
5. Before any capture, obtain distinct operator-approved `FROZEN_AT` and
   prospective `ACTIVATION_AT` values and their runtime SHA-256 fingerprint.
   The immutable-spec hash alone is **not** the runtime fingerprint.
6. Explicitly approve a fixed, reproducible symbol universe in
   `ME_R_V3_OOS_SYMBOLS`. Do not silently follow production scanner filtering.
7. Validate that closed 5m, 15m, 1h timeframes and maximum delay 300 seconds
   are met in **natural** non-synthetic data. On a gap, do not backfill OOS
   market candles or fabricate sources/outcomes.
8. Only then consider installing the service/timer; both are disabled by default.
   The environment file at `/etc/trad-bot/me-r-v3-oos.env` is not created by
   this PR and must remain independently approved.
9. Rollback is `systemctl disable --now trad-bot-me-r-v3-oos.timer`;
   preserve database evidence.

## Data lifecycle

After the approved cutoff, a timer cycle fetches public Bybit candles for an
explicit symbol list. Only a newest completed 5m candle fetched at most
300 seconds after close is eligible for durable storage. The frozen closed
candle detector consumes continuous closed 5m/15m/1h context. Only naturally
observed signals are passed to the existing V3 capture pipeline.
The existing evaluator uses local `dds.instrument` and `market.candle` and
creates linked baseline plus seven paired variants. No historical sources
are injected.

**Known open QA gates:** no live PostgreSQL or Bybit integration tests have
been run as part of this PR. Prove that deduplication and SQL conflict
semantics preserve immutable candle provenance; check partial API failure,
rate-limit, missed intervals, reboots, multi-symbol cycle latency and
no unapproved activation. Do not interpret this draft as OOS approval.
