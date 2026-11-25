# Issue #312 — H2 Prospective Runtime: Safe Deployment Runbook

**Status:** `IMPLEMENTATION_READY / ACTIVATION_HOLD`.
Nothing in this runbook may be executed without a separate explicit GO for
each stage.  Every stage is designed to be reversible and to leave production
untouched.

## Stage 0 — Preconditions (read-only verification)

1. Confirm #305 capture state on the VPS (READ-ONLY):
   `python scripts/me_upstream_source_v1_admin.py show`
   — note `activation` status, `freeze_ts`, `source_event` count.
2. Confirm the 167 H2 tests (PR #311) pass on the release commit.
3. Confirm the new suites pass:
   `pytest tests -k "h2_prospective"`.
4. Decide and record the candle API budget (FR-02): max requests/cycle and
   per hour.  **Without a confirmed budget the provider stays OFF** — this
   runbook assumes `H2_CANDLE_API_BUDGET_CONFIRMED=0` until Stage 3.

## Stage 1 — Database migration (separate GO)

1. Take a backup / snapshot of the research database.
2. Apply `sql/migrations/095_me_entry_quality_h2_prospective_v1.sql`
   (idempotent `CREATE ... IF NOT EXISTS`; creates ONLY the
   `research_me_entry_quality_h2_prospective` schema).
3. Optionally provision a dedicated role via
   `deploy/sql/312_h2_prospective_role.sql` (least privilege: SELECT on the
   #305 `source_event` table + full DML on its own schema only).
4. Verify: the migration touched no `dds.*` / `research_me_upstream_coverage`
   / trading table (schema diff).

**Rollback:** `DROP SCHEMA research_me_entry_quality_h2_prospective CASCADE;`

## Stage 2 — Deploy disabled (separate GO)

1. Ship the release to a NEW directory (e.g.
   `/opt/trad_bot_h2_prospective_release`) — never in-place on the scanner
   release.
2. Copy `deploy/env/h2-prospective-runtime.env.example` to
   `deploy/env/h2-prospective-runtime.env` with
   `H2_PROSPECTIVE_ENABLED=0` and `H2_CANDLE_API_BUDGET_CONFIRMED=0`.
3. Install `deploy/systemd/trad-bot-h2-prospective-runtime.service` but do
   NOT enable it.  `systemctl start` with the flag off must exit with
   `SuccessExitStatus=3` and perform no connection/request/write.
4. Verify scanner / paper / V3 / OOS-2 services are untouched:
   `systemctl status trad-bot-scanner ...` and their logs show no change.

**Rollback:** `systemctl stop` + remove the unit; nothing else was changed.

## Stage 3 — Enable the runtime WITHOUT the candle source (separate GO)

1. Set `H2_PROSPECTIVE_ENABLED=1`, keep `H2_CANDLE_API_BUDGET_CONFIRMED=0`.
2. `systemctl start trad-bot-h2-prospective-runtime`.
3. Verify:
   * `SOURCE_EMPTY_WAITING` (expected: the #305 cohort currently has 0
     events — this is NOT an RSI-algorithm error and NOT proof of source
     coverage);
   * runtime status `BLOCKED_CANDLE_SOURCE` in `runtime_health`;
   * zero Bybit requests (no API budget confirmed);
   * scanner services and their request rates are unchanged.
4. Let it accumulate source events; monitor `runtime_health` counters.

## Stage 4 — Enable the bounded candle source (separate GO, only if needed)

1. Confirm the research API budget with whoever owns the Bybit account rate
   limits; write the agreed numbers into the env file.
2. Set `H2_CANDLE_API_BUDGET_CONFIRMED=1`.
3. Restart the unit; verify:
   * request counters stay within `H2_CANDLE_MAX_REQUESTS_PER_HOUR`;
   * `PENDING_CONFIRMATION -> CONFIRMED_DELAYED / EXPIRED` transitions appear
     only from complete closed-candle windows;
   * `evaluation_log` shows no `EXPIRED` from incomplete data;
   * production scanner latency / rate-limit telemetry unchanged.

**Rollback:** `H2_CANDLE_API_BUDGET_CONFIRMED=0` + restart (provider off;
ingest continues).

## Stage 5 — Freeze & activation (OUT OF SCOPE here, separate decision)

Only after (a) #305 shows real source coverage, (b) the candle source is
approved, (c) `312_h2_entry_outcome_policy_v1.md` D-1..D-12 are decided:

1. Freeze the immutable policy fingerprint(s) and pin them via
   `H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS`.
2. Independent review of the accumulated cohort.
3. Separate GO for any activation/merge/production use.

## Emergency stop

`systemctl stop trad-bot-h2-prospective-runtime` — the runtime holds no
positions, opens no orders and never writes outside its own research schema,
so stopping it can never affect trading.  Its state survives restarts
(`variant_state` / `variant_result` / `reader_watermark`).
