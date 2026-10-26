# ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1 — Deployment / Rollback Runbook

Issue #208 follow-up. Companion to
`docs/research/ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1.md`.

> **Nothing in this runbook has been performed.** This document is
> source-controlled preparation only: no deployment, no SQL on the VPS, no
> service start/restart, no activation and no paper/live change was carried out
> while producing it. Every step below is a separate, explicitly approved
> operator action.

Scope of the runtime being deployed: independent, **observe-only** capture of
`MOMENTUM_EXHAUSTION` `SHORT` outcomes for the frozen ME SHORT A geometry —
`capture_source_signal` -> `record_event_lineage` ->
`capture_post_signal_candle` -> `finalize_outcome`. It never places an order
and never routes to paper or live trading.

---

## 0. Preconditions

| # | Precondition | Verified by |
|---|---|---|
| 1 | PR #209 reviewed and merged to `main` | GitHub |
| 2 | `origin/main` contains migration 078 **and** 079 | `git log --oneline -- sql/migrations` |
| 3 | VPS working tree is clean and at `origin/main` | `git status -sb`, `git rev-parse HEAD` |
| 4 | PostgreSQL `trad_bot` reachable, migration 077/078 already applied | `to_regclass(...)` checks below |
| 5 | No other change is in flight for the same services | operator judgement |
| 6 | A maintenance-free window (this deployment touches nothing that trades) | operator judgement |

**Explicitly out of scope forever in this runbook**: changing scanner gates,
the ME SHORT direction gate, paper settings, live settings, or the frozen OOS
outputs. If any step seems to require one of those, stop and escalate.

---

## 1. DEPLOYMENT

> **Mandatory apply order — do not reorder, do not combine:**
>
> ```text
> 078  ->  grants078  ->  079  ->  grants079
> ```
>
> Migration 078 creates the Issue #208 capture schema that migration 079
> depends on, and the grants scripts must run **after** the objects they
> privilege exist (`grants078` after 078, `grants079` after 079) — otherwise
> the `REVOKE`/`GRANT` statements fail on missing relations and the least
> -privilege model is left half-applied. Each step is verified before the next
> one starts. Nothing in this chain is rolled forward automatically.

### 1.1 Update the VPS checkout

```bash
cd /opt/trad_bot
git status -sb          # must be clean
git fetch origin
git switch main
git pull --ff-only origin main
git rev-parse HEAD      # record this hash in the change ticket
```

Do **not** pull over unknown local changes.

### 1.2 Step 1 — apply migration 078 (Issue #208 capture schema)

`sql/migrations/078_me_short_a_forward_only_outcome_lineage_v1.sql` is the
**prerequisite** of everything below: it creates the append-only capture
tables (`me_short_a_source_signal_v1`, `me_short_a_post_signal_candle_v1`,
`me_short_a_event_lineage_v1`, `me_short_a_outcome_observation_v1`), the
activation state machine and the lineage status view.

```bash
sudo -u postgres psql -d trad_bot -X -v ON_ERROR_STOP=1 -1 \
  -f sql/migrations/078_me_short_a_forward_only_outcome_lineage_v1.sql
```

Verify (READ-ONLY) before continuing:

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT to_regclass('research.me_short_a_lineage_activation_v1') AS activation,
          to_regclass('research.me_short_a_event_lineage_v1')     AS lineage,
          to_regclass('research.me_short_a_outcome_observation_v1') AS outcome,
          to_regclass('research.me_short_a_source_signal_v1')     AS source_signal;"
```

Expected: all four non-NULL. If any is NULL, **stop** — 079 will abort.

### 1.3 Step 2 — apply the Issue #208 lineage grants (grants078)

```bash
sudo -u postgres psql -d trad_bot -X -v ON_ERROR_STOP=1 -1 \
  -f sql/grants/me_short_a_forward_outcome_lineage_least_privilege_v1.sql
