# HTF Phase-2 — Migration 075 Rollback Hardening (V1)

Task ID: `HTF_PHASE2_MIGRATION_075_ROLLBACK_HARDENING_V1`
Issue: #185 (parent #181)
Production status: `PHASE_2_NO_GO` — nothing here authorizes applying 074/075
to production (that is a separate authorization via #181).

## 1. Root cause

The original runbook claimed that a rollback of migration 075 could restore the
previous privilege state with:

```sql
DROP OWNED BY trad_bot_htf_p2_observer;
DROP OWNED BY trad_bot_htf_p2_admin;
```

This is wrong and unsafe:

1. `REVOKE` **deletes** privilege tuples. `DROP OWNED BY` deletes
   objects/privileges owned by a role; it never re-grants anything to
   `PUBLIC`, `trad_bot`, `analytics_runner` or `analytics_agent`. The grants
   that 075 revoked simply vanish from every rollback attempt that does not
   know their exact content.
2. `DROP OWNED BY` is destructive: it drops every role-owned object and
   revokes every privilege the role holds, including pre-existing non-Phase-2
   objects.
3. It cannot restore function configuration (`SECURITY DEFINER`,
   `search_path` pinning) either.

The only reliable restore source is a **pre-075 ACL snapshot** that records the
exact effective ACL tuples and function configuration, driving a
snapshot-derived, transactional, fail-closed restore. This document specifies
that mechanism and the test matrix proving it.

## 2. Components

| Component | File | Role |
|---|---|--- |
| Privilege delta audit (FIX-1) | `docs/research/HTF_BASELINE_SHORT_PHASE2_MIGRATION_075_PRIVILEGE_DELTA_AUDIT_V1.md` | complete per-operation inventory of the 075 delta; the model the tools enforce |
| ACL snapshot tool (FIX-2) | `tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py` | READ-ONLY capture of the full privilege state + deterministic JSON + SHA-256 sidecar |
| Rollback engine (FIX-3) | `tools/research/htf_baseline_short_phase2_rollback_075_v1.py` | snapshot-driven, transactional, fail-closed restore of exactly the 075 delta |
| Runbook (FIX-4) | `docs/research/HTF_BASELINE_SHORT_PHASE2_MIGRATION_074_075_RUNBOOK_V1.md` | preflight → 074 → snapshot → 075 order, verification, rollback procedure, STOP conditions |
| Test matrix | `tests/test_htf_baseline_short_phase2_rollback_hardening_v1.py` | T01–T20 + adversarial cases on a disposable PostgreSQL 17 cluster |
| Unit/static tests | `tests/test_htf_baseline_short_phase2_acl_snapshot_tool_v1.py` | snapshot determinism, checksums, secret redaction, planning, scope statics |

## 3. Snapshot format (FIX-2)

`htf_p2_acl_snapshot_v1` — deterministic JSON (`sort_keys`, ASCII, stable
ordering everywhere) with:

* database, `research` schema, every `research` relation/function of the
  Phase-2 surface (3 tables, 1 sequence, 5 functions with **exact
  type-only signatures**), each with identity (oid, owner, `md5(prosrc)` for
  functions), **explicit ACL** (`acl IS NULL` flag + `aclexplode()` of the
  stored ACL) and **effective ACL** (`aclexplode()` of
  `COALESCE(acl, acldefault(...))`) as `(grantor, grantee, privilege,
  grantable)` tuples;
* all roles with their attributes (via the secret-free `pg_roles` view —
  `pg_authid` is never read), role memberships, default privileges,
  `pg_shdepend` rows of every captured role (cross-database dependency proof);
* per-role effective privileges (`has_*_privilege()`) as an independent second
  proof, kept separate from the explicit ACL;
* `payload_sha256` (hash of the canonical state) plus a `<file>.sha256`
  sidecar (hash of the exact file bytes);
* secret redaction is enforced (`assert_no_secrets`): no password/verifier/
  token material can enter a snapshot.

The tool runs with `SET LOCAL default_transaction_read_only = on`, requires
PostgreSQL 17, and its module contains no mutating SQL (asserted statically).

## 4. Rollback algorithm (FIX-3)

```text
load snapshot  -> verify file + payload SHA-256 (fail closed on mismatch/missing)
require superuser applying role                       (P2_RB_APPLYING_ROLE_INSUFFICIENT)
capture current state (read-only)
require snapshot database == connected database       (P2_RB_DATABASE_MISMATCH)
check pg_shdepend / pg_auth_members of the P2 roles   (P2_RB_ROLE_DEPENDENCY_UNEXPECTED)
classify every state difference against the audited 075 delta
    unexplained difference                          -> P2_RB_CONFLICTING_STATE
    nothing but representation/derived noise        -> P2_RB_NOTHING_TO_ROLL_BACK (safe refusal)
build the restore plan from the SNAPSHOT (never guessed):
    per object: REVOKE ALL per grantee, then GRANT <exact snapshot tuples>
                ... GRANTED BY <grantor> [WITH GRANT OPTION]
    functions:  SECURITY INVOKER, RESET ALL, then SET each snapshot proconfig entry
    roles:      DROP only roles created by 075 (absent from the snapshot) and
                dependency-free; pre-existing roles are preserved untouched
execute the whole plan in ONE transaction owned by run_rollback
re-capture and verify INSIDE that same still-uncommitted transaction:
    exact ACL tuples == snapshot, effective privileges == snapshot,
    roles dropped/preserved as planned      (P2_RB_VERIFICATION_FAILED)
commit ONLY after every verification passed -> RESTORED_AND_VERIFIED
(any failure at any point -> rollback: zero committed changes)
```

Properties:

* **No guessing**: every restored tuple comes from the snapshot; missing or
  tampered snapshots are refused, never reconstructed.
* **Scoped**: SQL never names objects outside the Phase-2 surface; the frozen
  Point-B `prospective_*` stack, other `research` objects, other roles and
  Phase-2 rows are untouched (asserted by T19/T20 and by static checks).
* **Atomic**: single transaction; `fail_after` and `fail_point` test seams
  prove that interruption, verification failure, ACL/function-config mismatch
  and failure before commit all leave zero committed changes (T17 + A01–A08).
* **Idempotent**: a second run refuses safely (T18).
* **Representation-aware**: NULL vs materialized ACLs are reported as
  `acl_representation` and never treated as an ACL difference.

### Transaction atomicity contract (PR #191 review fix)

`run_rollback()` owns the complete transaction lifecycle:

1. the drift/dependency checks, every restore statement, the post-restore
   capture (`capture_state(..., manage_transaction=False)`) and the full
   ACL/role/function verification share **one uncommitted transaction**;
2. the module contains exactly **one** `conn.commit()` and it executes
   strictly **after** every post-restore verification check passed;
3. every other exit — fail-closed refusal, `P2_RB_NOTHING_TO_ROLL_BACK`,
   injected test failure, unexpected exception — ends in `conn.rollback()`
   and preserves the original database state byte-for-byte.

Connection settings cannot bypass the boundary: the tool refuses
`autocommit` connections (`P2_RB_CONNECTION_AUTOCOMMIT`) and connections that
are already inside a transaction it does not own
(`P2_RB_TRANSACTION_NOT_IDLE`), both fail closed before anything runs. The
deterministic failure-injection seams (`fail_after` = abort before statement
N; `fail_point` ∈ `verify_exception` / `skip_acl_grants` /
`skip_function_config_restore` / `before_commit`) exist purely for the
atomicity tests and are rejected for any other value.

PostgreSQL limitations to keep in mind (documented, not worked around):

* `CREATE ROLE` / `DROP ROLE` are cluster-wide catalog changes: they are
  transactional (they roll back with the transaction) but are visible to all
  databases only at commit, and concurrent role DDL serializes on the
  `pg_authid` locks — a long rollback window blocks other role DDL.
* `DROP ROLE` fails while the role carries dependencies in **any** database;
  the pre-check turns that into a fail-closed refusal
  (`P2_RB_ROLE_DEPENDENCY_UNEXPECTED`) before any statement runs.
* Concurrent DDL on the audited surface during the rollback window makes the
  post-restore verification fail (`P2_RB_CONFLICTING_STATE` /
  `P2_RB_VERIFICATION_FAILED`) and rolls everything back — the rollback must
  run in a quiet window, as the runbook requires.

### Fail-closed codes

| Code | Condition |
|---|--- |
| `P2_RB_SNAPSHOT_REQUIRED` | no snapshot path given |
| `SNAPSHOT_FILE_MISSING` / `SNAPSHOT_CHECKSUM_FILE_MISSING` / `SNAPSHOT_FILE_CHECKSUM_MISMATCH` / `SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH` | snapshot integrity (T13/T14) |
| `P2_RB_APPLYING_ROLE_INSUFFICIENT` | applying role is not a superuser |
| `P2_RB_DATABASE_MISMATCH` | snapshot taken on another database |
| `P2_RB_ROLE_DEPENDENCY_UNEXPECTED` | role membership/ownership/cross-DB/out-of-surface dependency of a P2 role (T16) |
| `P2_RB_CONFLICTING_STATE` | state difference the audited 075 delta cannot explain, incl. object-identity drift (T15) |
| `P2_RB_NOTHING_TO_ROLL_BACK` | safe refusal: nothing to do (T18) |
| `P2_RB_VERIFICATION_FAILED` | post-restore verification mismatch |
| `P2_RB_CONNECTION_AUTOCOMMIT` | connection runs with `autocommit` — no single-transaction restore possible |
| `P2_RB_TRANSACTION_NOT_IDLE` | connection already inside a transaction the tool does not own |
| `ABORTED_NO_CHANGES` (CLI) | interrupted/failed transaction — nothing committed (T17) |

## 5. Test matrix (T01–T20) — temporary isolated PostgreSQL 17 only

Cluster used: `/var/lib/postgresql/17/issue185_it`, port **5545**, loopback
only — NOT the production cluster (`/var/lib/postgresql/17/main`, port 5432),
NOT the earlier `issue181_it` cluster (port 5544, stopped and left untouched).
Tests create/drop only `trad_bot_p2_it_*` databases and test-owned roles; the
production database name `trad_bot` is blocked in code.

| ID | Scenario | Expected result | Test |
|---|---|---|--- |
| T01 | Snapshot taken before 075 | full surface captured; deterministic; checksummed; secret-free; read-only | `test_t01_snapshot_before_075_captures_full_surface` |
| T02 | Apply 074 then 075 | every state difference explainable by the audited delta | `test_t02_apply_074_then_075` |
| T03 | Observer least privilege | SELECT on 3 tables + EXECUTE insert only; no DML/sequence/activate | `test_t03_observer_least_privilege` |
| T04 | Admin activation-only | EXECUTE activate + SELECT only; observer cannot activate | `test_t04_admin_activation_only_access` |
| T05 | PUBLIC revoked | no PUBLIC entry on the whole Phase-2 surface | `test_t05_public_access_revoked_on_phase2_surface` |
| T06 | Existing app roles isolated | `trad_bot`/`analytics_*` lose Phase-2 access, unrelated grants intact | `test_t06_existing_application_roles_isolated` |
| T07 | Rollback 075 | `RESTORED_AND_VERIFIED`, both 075 roles dropped | `test_t07_rollback_075_completes` |
| T08 | ACL before == ACL after rollback | tuple-level equality of every effective ACL + effective privileges | `test_t08_exact_acl_before_equals_after_rollback` |
| T09 | SECURITY DEFINER restored | `prosecdef` back to `INVOKER` on both functions | `test_t09_security_definer_restored` |
| T10 | search_path restored | exact pre-075 `proconfig` on all 5 functions | `test_t10_search_path_restored` |
| T11 | Existing roles preserved | pre-existing P2-named roles + app roles untouched, attributes identical | `test_t11_existing_roles_preserved` |
| T12 | Phase-2 data preserved | observation rows and frozen experiment row identical after rollback | `test_t12_phase2_data_preserved` |
| T13 | Missing snapshot | FAIL CLOSED, database unchanged | `test_t13_missing_snapshot_fail_closed` |
| T14 | Checksum mismatch | FAIL CLOSED (file, payload, missing sidecar), unchanged | `test_t14_snapshot_checksum_mismatch_fail_closed` |
| T15 | Object identity drift | FAIL CLOSED (`P2_RB_CONFLICTING_STATE`), unchanged | `test_t15_object_identity_drift_fail_closed` |
| T16 | Unexpected role dependency | FAIL CLOSED (out-of-surface grant, cross-DB grant, membership, ownership), unchanged | `test_t16_unexpected_role_dependency_fail_closed` |
| T17 | Interrupted rollback | no partial changes at any cut point; full rollback still works | `test_t17_interrupted_rollback_leaves_no_partial_changes` |
| T18 | Repeated rollback | safe refusal `P2_RB_NOTHING_TO_ROLL_BACK`, unchanged | `test_t18_repeated_rollback_safe_refusal` |
| T19 | Unrelated `research` objects | byte-identical across rollback (ACL + data) | `test_t19_unrelated_research_objects_unchanged` |
| T20 | Existing Point-B stack | unchanged; rollback SQL never names `prospective` | `test_t20_point_b_stack_unchanged_and_structurally_untouchable` |

Adversarial cases (same module): pre-seeded non-standard
`PUBLIC`/`trad_bot`/`analytics_runner`/custom-role grants with
`WITH GRANT OPTION` round-trip exactly (`test_adv_nonstandard_public_and_app_grants_roundtrip`);
custom-role grants outside the 075 delta survive untouched
(`test_adv_custom_role_grants_outside_075_delta_survive`);
pre-seeded P2-named roles are preserved with their attributes
(`test_adv_preseeded_p2_roles_with_login_are_preserved`);
non-superuser application is refused
(`test_adv_superuser_requirement_fails_closed`).

Exact ACL comparison requirement: satisfied as tuple-level comparison of
`aclexplode()` output over `COALESCE(acl, acldefault(...))` for every captured
object plus per-role `has_*_privilege()` — never substituted by
SELECT/INSERT availability checks (asserted in T08 and in the unit tests).

### Atomicity cases (A01–A08, same module)

| ID | Scenario | Expected result | Test |
|---|---|---|---|
| A01 | Failure during SQL execution (`fail_after`) | 0 commits, rollback called, full 075 state hash-identical, both roles present | `test_atomic_sql_execution_failure_commits_nothing` |
| A02 | Failure during post-verification (`fail_point=verify_exception`) | 0 commits, state unchanged | `test_atomic_post_verification_failure_commits_nothing` |
| A03 | ACL mismatch after restoration (`fail_point=skip_acl_grants`) | `P2_RB_VERIFICATION_FAILED`, 0 commits, no partial privileges/dropped roles | `test_atomic_acl_mismatch_after_restoration_rolls_back` |
| A04 | Function-config mismatch (`fail_point=skip_function_config_restore`) | `P2_RB_VERIFICATION_FAILED`, 0 commits, state unchanged | `test_atomic_function_config_mismatch_rolls_back` |
| A05 | Successful restore | exactly ONE `commit()`, zero `rollback()`, verified `RESTORED_AND_VERIFIED` | `test_atomic_success_commits_exactly_once_and_verifies` |
| A06 | Failure before commit (`fail_point=before_commit`) | 0 commits, nothing restored, roles still present | `test_atomic_failure_before_commit_leaves_no_partial_state` |
| A07 | `autocommit` connection | refused `P2_RB_CONNECTION_AUTOCOMMIT`, nothing executed | `test_atomic_autocommit_connection_is_refused` |
| A08 | foreign open transaction | refused `P2_RB_TRANSACTION_NOT_IDLE`, caller's state untouched | `test_atomic_foreign_open_transaction_is_refused` |

## 6. Test isolation and cleanup

* cluster identity is verified before use (`data_directory`/port/database
  list); tests refuse non-loopback targets without `P2_TEST_DB_ISOLATION_ACK=yes`;
* only `trad_bot_p2_it_*` databases and test-owned roles are created; every
  created object is tracked and dropped at teardown; pre-existing roles are
  never deleted;
* after the run, residual `trad_bot_p2_it_*` databases/roles must be zero
  (checked explicitly in the final report);
* the test cluster itself is stopped and removed after the campaign; the
  production cluster and the stopped `issue181_it` cluster are never touched.

## 7. Production isolation statement

No `GRANT`/`REVOKE`/`ALTER ROLE`/`CREATE ROLE`/rollback/migration was executed
against production at any point. Production was accessed read-only to record
the role list (isolation proof). The Phase-2 observer is not installed or
started; scanner/paper/evaluator were not restarted; `APPROVED_FREEZE_TS` and
frozen OOS state are untouched.
