# H1-E — Deploy Runbook (Issue #324) — PROPOSAL, requires owner GOs

Research only. Every step below is executed by an OPERATOR on `trad-bot-01`;
the assistant performs none of them. **Stop-on-error: on ANY non-zero exit or
unexpected output, STOP and go back to the last checkpoint. No step is
retried blindly.**

## Preconditions (verify, read-only)

| # | Check | Expected | On mismatch |
|---|---|---|---|
| P1 | `git -C /opt/... rev-parse HEAD` | matches the approved release SHA | STOP |
| P2 | `psql -c "SELECT status FROM research_me_upstream_coverage.source_event LIMIT 0"` | reachable | STOP |
| P3 | `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='research_me_entry_quality_h1e'` | `0` (unless re-deploying) | STOP, go to Recovery |
| P4 | untracked `.env.pre_*` files | present, untouched | STOP if altered |

**Checkpoint 0** = P1–P4 recorded in the change log.

## Step 1 — deploy (GO #1: DEPLOY)

1. Copy `deploy/systemd/trad-bot-h1e-observer.service` → `/etc/systemd/system/`.
2. Copy `deploy/env/h1e-observer.env.example` → `deploy/env/h1e-observer.env`
   (NEVER touch existing env files; set nothing yet — all gates stay 0).
3. `systemctl daemon-reload`; `systemctl start trad-bot-h1e-observer`.
4. Expected: exit code `3` (`disabled / not activated`), no SQL, no API.
   Verify: `journalctl -u trad-bot-h1e-observer -n 5` shows the DISABLED line.

**Checkpoint 1** = unit installed, worker disabled, exit 3 observed. GO #1 recorded.

## Step 2 — DDL + role (GO #2: DDL)

1. Backup first: `pg_dump --schema=research_me_upstream_coverage --file=pre324_<date>.sql`.
2. Apply `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` (ONE transaction).
3. Apply `deploy/sql/324_h1e_observer_role.sql` (ONE transaction).
4. Verify (read-only):
   - 10 tables in `research_me_entry_quality_h1e`;
   - `SELECT 1 FROM research_me_entry_quality_h1e.h1e_event LIMIT 0` works
     as `tradbot_h1e_observer`;
   - observer role CANNOT write `h1e_activation` (expected permission denied).
5. On ANY error: run the rollback/recovery guidance in
   `sql/proposals/324_h1e_prospective_ledger_v1_rollback.sql` and STOP.

**Checkpoint 2** = DDL applied, role verified least-privilege. GO #2 recorded.

## Step 3 — H1-E freeze (GO #3: FREEZE)

1. Decide the freeze instant `T` (strictly AFTER the #305 freeze
   `2026-11-24 13:31:53.365952+00:00`; prospective only).
2. Mint, in the administrative contour ONLY:
   `freeze_ts = T`, `protocol_fingerprint` (from the frozen protocol module),
   `approval_ref`, `approved_by`.
3. Record the four values in the change log. NEVER pre-declare them; once
   frozen, the activation row is immutable (DB trigger enforces).

**Checkpoint 3** = freeze values recorded. GO #3 recorded.

## Step 4 — activation (GO #4: ACTIVATE)

1. Insert the `h1e_activation` row (`status='APPROVED'`) via the
   administrative contour (the observer role cannot).
2. Populate `deploy/env/h1e-observer.env`: `H1E_PG_DSN` (observer role DSN),
   `H1E_ACTIVATION_ID`, `H1E_FREEZE_TS_US`, `H1E_PROTOCOL_FINGERPRINT`,
   `H1E_APPROVAL_REF`, `H1E_APPROVED_BY`, then `H1E_DDL_APPLIED=1`,
   `H1E_DEPLOY_GATE=1`, and ONLY LAST `H1E_WORKER_ENABLE=1`.
3. `systemctl restart trad-bot-h1e-observer`; `systemctl status` → `active (running)`.
4. `... -m app.research.me_entry_quality_h1e_infra_v1.worker --health` → `h1e health OK`.
5. Watch the first cycles: bounded batch sizes, checkpoint advancing,
   pending queue empty or bounded; `journalctl -f -u trad-bot-h1e-observer`.

**Checkpoint 4** = worker running, health OK, first cycles bounded. GO #4 recorded.

## Post-deploy verification (read-only, 24h)

- classification rows grow only for `signal_time > freeze` (DB enforces);
- `h1e_pending_retry` stays bounded; every deferred row has `attempt_count <= max_attempts`;
- no writes appear outside `research_me_entry_quality_h1e` (audit via pg_stat_activity sampling);
- existing services: their units/processes show no restarts caused by H1-E.

## Recovery / stop procedures

| Symptom | Action |
|---|---|
| worker crash-loop | `systemctl stop`; inspect journal; state is durable (checkpoint + pending); safe to resume |
| wrong freeze minted | STOP before activation; a minted freeze is immutable — new experiment id required |
| DDL failure mid-apply | transaction rolled back; re-run the proposal after fixing the cause |
| rollback wanted | `pg_dump` the H1-E schema (evidence), then the rollback script (owner GO) |
| duplicate delivery suspected | no action: exactly-once effect is enforced (unique identity + idempotent journal) |

## Explicit non-actions (all require separate GOs)

No `git pull`/deploy beyond the steps above; no DDL to any working database
beyond Step 2; no H1-E freeze minting without GO #3; no observer start without
GO #4; no restarts of existing services; no historical backfill; no OOS
transition (`H1_OOS_HOLD`).