```

Verify (READ-ONLY):

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT grantee, privilege_type, table_name
     FROM information_schema.role_table_grants
    WHERE table_name LIKE 'me_short_a_%'
    ORDER BY table_name, grantee, privilege_type;"
```

Expected: `trad_bot_me_short_a_collector` = `INSERT` only on the four capture
tables, `trad_bot_me_short_a_audit` = `SELECT` only, and **no** `UPDATE` /
`DELETE` / `TRUNCATE` for anyone. Confirm the append-only triggers exist:

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT tgname, tgrelid::regclass AS tbl FROM pg_trigger
    WHERE NOT tgisinternal
      AND tgrelid::regclass::text LIKE 'research.me_short_a_%'
    ORDER BY tbl, tgname;"
```

### 1.4 Step 3 — apply migration 079 (append-only runtime log + status view)

Run with a role allowed to create objects (e.g. `postgres`), inside one
transaction, and with `ON_ERROR_STOP`:

```bash
sudo -u postgres psql -d trad_bot -X -v ON_ERROR_STOP=1 -1 \
  -f sql/migrations/079_me_short_a_forward_outcome_runtime_integration_v1.sql
```

Preconditions enforced by the script itself: migration 078 objects
(`research.me_short_a_lineage_activation_v1`,
`research.me_short_a_event_lineage_v1`,
`research.me_short_a_outcome_observation_v1`) must already exist, otherwise it
raises and aborts.

Verify (READ-ONLY):

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT to_regclass('research.me_short_a_forward_outcome_runtime_heartbeat_v1') AS heartbeat,
          to_regclass('research.v_me_short_a_forward_outcome_runtime_status_v1') AS status_view;"

sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT tgname FROM pg_trigger
    WHERE tgrelid='research.me_short_a_forward_outcome_runtime_heartbeat_v1'::regclass
      AND NOT tgisinternal;"
```

Expected: both relations non-NULL, and
`trg_me_short_a_runtime_heartbeat_block_mutation` present.

Confirm the migration seeded **no rows** (it must never seed):

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT COUNT(*) FROM research.me_short_a_forward_outcome_runtime_heartbeat_v1;"
```

Expected: `0`.

### 1.5 Step 4 — apply the least-privilege runtime grants (grants079)

```bash
sudo -u postgres psql -d trad_bot -X -v ON_ERROR_STOP=1 -1 \
  -f sql/grants/me_short_a_forward_outcome_runtime_least_privilege_v1.sql
```

Verify (READ-ONLY):

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT grantee, privilege_type, table_name
     FROM information_schema.role_table_grants
    WHERE table_name IN ('me_short_a_forward_outcome_runtime_heartbeat_v1',
                         'me_short_a_fill_feasibility_v1',
                         'me_short_a_lineage_activation_v1')
    ORDER BY table_name, grantee, privilege_type;"
```

Expected:

* `trad_bot_me_short_a_runtime` on the heartbeat table: `INSERT`, `SELECT`
  (and **nothing else**);
* `trad_bot_me_short_a_runtime` on
  `research.me_short_a_fill_feasibility_v1`: **`SELECT` only**;
* `trad_bot_me_short_a_runtime` on
  `research.me_short_a_lineage_activation_v1`: **`SELECT` only**;
* `trad_bot_me_short_a_runtime_audit`: `SELECT` only;
* **no** `UPDATE` / `DELETE` / `TRUNCATE` for anyone on the heartbeat table;
* **no** grant at all on `research.prospective_observation` or
  `research.prospective_outcome` (the closed frozen OOS cohort).

### 1.6 Create the dedicated runtime credentials

Create `/opt/trad_bot/etc/me_short_a_forward_runtime.env` with mode `0600`,
owner `tradbot:tradbot`. It must contain the least-privilege login and the
runtime feature flag, and **nothing else**:

