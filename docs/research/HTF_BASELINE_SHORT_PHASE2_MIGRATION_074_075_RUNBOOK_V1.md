# HTF BASELINE SHORT Phase-2 — Migration 074 → 075 Apply Runbook (V1)

Issue: #176 Phase 2 — DB privilege and runtime hardening (CP1/CP6).
Status of the target environment (VPS `/opt/trad_bot`, PostgreSQL `trad_bot`)
at authoring time: migration 074 **not applied**, Phase-2 observer **not
installed/running**. Nothing in this runbook has been executed against
production — it is a procedure awaiting independent review and an explicit
operator go.

`PHASE_2_NO_GO` remains in force. Applying 074/075 does NOT activate Phase 2
and must never be combined with activation steps.

## 0. Preflight (all items mandatory, stop on any failure)

### 0.1 Approvals and scope

* separate, explicit operator approval for the **075 application window** and —
  required again, independently — for any **rollback** (§4). Neither is
  covered by a code review, a CI run, or by issue #185;
* `PHASE_2_NO_GO` still applies to activation: applying 074/075 never
  activates Phase 2 and must never be combined with activation steps;
* scanner/paper/evaluator are NOT restarted, the Phase-2 observer is NOT
  started or installed.

### 0.2 Environment identity checks

```bash
cd /opt/trad_bot
git rev-parse HEAD                  # must equal the reviewed/merged commit
git status --sb                     # must be clean
git rev-parse origin/main           # record; the tree must match it

sudo -u postgres psql -d trad_bot -P pager=off -c \
  "SELECT current_database(), current_user, version(), now()"
```

* PostgreSQL must be 17 (`server_version_num >= 170000`) — the ACL snapshot
  format and `MAINTAIN` privilege model require it;
* `current_database()` must be `trad_bot`, `current_user` must be the
  owner/superuser role (`postgres`) — 075 role creation requires it.

### 0.3 Backup requirement

* a current, verified base backup of the PostgreSQL cluster taken **after**
  074 and **before** 075 (see §1), with its checksum/verification result
  recorded in the change log;
* the backup is the last-resort recovery path. The normal rollback path (§4)
  is the snapshot-driven privilege restore, which loses nothing.

### 0.4 Pre-075 ACL snapshot (mandatory, checksum-verified)

The pre-075 snapshot is the ONLY artifact that can restore the privileges 075
revokes. `DROP OWNED BY` cannot re-create grants it never held (see §4).
Snapshot tool (read-only; never reads `pg_authid`, never prints secrets):

```bash
cd /opt/trad_bot
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
  snapshot --dbname trad_bot \
  --output var/htf_phase2/pre075_acl_snapshot.json
# writes var/htf_phase2/pre075_acl_snapshot.json + .sha256 sidecar
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
  verify --snapshot var/htf_phase2/pre075_acl_snapshot.json
```

* `verify` MUST print `OK` with matching file and payload SHA-256; on ANY
  mismatch/missing sidecar: STOP.
* Store the snapshot and its sidecar together with the backup metadata
  (retention: at least until 075 is re-applied from scratch and re-verified).

### 0.5 Strict order

```text
074  ->  pre-075 ACL snapshot + checksum  ->  075  ->  post-apply verification (§2)
```

The snapshot MUST be taken after 074 and before 075: 075 is exactly the delta
this snapshot is taken against, and the rollback engine FAILS CLOSED on any
unexplained drift from it.

## 1. Apply order (strict: 074 first, then snapshot, then 075)

Each file is single-transaction safe; apply with `--single-transaction` and
stop on error:

```bash
cd /opt/trad_bot
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 --single-transaction \
  -f sql/migrations/074_htf_baseline_short_phase2_registration.sql
# ... then the snapshot of §0.4, verify it, and only then:
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 --single-transaction \
  -f sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql
```

* 074 is idempotent (`CREATE … IF NOT EXISTS`, guarded DO blocks). Re-running
  074 is NEVER a substitute for restoring privileges: it does not re-create
  grants removed by 075.
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

