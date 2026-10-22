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

## 2. Post-apply verification (read-only) — MANDATORY, STOP ON MISMATCH

These checks are NOT optional: PostgreSQL `GRANT`/`REVOKE` issued by a role
without the required rights can silently no-op, so the only proof is the
EFFECTIVE privilege state. Every statement below returns rows of the shape
`check | expected | actual` — **for EVERY row `actual` must equal `expected`**.
On ANY mismatch or error: STOP — do not provision secrets, do not enable the
unit, do not proceed to activation. Record the output in the change log.

Notes:
* PUBLIC is a pseudo-role, not a role: `has_*_privilege('PUBLIC', ...)` raises
  an error and must never be used. PUBLIC grants are counted via
  `aclexplode()` with `grantee = 0`, over `COALESCE(acl, acldefault(...))` so
  default privileges (e.g. function EXECUTE for PUBLIC) are counted too.
* `trad_bot` rows are guarded with `CASE WHEN EXISTS(pg_roles ...)`: a missing
  role yields `false` (no privileges) and never an SQL error.
* Function checks match by `proname`, covering ALL overloads.
* This block is executed automatically by
  `tests/test_htf_baseline_short_phase2_db_security_v1.py`; keep the
  `VERIFICATION_SQL_*` markers and the `check|expected|actual` shape intact.