```text
ME_SHORT_A_RUNTIME_DB_HOST=127.0.0.1
ME_SHORT_A_RUNTIME_DB_PORT=5432
ME_SHORT_A_RUNTIME_DB_NAME=trad_bot
ME_SHORT_A_RUNTIME_DB_USER=trad_bot_me_short_a_runtime
ME_SHORT_A_RUNTIME_DB_PASSWORD_FILE=/opt/trad_bot/etc/me_short_a_forward_runtime.dbpass
ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1
```

The shared application `.env` is intentionally **not** loaded by this unit: the
runtime must never reuse scanner, paper or live credentials. The password file
is `0600`, owner `tradbot`.

### 1.7 Install the systemd unit

```bash
sudo install -m 0644 deploy/systemd/trad-bot-me-short-a-forward-outcome.service \
  /etc/systemd/system/trad-bot-me-short-a-forward-outcome.service
sudo systemctl daemon-reload
```

The unit is gated on
`ConditionPathExists=/opt/trad_bot/etc/me_short_a_forward_runtime_enabled`, so
it cannot start before the operator creates that flag file. Do **not**
`systemctl enable` it as part of a routine deploy; enablement is an explicit
operator decision recorded in the change ticket.

### 1.8 Activation checkpoint (separate, explicit approval)

This is the point where evidence starts being recorded. It is **not** part of
the code deployment and must be approved on its own.

1. Confirm the migration 078 activation row is still untouched:

   ```bash
   sudo -u postgres psql -d trad_bot -P pager=off -c \
     "SELECT experiment_id, status, freeze_ts
        FROM research.me_short_a_lineage_activation_v1
       WHERE protocol_version='ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1';"
   ```

   Expected: `READY_TO_START`, `freeze_ts` = `NULL`.

2. Record the operator-approved UTC `freeze_ts` in the change ticket **before**
   writing it.

3. Apply the activation SQL (one row, one transition, immutable afterwards):

   ```sql
   UPDATE research.me_short_a_lineage_activation_v1
      SET status = 'RUNNING',
          freeze_ts = '<OPERATOR-APPROVED-UTC-TIMESTAMP>'
    WHERE protocol_version = 'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'
      AND status = 'READY_TO_START'
      AND freeze_ts IS NULL;
   ```

   The database state machine permits exactly
   `READY_TO_START -> RUNNING` and refuses to change `freeze_ts` afterwards, so
   a retry is safe and a re-freeze is impossible.

4. Create the operator flag file:

   ```bash
   sudo install -o tradbot -g tradbot -m 0600 /dev/null \
     /opt/trad_bot/etc/me_short_a_forward_runtime_enabled
   ```

   This file must never be created automatically.

### 1.9 Start the runtime

```bash
sudo systemctl start trad-bot-me-short-a-forward-outcome
```

---

## 2. POST-DEPLOY health check

A deployment is not successful merely because the unit is `active`.

### 2.1 Service state

```bash
systemctl is-active trad-bot-me-short-a-forward-outcome
journalctl -u trad-bot-me-short-a-forward-outcome -n 50 --no-pager -l
```

Expected: `active`; no `ERROR`/`Traceback` lines; a startup log line containing
`runtime activated: freeze_ts=...`.

### 2.2 Exit-code contract

**Exit 3 is the expected fail-closed outcome** before the activation
checkpoint; it needs operator action, not a restart.

| Exit | Meaning | Action |
|---|---|---|
| `0` | OK / clean stop | none |
| `3` | `EXIT_NOT_RUNNABLE` — capture disabled, activation not `RUNNING`, `freeze_ts` NULL, or dedicated credentials unusable | **operator action required**; the unit will not restart-loop (`RestartPreventExitStatus=3`) |
| `4` | `EXIT_STATE_ERROR` — registry identity mismatch | fix the registry, redeploy |
| `5` | `EXIT_WRITER_ERROR` — the cycle itself failed | inspect journal, then rollback if unresolved |

### 2.3 Functional health (READ-ONLY)

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT runner_id, runner_state, lifecycle_phase, last_heartbeat_at,
          heartbeat_age_seconds, is_stale, collector_activated,
          cycles_completed, candidates_seen, signals_captured,
          lineages_recorded, candles_captured, outcomes_finalized, errors,
          last_error
     FROM research.v_me_short_a_forward_outcome_runtime_status_v1;"
