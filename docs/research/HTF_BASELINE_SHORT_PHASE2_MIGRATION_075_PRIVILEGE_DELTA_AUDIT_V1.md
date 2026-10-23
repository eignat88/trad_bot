# HTF Phase-2 — Migration 075 Privilege Delta Audit (V1)

Task ID: `HTF_PHASE2_MIGRATION_075_ROLLBACK_HARDENING_V1`
Issue: #185 (parent #181)
Status of production application of 074/075: **PHASE_2_NO_GO** — nothing in this
document authorizes applying 074/075 to production.

## 1. Purpose

This audit is FIX-1 of issue #185: a complete, operation-by-operation inventory
of every privilege, role, and function-configuration change that
`sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql` performs.
It is the delta model used by:

* `tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py` (FIX-2),
* `tools/research/htf_baseline_short_phase2_rollback_075_v1.py` (FIX-3),
* `tests/test_htf_baseline_short_phase2_rollback_hardening_v1.py` (T01–T20).

Audit method: full read of `074` and `075`, catalog-level state capture before
and after `075` on an isolated disposable PostgreSQL 17 cluster
(`/var/lib/postgresql/17/issue185_it`, port 5545, loopback only), and
tuple-level comparison of effective ACLs
`(grantor, grantee, privilege_type, is_grantable)` via `aclexplode()`.

## 2. Object inventory: pre-existing vs 074/075-created

| Object | Created by | Pre-existing before 075? | 075 touches it? |
|---|---|---|--- |
| `research` schema | pre-existing (074 has `CREATE SCHEMA IF NOT EXISTS`) | YES | grants only (`GRANT USAGE` to the two P2 roles) |
| `research.htf_baseline_short_phase2_experiment` | 074 | NO | REVOKE (PUBLIC, app roles) + GRANT (SELECT) |
| `research.htf_baseline_short_phase2_observation` | 074 | NO | REVOKE (PUBLIC, app roles) + GRANT (SELECT) |
| `research.htf_baseline_short_phase2_freeze_registry` | 074 | NO | REVOKE (PUBLIC, app roles) + GRANT (SELECT) |
| `research.htf_baseline_short_phase2_observation_observation_id_seq` | 074 | NO | REVOKE (PUBLIC, app roles) |
| `research.htf_baseline_short_phase2_insert_observation(...)` | 074 | NO | REVOKE (PUBLIC, app roles) + `SECURITY DEFINER` + `SET search_path` + GRANT (EXECUTE → observer) |
| `research.htf_baseline_short_phase2_activate(...)` | 074 | NO | REVOKE (PUBLIC, app roles) + `SECURITY DEFINER` + `SET search_path` + GRANT (EXECUTE → admin) |
| `research.htf_baseline_short_phase2_experiment_guard()` | 074 | NO | REVOKE (PUBLIC) |
| `research.htf_baseline_short_phase2_observation_guard()` | 074 | NO | REVOKE (PUBLIC) |
| `research.htf_baseline_short_phase2_freeze_registry_guard()` | 074 | NO | REVOKE (PUBLIC) |
| role `trad_bot_htf_p2_observer` | 075 (if absent) | MAY already exist | created/validated + receives grants |
| role `trad_bot_htf_p2_admin` | 075 (if absent) | MAY already exist | created/validated + receives grants |
| roles `trad_bot`, `analytics_runner`, `analytics_agent` | pre-existing | YES (any of them) | REVOKE only, only on the P2 surface |
| current database | pre-existing | YES | `GRANT CONNECT` to the two P2 roles |
| everything else in `research` (e.g. the frozen Point-B `prospective_*` stack) | pre-existing | YES | **NOT TOUCHED** — 075 never names it |

075 refuses to run (`P2_MIGRATION_074_REQUIRED` /
`P2_MIGRATION_074_FUNCTIONS_REQUIRED`) unless all ten 074 objects exist, so
"re-applying 074" is never part of any recovery: 074 creates nothing that 075
revokes, and 074 re-run is a no-op guarded by `IF NOT EXISTS` /
`CREATE OR REPLACE`, but it can never reconstruct grants it never held.

## 3. Role delta

075 creates (only when absent) and otherwise validates:

| Role | DDL when created | Validation when pre-existing | 075-created? |
|---|---|---|--- |
| `trad_bot_htf_p2_observer` | `CREATE ROLE ... WITH LOGIN NOINHERIT` | `P2_OBSERVER_ROLE_ATTRS_UNSAFE` unless `rolcanlogin` AND NOT `rolsuper` AND NOT `rolcreatedb` AND NOT `rolcreaterole` | ONLY if absent |
| `trad_bot_htf_p2_admin` | `CREATE ROLE ... WITH NOLOGIN NOINHERIT` | `P2_ADMIN_ROLE_ATTRS_UNSAFE` unless NOT `rolcanlogin` AND NOT `rolsuper` | ONLY if absent |

