# HTF BASELINE SHORT Phase-2 — Migration 074 → 075 Apply Runbook (V1)

Issue: #176 Phase 2 — DB privilege and runtime hardening (CP1/CP6).
Status of the target environment (VPS `/opt/trad_bot`, PostgreSQL `trad_bot`)
at authoring time: migration 074 **not applied**, Phase-2 observer **not
installed/running**. Nothing in this runbook has been executed against
production — it is a procedure awaiting independent review and an explicit
operator go.

`PHASE_2_NO_GO` remains in force. Applying 074/075 does NOT activate Phase 2
and must never be combined with activation steps.

## 0. Preconditions

* maintenance window approval for `trad_bot` DB (schema-only change);
* DBA access to the `trad_bot` database as the owner/superuser role
  (`postgres`) — role creation in 075 requires it;
* a current verified base backup (see Rollback);
* working tree on VPS at the merged commit containing this branch;
* scanner/paper/evaluator unaffected: this procedure touches only
  `research.htf_baseline_short_phase2_*` objects and two NEW roles.

## 1. Apply order (strict: 074 first, then 075)

Both files are single-transaction safe; apply each with a single transaction
and stop on error:

```bash
cd /opt/trad_bot
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 --single-transaction \
  -f sql/migrations/074_htf_baseline_short_phase2_registration.sql
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 --single-transaction \
  -f sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql
```

* 074 is idempotent (`CREATE … IF NOT EXISTS`, guarded DO blocks).
* 075 is ONE DO block → atomic even under autocommit and **fail-closed**: it
  raises `P2_MIGRATION_074_REQUIRED` when 074 objects are missing and
  `P2_*_ROLE_ATTRS_UNSAFE` when an existing role carries superuser/CREATEDB/
  CREATEROLE/LOGIN attributes that contradict the least-privilege contract.
  A failed 075 leaves the database exactly as it was.
* 075 is re-runnable: grants/revokes are idempotent.

## 2. Post-apply verification (read-only)

```sql
-- 1. objects
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'research' AND table_name LIKE 'htf_baseline_short_phase2%';

-- 2. roles
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole
  FROM pg_roles WHERE rolname LIKE 'trad_bot_htf_p2%';

-- 3. no direct DML left for application roles on Phase-2 tables
SELECT grantee, table_name, privilege_type
  FROM information_schema.table_privileges
 WHERE table_schema = 'research'
   AND table_name LIKE 'htf_baseline_short_phase2%'
   AND privilege_type IN ('INSERT','UPDATE','DELETE')
 ORDER BY 1,2,3;
-- expected: only the owner (postgres)

-- 4. activation function is not PUBLIC
SELECT proacl FROM pg_proc
 WHERE proname = 'htf_baseline_short_phase2_activate';
-- expected: no PUBLIC EXECUTE entry
```

Then provision credentials per
`docs/research/HTF_BASELINE_SHORT_PHASE2_DB_SECRETS_V1.md`.

## 3. Graceful enablement (separate, later, requires its own go)

```bash
install -o tradbot -g tradbot -m 0700 -d /opt/trad_bot/var/htf_phase2
install -o tradbot -g tradbot -m 0700 -d /opt/trad_bot/var/htf_phase2/candles
systemd-analyze verify deploy/systemd/trad-bot-htf-baseline-short-observer.service
# unit stays disabled; no enable/start without the full activation runbook
```

`systemd-analyze verify` runs on the VPS (not available in the Windows dev
environment) and is a no-op until the activation sequence is approved.

## 4. Rollback / recovery plan

Rollback of 075 (privilege layer only — safe, no data loss):

```sql
BEGIN;
DROP OWNED BY trad_bot_htf_p2_observer;
DROP OWNED BY trad_bot_htf_p2_admin;
ROLLBACK;  -- preview first; COMMIT to execute
```

`DROP OWNED BY` removes every grant of the roles in this database; afterwards
the roles can be dropped cluster-wide (`DROP ROLE`) if no other database uses
them. Because 075 only REVOKEd rights from existing roles and added a
SECURITY DEFINER layer, rolling it back restores the pre-075 privilege state
by construction (the grants it removed were re-created only by 075's absence —
if a strict rollback to the 074-only state is required, re-apply the default
privileges that existed, i.e. simply drop and re-run 074 in a scratch DB to
diff ACLs).

Rollback of 074 (tables + functions): DROP the three Phase-2 tables (they are
empty unless the observer ran, which is out of scope here) and the two
functions. Use only with DBA approval:

```sql
BEGIN;
DROP TABLE IF EXISTS research.htf_baseline_short_phase2_observation;
DROP TABLE IF EXISTS research.htf_baseline_short_phase2_freeze_registry;
DROP TABLE IF EXISTS research.htf_baseline_short_phase2_experiment;
DROP FUNCTION IF EXISTS research.htf_baseline_short_phase2_insert_observation(text,text,text,text,text,timestamp with time zone,timestamp with time zone,timestamp with time zone,numeric,numeric,boolean,boolean,jsonb);
DROP FUNCTION IF EXISTS research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text);
COMMIT;
```

Point-B tables (`research.prospective_*`) are never involved — recovery of the
frozen OOS stack is unaffected by any of the above.

Corrupt-state recovery (observer side): see runbook §9 of the activation
runbook (checkpoint move-aside + replay is at-least-once/idempotent).

## 5. Remaining activation blockers (unchanged by this hardening)

1. live market-data source missing (CSV-only; Bybit integration pending);
2. 6 protocol operator approvals outstanding; `APPROVED_FREEZE_TS = None`;
   protocol not frozen (`PROTOCOL_NOT_FROZEN`);
3. production application/verification of 074 → 075 (this runbook, separate go);
4. dedicated credentials + `etc/htf_phase2_observer.env` provisioning;
5. activation config + freeze registry claim + flag file — the explicit
   operator activation sequence (never automated).

`MIGRATIONS_APPLIED_TO_PRODUCTION = NO` as of this document.