```

Expected: `runner_state = RUNNING`, `is_stale = false`,
`collector_activated = true`, `errors = 0`, and `cycles_completed` increasing
between two samples.

Forward-only check (must stay empty):

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT COUNT(*) AS pre_freeze_evidence
     FROM research.me_short_a_event_lineage_v1 l
     JOIN research.me_short_a_lineage_activation_v1 a
       ON a.protocol_version='ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'
    WHERE l.signal_time <= a.freeze_ts;"
```

### 2.4 Grafana

None is required for this protocol. If a dashboard is added later, follow the
chain `PostgreSQL -> mart/view -> grafana_reader -> datasource -> panel` before
touching any panel definition.

---

## 3. ROLLBACK

Rollback is the deployment chain in reverse, and it must be able to stop
evidence collection at any moment **without destroying anything that was
already captured**. All captured data is append-only and is preserved in
every scenario below.

> **Reverse order of the apply chain:**
>
> ```text
> stop runtime  ->  flags/credentials  ->  grants079  ->  079
>                                                  |
>                                    (078 / grants078: separate approval only)
> ```
>
> Choose the *shallowest* step that fixes the problem. Steps 3.1–3.2 are
> non-destructive and are always safe; 3.3–3.8 remove things and are each
> gated on their own approval.

| Symptom | Rollback depth needed |
|---|---|
| bad data being captured / wrong counters | 3.1 (fast stop) |
| runtime must never start again | 3.1 + 3.3 |
| runtime role is over-privileged | 3.4 |
| 079 schema is wrong | 3.4 + 3.5 |
| the whole integration must be reverted | 3.1 → 3.3 → 3.4 → 3.5 → 3.7 |

### 3.1 Fast stop (preferred, non-destructive)

```bash
sudo rm -f /opt/trad_bot/etc/me_short_a_forward_runtime_enabled
sudo systemctl stop trad-bot-me-short-a-forward-outcome
```

Removing the flag file also prevents an accidental restart. No data is lost:
whatever was captured remains in the append-only tables. **No SQL is
required to stop collection** — this is the primary rollback.

### 3.2 Freeze further capture at the protocol level

If the protocol itself must stop recording permanently, use the terminal
transition (explicitly approved):

```sql
UPDATE research.me_short_a_lineage_activation_v1
   SET status = 'CANCELLED'
 WHERE protocol_version='ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'
   AND status = 'RUNNING';
```

`CANCELLED` is terminal and the state machine refuses further capture. Do
**not** attempt to move `freeze_ts` — it is immutable by design and a failed
attempt is expected.

### 3.3 Remove the runtime artifacts

```bash
sudo systemctl disable --now trad-bot-me-short-a-forward-outcome 2>/dev/null || true
sudo rm -f /etc/systemd/system/trad-bot-me-short-a-forward-outcome.service
sudo rm -f /opt/trad_bot/etc/me_short_a_forward_runtime_enabled
sudo rm -f /opt/trad_bot/etc/me_short_a_forward_runtime.env
sudo rm -f /opt/trad_bot/etc/me_short_a_forward_runtime.dbpass
sudo systemctl daemon-reload
```

### 3.4 Revoke the runtime grants (undo step 4 — `grants079`)

```bash
sudo -u postgres psql -d trad_bot -X -v ON_ERROR_STOP=1 -1 \
  -c "REVOKE ALL ON SCHEMA research FROM trad_bot_me_short_a_runtime;
      REVOKE ALL ON SCHEMA research FROM trad_bot_me_short_a_runtime_audit;"
```

Verify (READ-ONLY) that nothing is left:

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT grantee, table_name, privilege_type
     FROM information_schema.role_table_grants
    WHERE grantee IN ('trad_bot_me_short_a_runtime',
                      'trad_bot_me_short_a_runtime_audit');"
