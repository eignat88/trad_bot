# HTF BASELINE SHORT Phase-2 — DB Secrets Setup (V1)

Issue: #176 Phase 2 — DB privilege and runtime hardening (CP2/CP6).
Scope: how to provision the dedicated observer credentials safely. Nothing
here activates Phase 2 or touches production data.

## Roles

Migration 075 creates two roles (cluster-wide):

| Role | Login | Purpose |
|---|---|---|
| `trad_bot_htf_p2_observer` | LOGIN | runtime observer: SELECT on the Phase-2 tables + EXECUTE on the insert function ONLY |
| `trad_bot_htf_p2_admin` | NOLOGIN | operator `SET ROLE` holder: EXECUTE on the activation function ONLY |

The observer role must never be the `postgres` superuser or any shared
application role (`trad_bot`, `analytics_runner`, `analytics_agent`) — the
connector rejects those names outright.

## Password provisioning (never in Git)

Set the password once, as a DBA, outside any migration (migrations never
contain passwords):

```sql
\password trad_bot_htf_p2_observer
-- or, from a secrets manager:
ALTER ROLE trad_bot_htf_p2_observer PASSWORD '<value from secrets manager>';
```

The password is SCRAM-SHA-256 verifiable by PostgreSQL; pg8000 performs the
SCRAM handshake natively.

## Runtime secrets file

The systemd unit reads ONLY `/opt/trad_bot/etc/htf_phase2_observer.env`
(the shared application `.env` is intentionally not loaded — scanner
credentials must never satisfy the observer):

```text
HTF_P2_DB_HOST=127.0.0.1
HTF_P2_DB_PORT=5432
HTF_P2_DB_NAME=trad_bot
HTF_P2_DB_USER=trad_bot_htf_p2_observer
HTF_P2_DB_PASSWORD_FILE=/opt/trad_bot/etc/htf_phase2_observer.pw
```

Prefer the password file form. Create both files with strict permissions:

```bash
install -o tradbot -g tradbot -m 0600 /dev/null /opt/trad_bot/etc/htf_phase2_observer.env
install -o tradbot -g tradbot -m 0600 /dev/null /opt/trad_bot/etc/htf_phase2_observer.pw
```

Then fill in the values. `HTF_P2_DB_PASSWORD` (direct value) is also accepted
but the file form keeps the secret out of process listings.

## Fail-closed contract (CP2)

`load_db_credentials()` refuses to connect when:

* `HTF_P2_DB_USER` missing → error (there is NO default user);
* `HTF_P2_DB_USER` ∈ {postgres, trad_bot, analytics_runner, analytics_agent} → error;
* `HTF_P2_DB_NAME` missing → error (no implicit database);
* neither `HTF_P2_DB_PASSWORD` nor `HTF_P2_DB_PASSWORD_FILE` set → error;
* password file unreadable or empty → error.

The password is passed only to the DB driver and is never logged; the
connection log line prints host/port/db/user only.

## Rotation

1. `ALTER ROLE trad_bot_htf_p2_observer PASSWORD '<new>';`
2. rewrite `/opt/trad_bot/etc/htf_phase2_observer.pw` (0600);
3. `systemctl restart trad-bot-htf-baseline-short-observer` (only this unit;
   scanner/paper/evaluator are never restarted for Phase-2 work).

Rotation does not require re-running migrations 074/075.

## Never

* commit these values to Git (`.env`, password files and dumps are forbidden
  by the repository secret policy);
* reuse scanner credentials for the observer;
* share the admin procedure through the observer role (activation is
  revoked from it by 075 and enforced by the database, not by convention).