075 grants no role attributes, no memberships, no `GRANT ROLE`, no
`ALTER ROLE` on any role; it never sets or reads passwords
(`pg_authid` is never touched — password state is out of scope for both the
snapshot tool and the rollback).

## 4. Per-operation privilege delta matrix

Notation: `explicit-ACL` = stored `*acl` column; `effective` = `has_*_privilege()`.
`app roles` = `trad_bot`, `analytics_runner`, `analytics_agent` **only when the
role exists** (075 iterates a fixed list and skips absent names).

| # | 075 operation | Object | Grantee | Before (after 074) | After 075 | Restore strategy |
|---|---|---|---|---|---|--- |
| 1 | `GRANT CONNECT ON DATABASE` | current DB | `trad_bot_htf_p2_observer`, `trad_bot_htf_p2_admin` | entry absent | `(postgres, role, CONNECT, no)` | `REVOKE ALL ON DATABASE ... FROM <grantee>` + re-grant the snapshot's exact tuples |
| 2 | `GRANT USAGE ON SCHEMA research` | `research` | both P2 roles | entry absent (or whatever was there) | adds `(…, USAGE, no)` | same reset-and-regrant of exact snapshot tuples |
| 3 | `REVOKE ALL ON FUNCTION insert_observation(...) FROM PUBLIC` | insert fn | `PUBLIC` | default `EXECUTE` for `PUBLIC` (`acl` NULL) | `PUBLIC` entry gone | re-grant `EXECUTE ... TO PUBLIC` from the snapshot |
| 4 | `REVOKE ALL ON FUNCTION activate(...) FROM PUBLIC` | activate fn | `PUBLIC` | default `EXECUTE` for `PUBLIC` | gone | same |
| 5 | `REVOKE ALL ON FUNCTION *_guard() FROM PUBLIC` (×3) | 3 guard fns | `PUBLIC` | default `EXECUTE` | gone | same |
| 6 | `REVOKE ALL ON <table> FROM PUBLIC` (×3) | 3 P2 tables | `PUBLIC` | no PUBLIC DML by default; revokes any pre-seeded PUBLIC grant | gone | re-grant pre-seeded PUBLIC tuples exactly |
| 7 | `REVOKE ALL ON SEQUENCE ... FROM PUBLIC` | P2 sequence | `PUBLIC` | default `USAGE` (and any pre-seeded grant) | gone | same |
| 8 | `REVOKE ALL ON <table> FROM <app role>` (×3 tables ×3 roles) | 3 P2 tables | app roles | whatever default-privileges/direct grants put there | gone | re-grant exact snapshot tuples (incl. `WITH GRANT OPTION` flags) |
| 9 | `REVOKE ALL ON SEQUENCE ... FROM <app role>` (×3 roles) | P2 sequence | app roles | same | gone | same |
| 10 | `REVOKE ALL ON FUNCTION ... FROM <app role>` (×2 fns ×3 roles) | insert/activate | app roles | same | gone | same |
| 11 | `ALTER FUNCTION insert_observation SECURITY DEFINER` | insert fn | — | `prosecdef = false` (INVOKER) | `prosecdef = true` | `ALTER FUNCTION ... SECURITY INVOKER` |
| 12 | `ALTER FUNCTION insert_observation SET search_path = pg_catalog, research` | insert fn | — | `proconfig IS NULL` | `proconfig = {search_path=pg_catalog, research}` | `RESET ALL` then re-`SET` snapshot proconfig entries |
| 13 | `ALTER FUNCTION activate SECURITY DEFINER` | activate fn | — | INVOKER | DEFINER | as #11 |
| 14 | `ALTER FUNCTION activate SET search_path = ...` | activate fn | — | `proconfig IS NULL` | pinned `search_path` | as #12 |
| 15 | `GRANT SELECT ON <table>` (×3) | 3 P2 tables | both P2 roles | absent | `(postgres, role, SELECT, no)` | revoke + re-grant snapshot tuples |
| 16 | `GRANT EXECUTE ON FUNCTION insert_observation` | insert fn | observer | absent | `(postgres, observer, EXECUTE, no)` | same |
| 17 | `GRANT EXECUTE ON FUNCTION activate` | activate fn | admin | absent | `(postgres, admin, EXECUTE, no)` | same |

Combined effect on the *effective* access matrix (post-075, proven by
`has_*_privilege()` in tests T03–T06):