```

Expected: zero rows.

### 3.5 Roll back the 079 schema (undo step 3 — DESTRUCTIVE)

Only if the captured runtime data is genuinely worthless and a separate
destructive approval exists. **Not applied by default.**

```sql
BEGIN;
DROP VIEW IF EXISTS research.v_me_short_a_forward_outcome_runtime_status_v1;
DROP TABLE IF EXISTS research.me_short_a_forward_outcome_runtime_heartbeat_v1;
DROP FUNCTION IF EXISTS research.fn_block_me_short_a_runtime_heartbeat_mutation();
COMMIT;
```

Dropping the heartbeat log destroys the operational audit trail. Re-applying
it later is safe: migration 079 is `CREATE ... IF NOT EXISTS` /
`CREATE OR REPLACE` and can be re-run unchanged.

### 3.6 Application rollback

```bash
cd /opt/trad_bot
git fetch origin
git switch main
git pull --ff-only origin main
git log --oneline -3
```

If the regression is in the runtime code only, roll the checkout back to the
last known-good `main` commit and re-run section 2.

### 3.7 Rolling back 078 / `grants078` (undo steps 1–2) — SEPARATE APPROVAL

These belong to the Issue #208 collector, not to this runtime integration,
and they hold **research evidence**. They are **never** rolled back as part of
a routine runtime rollback.

Only with a separate, explicit approval, and in reverse order (`grants078`
first, then 078):

```sql
BEGIN;
-- grants078: revoke the collector/audit roles first
REVOKE ALL ON SCHEMA research FROM trad_bot_me_short_a_collector;
REVOKE ALL ON SCHEMA research FROM trad_bot_me_short_a_audit;
-- 078
DROP VIEW IF EXISTS research.v_me_short_a_lineage_status_v1;
DROP TABLE IF EXISTS research.me_short_a_outcome_observation_v1;
DROP TABLE IF EXISTS research.me_short_a_post_signal_candle_v1;
DROP TABLE IF EXISTS research.me_short_a_event_lineage_v1;
DROP TABLE IF EXISTS research.me_short_a_source_signal_v1;
DROP TABLE IF EXISTS research.me_short_a_lineage_activation_v1;
COMMIT;
```

**This destroys captured research evidence and the activation/freeze record.**
Confirm with the requester in writing before running it. If in doubt: stop at
3.1 and leave the schema in place.

---

## 4. What this runbook deliberately never does

* No change to `research.me_short_a_fill_feasibility_v1` (Phase 1): no write,
  no `source_observation_ids` retro-fitting.
* No change to `research.prospective_observation` /
  `research.prospective_outcome` — the closed frozen OOS cohort stays closed.
* No scanner change, no scanner-gate change, no ME SHORT direction-gate change.
* No paper or live activation, and no order pathway of any kind.
* **No backfill**: every capture table carries
  `CHECK (signal_time > freeze_ts)`, so historical rows cannot be inserted even
  deliberately. Do not attempt one.
* No Grafana change is required or performed.

---

## 5. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| unit refuses to start, `ConditionFailed` | flag file missing | that is the intended fail-closed state; create it only during the activation checkpoint |
| exit `3` in the journal | capture disabled / activation not `RUNNING` / `freeze_ts` NULL / credentials unusable | check section 1.4 and 1.6; do not restart in a loop |
| `is_stale = true` in the status view | runner stopped or cycling too slowly | `journalctl`, then section 3.1 if it must stop |
| `errors > 0`, `last_error` populated | a single event/cycle failed | the runner keeps going; inspect `last_error`, replay is idempotent |
| `signals_captured` stays `0` | no eligible `MOMENTUM_EXHAUSTION` `SHORT` candidates after the freeze, or `skipped_pre_freeze` is growing | verify `freeze_ts` and the candidate query; both are read-only checks |
| `MISSING_LINEAGE` dominates the status view | no Phase 1 snapshot exists for those `event_key`s | honest state; not an error and never patched by hand |
