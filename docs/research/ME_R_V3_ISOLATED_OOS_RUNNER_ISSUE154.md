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


## PR #159 final preflight checkpoint (2026-10-16)

Validation evidence from disposable `trad_bot_me_r_v3_oos_pg_test`:
SQL 068--072, five research tables, local market tables and least-privilege
grants PASS; regression 210 passed / 3 skipped; PG integration 2 passed;
one-shot real atomic PostgreSQL E2E 1 passed (one committed source and
seven variants, subsequent uncommitted candle + source tuple rolled back).
The disposable database is now deliberately non-empty; never rerun the
one-shot integration or delete immutable rows to make it pass.

**Authoritative offline protocol approval (#151):**
`FROZEN_AT=2026-10-16T12:00:00Z`;
`ACTIVATION_AT=2026-10-19T12:00:00Z`;
runtime SHA-256
`ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a`.
These approve boundaries/fingerprint only, **not** systemd activation.
Activation timestamp is an inclusion cutoff, not an auto-launch schedule.
No retrospective signals or backfill, including delayed deployment.

For natural Bybit data preflight only, use the isolated Linux OS account,
an explicitly frozen `ME_R_V3_OOS_SYMBOLS` universe and a PR checkout.
Run `python -m app.research.me_r_long_early_mae_v3.isolated_oos
--market-preflight` immediately after a 5-minute boundary. The command
fetches public 5m/15m/1h market data, checks closed/gapless source
contexts and source delay <=300s, and **does not connect to PostgreSQL**.
A synthetic/unit test PASS does not substitute for a natural API PASS.
Keep service/timer disabled during this test.

Systemd preflight: `systemd-analyze verify <absolute service path>
<absolute timer path>` and `systemd-analyze calendar
'*-*-* *:0/5:30 UTC'`. Service runs as
`trad_bot_me_r_v3_oos`, requires a future reviewed merge of PR #159
into the deployed checkout and an independently approved environment file.
Never install the production units from the detached PR worktree or
start the timer before separate operator authorization.

Current status: DB_ATOMICITY_PASS /
NATURAL_BYBIT_PREFLIGHT_PENDING /
SYSTEMD_VPS_SYNTAX_PENDING /
PRODUCTION_ACTIVATION_NO_GO.