| Principal | P2 tables | P2 sequence | insert fn | activate fn | schema `research` | database |
|---|---|---|---|---|---|--- |
| `PUBLIC` | nothing | nothing | nothing | nothing | unchanged | unchanged (CONNECT/TEMPORARY default stays) |
| `trad_bot_htf_p2_observer` | SELECT only | nothing | EXECUTE | **no** | USAGE (no CREATE) | CONNECT only |
| `trad_bot_htf_p2_admin` | SELECT only | nothing | **no** | EXECUTE | USAGE (no CREATE) | CONNECT only |
| `trad_bot` / `analytics_runner` / `analytics_agent` | nothing (revoked) | nothing (revoked) | nothing | nothing | unchanged | unchanged |
| object owner / superuser | implicit, unchanged | implicit, unchanged | implicit | implicit | implicit | implicit |

## 5. Function configuration delta

| Function | `prosecdef` before | `prosecdef` after | `proconfig` before | `proconfig` after |
|---|---|---|---|---|
| `..._insert_observation(...)` | `false` | `true` | `NULL` | `{search_path=pg_catalog, research}` |
| `..._activate(...)` | `false` | `true` | `NULL` | `{search_path=pg_catalog, research}` |
| 3 × `..._*_guard()` | `false` | `false` (untouched) | `NULL` | `NULL` (untouched) |

075 changes nothing else about the functions: `prosrc` (body hash), argument
types, return type, `provolatile`, `prosecdef` of the guards, ownership — all
unchanged. The rollback verifies this via `md5(prosrc)` and identity fields.

## 6. Shared/cluster-level delta

| System area | 075 delta | Rollback handling |
|---|---|--- |
| `pg_shdepend` | derived rows for the new grants; rows for revoked app-role/PUBLIC grants disappear | verified indirectly; unexpected rows ⇒ `P2_RB_ROLE_DEPENDENCY_UNEXPECTED` |
| `pg_auth_members` | none | any new membership ⇒ FAIL CLOSED |
| `pg_default_acl` | none | any change ⇒ FAIL CLOSED |
| other databases | `GRANT CONNECT` is database-local | cross-DB dependency on a P2 role ⇒ FAIL CLOSED |
| `pg_authid` (passwords) | none, never read | never read, never restored, never printed |

## 7. PostgreSQL representation quirks (documented, verified on PG 17.11)

1. **NULL ACL vs empty ACL.** Fresh 074 objects have `acl IS NULL` (default
   form). The first `GRANT`/`REVOKE` materializes an explicit ACL. The
   materialized set is exactly the effective default — for sequences the
   materialized owner set is `rwU` although `acldefault('S', owner)` is just
   `U`. These are *representation-only* differences: the effective tuple set is
   what the snapshot compares, and the restore reproduces the exact snapshot
   tuples. A post-rollback NULL-vs-materialized difference is reported as
   `acl_representation`, never as an ACL inequality.
2. **`REVOKE ALL ... FROM ALL` is not valid syntax.** Revocation must be
   spelled per grantee, including `PUBLIC`.
3. **`GRANT ... GRANTED BY`** is required to restore the exact *grantor* of
   every tuple; the rollback uses it.
4. **`PUBLIC` cannot hold grant options** ("grant options can only be granted
   to roles"), so any `grantable` flag on a PUBLIC tuple would itself be an
   anomaly (fail closed).
5. `MAINTAIN` is a table privilege in PG 17; `SET` is not a table privilege
   (`has_table_privilege` rejects it).

## 8. Why `DROP OWNED BY` cannot restore this state

The erroneous runbook claim was that `DROP OWNED BY <role>` restores the
privileges that 075 revoked. It cannot:

* `REVOKE` deletes privilege tuples. No later command can recreate a tuple
  that the rollback does not know about; `DROP OWNED BY` deletes objects and
  privileges owned by a role — it never re-grants anything to `PUBLIC`,
  `trad_bot`, `analytics_runner`, or `analytics_agent`.
* `DROP OWNED BY` is destructive: it drops every object owned by the role and
  revokes every privilege the role holds — including pre-existing,
  non-Phase-2 objects and grants.
* It cannot restore `prosecdef`/`proconfig` changes.

The only reliable restore mechanism is a **pre-075 snapshot** (FIX-2) driving a
snapshot-derived, transactional restore (FIX-3), with tuple-exact verification.
That mechanism is implemented and tested in this branch; the runbook now
documents it (FIX-4).

## 9. Control set (must remain byte-identical across apply/rollback)

* the frozen Point-B `research.prospective_*` stack (existing OOS/evaluations);
* all `research` objects not listed in section 2;
* all roles except the two P2 roles that 075 itself created;
* Phase-2 table rows (experiment / observation / freeze registry);
* `APPROVED_FREEZE_TS` and every historical observation/outcome row.

The rollback tool never issues SQL naming any of those objects
(`tests/test_htf_baseline_short_phase2_rollback_hardening_v1.py::test_t20_...`
asserts this statically and dynamically).
