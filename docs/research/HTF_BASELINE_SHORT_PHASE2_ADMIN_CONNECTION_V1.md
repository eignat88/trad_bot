# HTF_BASELINE_SHORT_PHASE2_ADMIN_CONNECTION_V1

**Status: design rationale for issue #205. FUTURE USE ONLY — Phase 2 is NOT
activated, the observer is NOT enabled and no production database session was
opened while writing this document.**

Related documents:

* activation procedure: `HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1.md`
  (§3, §12 — admin channel, pre-mint connection check);
* freeze/timestamp authority: `HTF_BASELINE_SHORT_PHASE2_ISSUE197_FROZEN_TIMESTAMP_AUTHORITY_V1.md`;
* RBAC migrations: `sql/migrations/074_htf_baseline_short_phase2_registration.sql`,
  `sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql`.

---

## 1. Problem (issue #205)

The Phase-2 freeze tool
`tools/research/htf_baseline_short_phase2_freeze_record_v1.py` (`mint` →
`claim` → `verify`) opened its database sessions through the **observer**
credential loader (`connect_db_from_env()`, `HTF_P2_DB_*`). At the production
`PHASE_2_GO` attempt all three operations failed closed:

```text
REFUSED (unexpected error): HTF_P2_DB_USER must explicitly name the dedicated
observer role (no default user)
```

The refusal was CORRECT for what it was asked to do: the observer loader
deliberately names only the dedicated observer role and that role holds no
activation privilege. The defect is one of **connection routing**: the admin
operation had no credential channel of its own, so the operator could only
hand the tool observer credentials — which can never mint/claim/verify.

## 2. Design decision: a separate admin channel, not wider rights

Two independent credential namespaces now exist. NOTHING from the observer
namespace can satisfy the admin loader and vice versa:

| | observer runner | freeze tool (admin) |
| --- | --- | --- |
| env prefix | `HTF_P2_DB_*` | `HTF_P2_ADMIN_DB_*` |
| module | `..._observer_runner_v1.py` (unchanged) | `..._admin_connection_v1.py` |
| DB role | `trad_bot_htf_p2_observer` (LOGIN, NOINHERIT) | effective `trad_bot_htf_p2_admin` (NOLOGIN, NOINHERIT) |
| login identity | the observer role itself | an operator LOGIN that is a member of the admin role (production: `postgres`), via a controlled `SET ROLE` |
| privileges | SELECT + EXECUTE `insert_observation(...)` | see §4 |

Key properties (deliberately NOT changed):

* `trad_bot_htf_p2_admin` stays **NOLOGIN** — the fix routes connections
  through a membership + `SET ROLE`, it does not convert the admin role to a
  login, and it does not create new roles, logins, passwords or databases;
* observer RBAC is untouched — the observer still cannot write the freeze
  boundary (its rights are checked as an invariant before every admin session;
  any expansion is a hard refusal);
* no shared `.env`, no `PGPASSWORD`, no scanner variables, no default user —
  the admin loader reads only `HTF_P2_ADMIN_DB_*`, requires
  `HOST/PORT/NAME/USER` plus `PASSWORD`/`PASSWORD_FILE`, and never logs the
  password (optionally pins `SYSTEM_IDENTIFIER` with `pg_control_system()`);
* production `postgres` remains the only production-side login path — no new
  secrets are introduced for the standard flow.

Why `SET ROLE` from an operator login instead of giving the admin role LOGIN:
the activation privilege must remain an *authority*, not an *identity*. The
session reaches it only by an explicit, logged membership step that the
database itself enforces (`pg_has_role(..., 'MEMBER')` or superuser), and the
resulting effective role is verified after the switch. A stolen admin password
does not exist because no admin password exists.

## 3. Explicit target and privilege verification (fail closed)

`connect_admin_db(mode)` verifies, in order, BEFORE any artifact is written:

1. credentials complete (missing `USER`/`PASSWORD` → refusal naming the exact
   missing variable — the production incident message shape);
2. transaction write mode pinned (`SET TRANSACTION READ ONLY` / `READ WRITE`);
3. **server-reported identity** matches the pinned expectations —
   `current_database()`, `inet_server_addr()`, `inet_server_port()`,
   `session_user`, optional `system_identifier` (unverifiable pin → refusal,
   never a pass);
4. **RBAC invariants** — admin role is NOLOGIN, non-superuser, NOINHERIT,
   member of nothing that widens it; observer keeps exactly its frozen rights
   (SELECT + EXECUTE `insert_observation`, no EXECUTE `activate`, no
   INSERT/UPDATE/DELETE/TRUNCATE anywhere);