> **Corrected in issue #185.** The previous version of this section claimed
> that `DROP OWNED BY trad_bot_htf_p2_observer / _admin` restores the
> privileges that 075 revoked. **That claim was wrong**: `REVOKE` deletes
> privilege tuples, and no command — including `DROP OWNED BY` — can re-create
> grants it never held. `DROP OWNED BY` is additionally destructive: it drops
> every object owned by the role and revokes every privilege the role holds,
> including pre-existing non-Phase-2 objects. It must never be used for this
> recovery. The only reliable restore source is the pre-075 ACL snapshot of
> §0.4, applied by the fail-closed rollback engine below.

### 4.1 Rollback triggers (any one is sufficient)

* §2 post-apply verification returns ANY row where `actual != expected`;
* the effective ACL diff against the pre-075 snapshot shows grants outside the
  audited 075 delta (see
  `docs/research/HTF_BASELINE_SHORT_PHASE2_MIGRATION_075_PRIVILEGE_DELTA_AUDIT_V1.md`);
* unexpected `pg_shdepend`/`pg_auth_members` dependencies appear for the two
  P2 roles (privileges on unrelated objects, object ownership, role
  memberships, dependencies in other databases);
* the audit/verification tooling reports snapshot checksum or object-identity
  mismatch;
* any security review finding against the 075 privilege model.

Rolling back is a normal, safe operation: it restores privileges and function
settings only, in a single transaction, and never deletes Phase-2 data.

### 4.2 Preconditions and STOP conditions (fail closed)

STOP — do not run the rollback — when ANY of the following holds:

1. no verified pre-075 snapshot + `.sha256` sidecar (§0.4) is available;
2. `verify` reports a file/payload checksum mismatch or the sidecar is missing;
3. the snapshot names a different database than the one connected;
4. the state differs from the snapshot in ways the audited 075 delta cannot
   explain (`P2_RB_CONFLICTING_STATE`) — including object-identity drift
   (`md5(prosrc)`, owner, oid) on any Phase-2 object;
5. the two P2 roles carry dependencies outside the audited surface
   (`P2_RB_ROLE_DEPENDENCY_UNEXPECTED`), including dependencies in other
   databases or role memberships 075 did not create;
6. the applying role is not a superuser (`P2_RB_APPLYING_ROLE_INSUFFICIENT`);
7. separate operator approval for the rollback has not been given.

In every STOP case the engine refuses BEFORE executing anything and the
database remains exactly as it was. There is no "partial" state.

### 4.3 Step-by-step restore of 075 (snapshot-driven)

```bash
cd /opt/trad_bot

# 1) re-verify the snapshot checksums (mandatory)
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
  verify --snapshot var/htf_phase2/pre075_acl_snapshot.json

# 2) dry run: show the exact restore plan and the explained 075 delta
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_rollback_075_v1.py \
  --snapshot var/htf_phase2/pre075_acl_snapshot.json \
  --dbname trad_bot --dry-run

# 3) execute (one transaction; rolls back completely on ANY error)
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_rollback_075_v1.py \
  --snapshot var/htf_phase2/pre075_acl_snapshot.json \
  --dbname trad_bot

# 4) the tool verifies the result itself and prints
#    RESTORED_AND_VERIFIED  (or refuses with a P2_RB_* FAIL CLOSED code)
```

What the engine restores, in one transaction:

* every ACL tuple of the Phase-2 surface (database CONNECT grants, schema
  `research`, the three tables, the sequence, the five functions) to the exact
  pre-075 `(grantor, grantee, privilege, grantable)` set — including any
  non-standard pre-seeded grants to `PUBLIC`, `trad_bot` or other roles, with
  their `WITH GRANT OPTION` flags and grantors (`GRANT ... GRANTED BY`);