```sql
-- VERIFICATION_SQL_BEGIN
SELECT 'obs_sel' AS check, true AS expected,
       has_table_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_observation', 'SELECT') AS actual;
SELECT 'obs_ins' AS check, false AS expected,
       has_table_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_observation', 'INSERT') AS actual;
SELECT 'obs_upd_exp' AS check, false AS expected,
       has_table_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_experiment', 'UPDATE') AS actual;
SELECT 'obs_reg_ins' AS check, false AS expected,
       has_table_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_freeze_registry', 'INSERT') AS actual;
SELECT 'obs_fn_ins' AS check, true AS expected,
       (SELECT count(*) > 0 AND COALESCE(bool_and(has_function_privilege('trad_bot_htf_p2_observer', p.oid, 'EXECUTE')), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research' AND p.proname = 'htf_baseline_short_phase2_insert_observation') AS actual;
SELECT 'obs_fn_act' AS check, false AS expected,
       (SELECT count(*) > 0 AND COALESCE(bool_and(has_function_privilege('trad_bot_htf_p2_observer', p.oid, 'EXECUTE')), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research' AND p.proname = 'htf_baseline_short_phase2_activate') AS actual;
SELECT 'adm_sel' AS check, true AS expected,
       has_table_privilege('trad_bot_htf_p2_admin', 'research.htf_baseline_short_phase2_experiment', 'SELECT') AS actual;
SELECT 'adm_ins' AS check, false AS expected,
       has_table_privilege('trad_bot_htf_p2_admin', 'research.htf_baseline_short_phase2_observation', 'INSERT') AS actual;
SELECT 'adm_fn_act' AS check, true AS expected,
       (SELECT count(*) > 0 AND COALESCE(bool_and(has_function_privilege('trad_bot_htf_p2_admin', p.oid, 'EXECUTE')), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research' AND p.proname = 'htf_baseline_short_phase2_activate') AS actual;
SELECT 'adm_fn_ins' AS check, false AS expected,
       (SELECT count(*) > 0 AND COALESCE(bool_and(has_function_privilege('trad_bot_htf_p2_admin', p.oid, 'EXECUTE')), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research' AND p.proname = 'htf_baseline_short_phase2_insert_observation') AS actual;
SELECT 'tb_ins' AS check, false AS expected,
       CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot')
            THEN has_table_privilege('trad_bot', 'research.htf_baseline_short_phase2_observation', 'INSERT')
            ELSE false END AS actual;
SELECT 'tb_sel' AS check, false AS expected,
       CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot')
            THEN has_table_privilege('trad_bot', 'research.htf_baseline_short_phase2_observation', 'SELECT')
            ELSE false END AS actual;
SELECT 'pub_tbl' AS check, 0 AS expected,
       (SELECT count(*)
          FROM pg_class c,
               aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a
         WHERE c.relnamespace = 'research'::regnamespace
           AND c.relname IN ('htf_baseline_short_phase2_experiment',
                             'htf_baseline_short_phase2_observation',
                             'htf_baseline_short_phase2_freeze_registry')
           AND a.grantee = 0) AS actual;
SELECT 'pub_fn' AS check, 0 AS expected,
       (SELECT count(*)
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace,
               aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
         WHERE n.nspname = 'research'
           AND p.proname IN ('htf_baseline_short_phase2_insert_observation',
                             'htf_baseline_short_phase2_activate',
                             'htf_baseline_short_phase2_experiment_guard',
                             'htf_baseline_short_phase2_observation_guard',
                             'htf_baseline_short_phase2_freeze_registry_guard')
           AND a.grantee = 0) AS actual;
SELECT 'definer_fn' AS check, true AS expected,
       (SELECT count(DISTINCT p.proname) = 2 AND COALESCE(bool_and(p.prosecdef), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research'
           AND p.proname IN ('htf_baseline_short_phase2_insert_observation',
                             'htf_baseline_short_phase2_activate')) AS actual;
SELECT 'pinned_search_path' AS check, true AS expected,
       (SELECT count(DISTINCT p.proname) = 2
          AND COALESCE(bool_and('search_path=pg_catalog, research' = ANY (p.proconfig)), false)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'research'
           AND p.proname IN ('htf_baseline_short_phase2_insert_observation',
                             'htf_baseline_short_phase2_activate')) AS actual;
SELECT 'leftover_dml' AS check, 0 AS expected,
       (SELECT count(*)
          FROM pg_class c,
               aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a
         WHERE c.relnamespace = 'research'::regnamespace
           AND c.relname IN ('htf_baseline_short_phase2_experiment',
                             'htf_baseline_short_phase2_observation',
                             'htf_baseline_short_phase2_freeze_registry')
           AND a.grantee <> c.relowner
           AND a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')) AS actual;
SELECT 'seq_observer' AS check, false AS expected, x.actual AS actual FROM (
  SELECT has_sequence_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'USAGE') AS actual
  UNION ALL
  SELECT has_sequence_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'SELECT')
  UNION ALL
  SELECT has_sequence_privilege('trad_bot_htf_p2_observer', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'UPDATE')
) x;
SELECT 'seq_admin' AS check, false AS expected, x.actual AS actual FROM (
  SELECT has_sequence_privilege('trad_bot_htf_p2_admin', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'USAGE') AS actual
  UNION ALL
  SELECT has_sequence_privilege('trad_bot_htf_p2_admin', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'SELECT')
  UNION ALL
  SELECT has_sequence_privilege('trad_bot_htf_p2_admin', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'UPDATE')
) x;
SELECT 'seq_trad_bot' AS check, false AS expected, x.actual AS actual FROM (
  SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot')
              THEN has_sequence_privilege('trad_bot', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'USAGE')
              ELSE false END AS actual
  UNION ALL
  SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot')
              THEN has_sequence_privilege('trad_bot', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'SELECT')
              ELSE false END
  UNION ALL
  SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot')
              THEN has_sequence_privilege('trad_bot', 'research.htf_baseline_short_phase2_observation_observation_id_seq', 'UPDATE')
              ELSE false END
) x;
SELECT 'seq_pub' AS check, 0 AS expected,
       (SELECT count(*)
          FROM pg_class c,
               aclexplode(COALESCE(c.relacl, acldefault('S', c.relowner))) a
         WHERE c.relnamespace = 'research'::regnamespace
           AND c.relkind = 'S'
           AND c.relname = 'htf_baseline_short_phase2_observation_observation_id_seq'
           AND a.grantee = 0) AS actual;
-- VERIFICATION_SQL_END
```

**STOP contract:** any value differing from the expected comment above means
the migration did not take effect as intended (wrong applying role is the
most likely cause). Re-apply 075 as the table owner (`postgres`) and re-run
this section; never fall back to manual GRANT/REVOKE surgery in production.

Earlier structural checks (object list, role attributes, ACL diff of
pre-existing research objects) remain useful and stay as written below.

```sql
-- 1. objects
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'research' AND table_name LIKE 'htf_baseline_short_phase2%';

-- 2. roles
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole
  FROM pg_roles WHERE rolname LIKE 'trad_bot_htf_p2%';
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