5. `SET ROLE trad_bot_htf_p2_admin` — refused for non-member logins;
6. **effective context** — `current_user` is the admin role, SELECT is
   allowed, INSERT/UPDATE/DELETE/TRUNCATE are denied (all three Phase-2
   relations), EXECUTE `activate()` only in activate mode, and
   `SHOW transaction_read_only` matches the operation.

Any failure closes the connection and the tool exits `REFUSED` before writing
a record, a claim receipt, an activation config or any database row.

## 4. Per-operation privilege matrix

| operation | session mode | rights used | writes |
| --- | --- | --- | --- |
| `mint` | `READ_ONLY_MODE` (DB-enforced read-only) | SELECT (`now()`) | none — artifact file only |
| `verify` | `READ_ONLY_MODE` | SELECT (3 authorities + freeze registry evidence) | none |
| recovery `claim --repair-activation-json` | `READ_ONLY_MODE` | SELECT | artifact file only, identical record + matching sealed claim receipt |
| initial `claim` | `ACTIVATE_MODE` (the only read-write session) | SELECT + EXECUTE `activate(text,timestamptz,text)` | the single sanctioned `activate()` call + its side-effect-free artifacts |

Even the read-write claim session cannot write directly: the admin role has no
INSERT/UPDATE/DELETE/TRUNCATE on the Phase-2 relations, so the only database
effect possible is `research.htf_baseline_short_phase2_activate(...)` —
atomic, single-row, no-clobber (`P2_FREEZE_TS_ALREADY_CLAIMED`,
`P2_FREEZE_TS_SET_ONCE`). `mint`/`verify` additionally run with
`default_transaction_read_only = on`, so they cannot perform the activation
even in principle (a mint-only or verify-only channel needs no activation
privilege — regression-tested by revoking EXECUTE and confirming mint still
works while claim refuses).

## 5. Failure matrix (all fail-closed, nothing written, DB untouched)

| scenario | outcome |
| --- | --- |
| missing admin credentials (the incident shape: only `HTF_P2_DB_*` present) | all three ops `REFUSED`, observer env never reaches the loader |
| observer credentials passed to the admin tool | `REFUSED` (loader names the dedicated observer role) |
| wrong password / unreachable database | `REFUSED (admin connection)` before any artifact |
| login that is not a member of the admin role | `REFUSED` at `SET ROLE` |
| admin role converted to `LOGIN` / to superuser | `REFUSED` (RBAC invariant) |
| observer RBAC expanded (e.g. EXECUTE `activate` granted) | `REFUSED` (RBAC invariant) |
| wrong database / wrong cluster (identity or system-identifier pin) | `REFUSED` before any artifact |
| stale or non-canonical freeze timestamp | `mint` `REFUSED` |
| fingerprint mismatch in the record | `claim` `REFUSED` |
| repeated `mint` over an existing record | `REFUSED`, file untouched (no overwrite) |
| repeated `claim` / second record for the same boundary | `REFUSED`, registry keeps exactly one row |
| concurrent claims | exactly one winner; loser `REFUSED` |
| crash after DB commit, before activation JSON | `REFUSED` + recovery via `claim --repair-activation-json` with the sealed claim receipt |
| missing / damaged claim receipt in recovery | `REFUSED` (identity unverifiable — STOP and escalate) |
| observer attempting to bypass the freeze | cannot `SET ROLE` admin, cannot execute `activate()` |

## 6. Verification of the freeze (three authorities + registry evidence)

`verify` remains READ-ONLY and requires full agreement of the freeze record,
the database registration and the activation config — plus the freeze-registry
evidence row (`FREEZE_REGISTRY_EVIDENCE=OK`) — before it prints
`THREE_AUTHORITY_AGREEMENT=OK`. `observer_enabled` stays `FALSE` and the
observation table stays empty across the entire `mint → claim → verify`
cycle; enabling the observer is a separate operator action outside this tool
(runbook §3.6).

## 7. Test evidence

* `tests/test_htf_baseline_short_phase2_admin_connection_v1.py` — unit:
  loader/credential matrix, refusal of observer credentials, password-file
  handling and never-logged passwords, identity pinning, RBAC invariant
  refusals, effective-context matrix, connect orchestration and ordering,
  source-scan (no `.env`, no `os.environ` outside the admin prefix).
* `tests/test_htf_baseline_short_phase2_freeze_record_v1.py` — tool routing:
  `mint`/`verify`/recovery never open an activate session; the initial claim
  opens read-only first and exactly one activate session; verify demands
  registry evidence.
* `tests/test_htf_baseline_short_phase2_admin_connection_postgres_v1.py` —
  isolated PostgreSQL integration (127.0.0.1:5544, disposable
  `trad_bot_p2_it_*` databases, real SCRAM + operator-logins): the full cycle
  through both driver paths, the privilege matrix above and the complete
  failure matrix, including the Point-B sentinel and the observer-still-off
  invariants.