* function configuration of
  `htf_baseline_short_phase2_insert_observation(...)` and
  `htf_baseline_short_phase2_activate(...)`: `SECURITY INVOKER` and the exact
  previous `proconfig` (pinned `search_path` removed via `RESET ALL`, then the
  snapshot entries re-applied);
* the two P2 roles are dropped ONLY if they were created by 075 (i.e. absent
  from the snapshot) and hold no dependency outside the audited surface;
  roles that pre-existed 075 are preserved untouched, together with their
  attributes;
* nothing else: Phase-2 rows (experiment/observation/freeze registry), the
  frozen Point-B `research.prospective_*` stack, other `research` objects,
  other roles, default privileges and other databases are never named by the
  engine.

### 4.4 ACL restore verification (mandatory)

The engine re-captures the full state after the restore and FAILS CLOSED with
`P2_RB_VERIFICATION_FAILED` unless:

* every effective ACL tuple matches the snapshot exactly (this is a
  tuple-level comparison of `aclexplode()` output — NOT a SELECT/INSERT
  availability smoke test);
* per-role effective privileges (`has_*_privilege()`) match the snapshot for
  every captured role and object;
* roles created by 075 are verified gone; pre-existing roles and their
  attributes are verified unchanged.

Independent read-only cross-check (operator, optional but recommended):

```sql
-- same shape as §2: for EVERY row actual must equal expected
-- (run the §2 block; after rollback the expected values are the PRE-075
--  ones: no P2 roles present, no SECURITY DEFINER wall, no pinned
--  search_path, and the snapshot file is the authority for exact ACLs)
SELECT rolname FROM pg_roles WHERE rolname LIKE 'trad_bot_htf_p2%';  -- 0 rows
SELECT p.proname, p.prosecdef, p.proconfig
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'research'
   AND p.proname IN ('htf_baseline_short_phase2_insert_observation',
                     'htf_baseline_short_phase2_activate');
```

Then re-run the snapshot tool and diff against the pre-075 snapshot (this is
exactly what the engine asserts internally):

```bash
sudo -u postgres python3 tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
  compare --before var/htf_phase2/pre075_acl_snapshot.json \
          --after  var/htf_phase2/post_rollback_snapshot.json
# expected: acl_equal=true, effective_privileges_equal=true
```

### 4.5 Partial-failure handling and idempotency

* the restore runs in ONE transaction; an interruption or SQL error rolls the
  whole transaction back — the database is never left partially changed
  (tested: `test_t17_interrupted_rollback_leaves_no_partial_changes`). The CLI
  exits with `ABORTED_NO_CHANGES` and a `P2_RB_*` code in this case;
* re-running the rollback after success is a safe refusal
  (`P2_RB_NOTHING_TO_ROLL_BACK`) and changes nothing (tested: T18);
* `--dry-run` changes nothing and can be repeated freely;
* if ANY `P2_RB_*`/`SNAPSHOT_*` code is returned: STOP, record the code and
  the change log entry, and escalate to the DBA — never retry with manual
  GRANT/REVOKE surgery.

### 4.6 Emergency observer stop is NOT a rollback

Stopping the Phase-2 observer (e.g. `systemctl stop
trad-bot-htf-baseline-short-observer`, revoking the observer login, or
disabling `observer_enabled`) is a SEPARATE operational action for incident
response. It does NOT restore privileges and must never be confused with the
full 075 rollback above; conversely a full 075 rollback is never a substitute
for the graceful stop procedure of the observer runbook (checkpoint
move-aside + replay is at-least-once/idempotent — see the activation runbook
§9).

### 4.7 Rollback of 074 (tables + functions) — destructive, separate approval

Only when Phase-2 is being abandoned entirely and with explicit DBA approval:
DROP the three Phase-2 tables (they are empty unless the observer ran) and the
two functions. This deletes Phase-2 data and is NOT covered by the snapshot
engine:

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
frozen OOS stack is unaffected by any of the above, and no procedure in this
runbook ever names them.

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
