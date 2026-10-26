# HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1

**Status: FUTURE USE ONLY. Nothing in this runbook was executed. Phase 2 is not
activated. This document exists so that a future, separately authorized
activation is explicit, ordered and auditable.**

Every step below requires explicit operator instruction. No step is automated,
none is part of CI, merge or deployment.

> **Issue #197 update (frozen protocol + timestamp authority V1).** The
> protocol module is now `FROZEN` (fingerprint pinned
> `22fa0c08eed92df7bb6e94576987305343a71083f3129f3ce4acd07c287cb3f0`) and the
> freeze timestamp is NO LONGER stored in Git. It is chosen at the separate
> operator GO and materialized as an operator-approved, immutable **freeze
> record** claimed through
> `tools/research/htf_baseline_short_phase2_freeze_record_v1.py`
> (`mint` → `claim` → `verify`). The observer unit now passes
> `--freeze-record /opt/trad_bot/etc/htf_phase2_freeze_record.json`; without a
> valid, mutually consistent freeze record the registration gate refuses
> startup (exit 3, zero observations). Design rationale and the partial-failure
> matrix: `HTF_BASELINE_SHORT_PHASE2_ISSUE197_FROZEN_TIMESTAMP_AUTHORITY_V1.md`.
> Nothing here authorizes activation — `PHASE_2_NO_GO` remains in force.

> **Issue #205 update (admin connection routing).** `mint`/`claim`/`verify` no
> longer open any database session through the observer credential loader. The
> tool uses a separate **admin channel**
> (`app/research/htf_baseline_short_phase2_admin_connection_v1.py`, env prefix
> `HTF_P2_ADMIN_DB_*`): an operator LOGIN that is a member of the NOLOGIN role
> `trad_bot_htf_p2_admin` gets there via a controlled `SET ROLE`, with
> per-operation privileges — `mint`, `verify` and claim-recovery run in
> PostgreSQL READ-ONLY sessions with SELECT only, and only the initial `claim`
> opens read-write, where its single write is the sanctioned
> `research.htf_baseline_short_phase2_activate(...)` call (direct DML is denied
> to the admin role by the database). The dedicated observer role/credentials
> serve ONLY the observer runner and are refused by the admin tool. Design
> rationale and the failure matrix:
> `HTF_BASELINE_SHORT_PHASE2_ADMIN_CONNECTION_V1.md`. Nothing here authorizes
> activation — `PHASE_2_NO_GO` remains in force.

> **Issue #184 update (systemd Bybit wiring).** The observer unit now selects
> the Bybit closed-candle source **explicitly** via `--candle-source bybit` in
> `ExecStart`. The former CSV runtime limitation is gone: CSV remains only the
> frozen **offline replay / deterministic test** source and is still the Python
> default. Every activation gate is unchanged and still fails closed. Nothing
> in this update authorizes activation — `PHASE_2_NO_GO` remains in force.
> See §11–§15.

---

## 0. Preconditions (all must hold)

1. PR for `feature/htf-issue176-phase2-runtime-repair-v1` reviewed and merged
   to `main` (merge **must not** activate anything).
2. PR for the Phase-2 Bybit candle source (issue #181 / PR #182) reviewed and
   merged to `main` — `BybitPhase2CandleSource` (closed 5m candles, server-time
   ASOF verification, bounded pagination, durable cursor-aware backfill).
3. PR for the systemd Bybit wiring (issue #184) reviewed and merged to `main`
   — the unit carries `--candle-source bybit` explicitly (merge **must not**
   activate anything).
4. Repeat readiness audit (Gate A–K) completed with verdict
   `READY_FOR_OPERATOR_REVIEW` — and separately approved.
5. Protocol `HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1` reviewed; the six
   `OPERATOR_APPROVAL_REQUIRED` fields decided; the approved fingerprint
   recorded.
6. Deployment `main → VPS` performed through the normal pipeline; VPS working
   tree clean; health checks green.

## 1. Protocol freeze (operator)

**Implemented by PR for issue #197 — the specification freeze is DONE; the
timestamp/activation gates below are NOT.**

1. The protocol module carries `PROTOCOL_STATUS = "FROZEN"` with the pinned
   `FROZEN_PROTOCOL_FINGERPRINT`; the six approved values, causal contracts and
   identity rules are unchanged (regression-tested). Any later parameter change
   needs a NEW protocol version, a NEW experiment version and a NEW freeze.
2. Verify on the deployed commit (read-only):

   ```bash
   python - <<'PY'
   from app.research.htf_baseline_short_phase2_protocol_v1 import (
       PROTOCOL_STATUS, APPROVED_FREEZE_TS, approved_freeze_ts,
       protocol_fingerprint, verify_frozen_fingerprint, operator_decisions_pending,
   )
   print("status", PROTOCOL_STATUS)
   print("freeze_ts_in_git", APPROVED_FREEZE_TS, approved_freeze_ts())
   print("pending", operator_decisions_pending())
   print("fingerprint", protocol_fingerprint())
   print("pin_ok", verify_frozen_fingerprint())
   PY
   ```

   Expected: `FROZEN`, `None`, `[]`, fingerprint
   `22fa0c08eed92df7bb6e94576987305343a71083f3129f3ce4acd07c287cb3f0`,
   `True`. ANY deviation is a hard STOP.
3. Record `FROZEN + <commit sha> + <fingerprint> + <operator> + <date>` in the
   change log. The protocol carries NO timestamp on purpose: the freeze
   timestamp is chosen at the activation GO (§3) so it can never go stale
   during review/merge/CI/deployment.

## 2. Database migration (operator-approved, separate from merge)

1. Take a backup/point-in-time snapshot of `trad_bot`.
2. Apply `sql/migrations/074_htf_baseline_short_phase2_registration.sql`
   through the standard migration process on VPS.
3. Verify (read-only):

   ```sql
   SELECT experiment_id, status, freeze_ts, observer_enabled, protocol_fingerprint
     FROM research.htf_baseline_short_phase2_experiment;
   ```

   Expected: `status = PENDING`, `freeze_ts = NULL`,
   `observer_enabled = FALSE`, `protocol_fingerprint = NULL`.
4. Confirm Point-B experiments are untouched:

   ```sql
   SELECT experiment_id, status, freeze_ts FROM research.prospective_experiment
    WHERE experiment_id LIKE 'HTF%';
   ```

   The two Point-B rows must keep `freeze_ts = 2026-10-06T14:00:00Z`.

## 3. Activation (operator, one deliberate action)

**None of the steps below may run until a separate `PHASE_2_GO` decision is
issued.** The section records the future, ordered procedure. Timestamp
authority V1 (issue #197): the `FREEZE_TS` is chosen HERE — at the activation
GO, as close to the database claim as possible — and never earlier.

Freeze timestamp selection rules (enforced, not advisory):

* chosen at the GO by the `mint` tool from the **PostgreSQL server clock**
  (or passed explicitly and validated against it); never `2026-10-06T14:00:00Z`
  (Point-B), never a past timestamp, never a re-used one;
* claimable only inside the DB acceptance window `now()-60s .. now()+5m`
  (checked in `mint`/`claim` AND inside `activate()`);
* `signal_time > freeze_ts` and `first_touch_time > freeze_ts` are strict;
* everything at or before the freeze is ineligible forever.

1. Prepare the operator-maintained configuration artifacts (§12) — the
   dedicated secrets file `htf_phase2_observer.env` and the runtime activation
   config path `htf_phase2_activation.json` (written by `claim`, step 4).
   None is ever generated automatically; none may be committed to Git.
2. **Configure the admin channel and run the pre-mint connection check**
   (issue #205) — the freeze tool never uses observer credentials; it needs the
   separate admin channel (§12). Create the admin credentials (password file
   0600, `HTF_P2_ADMIN_DB_*`), preferably pin the cluster with
   `HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER`, then run the pre-mint connection check
   (a scratch `mint` — it opens a READ-ONLY session and writes **nothing** to
   the database):

   ```bash
   python tools/research/htf_baseline_short_phase2_freeze_record_v1.py mint \
     --operator-approval-ref "<pre-mint connection check>" \
     --issued-by "<operator>" --record-id "$(uuidgen)" \
     --out /tmp/htf_p2_conncheck_freeze_record.json
   rm -f /tmp/htf_p2_conncheck_freeze_record.json
   ```

   Expected: `ADMIN_DB_CONNECTED mode=read_only role=trad_bot_htf_p2_admin` and
   a minted scratch record. ANY `REFUSED` / `REFUSED (admin connection)` here
   is a hard STOP **before** the real GO: fix credentials, cluster/database
   target or RBAC drift now — never at claim time. Confirm the admin env is
   complete and that no `HTF_P2_DB_*` observer variable is set in this shell.
3. **Mint the freeze record** (operator GO; writes nothing to the database):

   ```bash
   python tools/research/htf_baseline_short_phase2_freeze_record_v1.py mint \
     --operator-approval-ref "<GO reference + operator identity>" \
     --issued-by "<operator>" --record-id "$(uuidgen)" \
     --out /opt/trad_bot/etc/htf_phase2_freeze_record.json
   ```

   Record the printed `record_sha256` + canonical JSON in the change log
   **before running `claim`** — that hash fixes the record's identity for the
   whole lifecycle (the claim receipt binds it exactly; any later repair must
   match it byte-for-byte). Never re-mint to "refresh" a record: `mint`
   refuses to overwrite an existing file and a second record can never claim
   the same boundary. Independent verification recipe and its limits — tamper
   evidence only, NOT a signature — in
   `HTF_BASELINE_SHORT_PHASE2_ISSUE197_FROZEN_TIMESTAMP_AUTHORITY_V1.md` §4.2.
   `mint` refuses on pinned-fingerprint drift, non-canonical timestamps,
   out-of-window values and legacy boundary timestamps.
4. **Claim** (same GO, immediately after) — this is the ONE sanctioned
   activation path (`research.htf_baseline_short_phase2_activate(...)`), then
   the runtime activation config is written and all three authorities are
   re-verified:

   ```bash
   python tools/research/htf_baseline_short_phase2_freeze_record_v1.py claim \
     --record /opt/trad_bot/etc/htf_phase2_freeze_record.json \
     --activation-out /opt/trad_bot/etc/htf_phase2_activation.json
   ```

   Expected terminal line: `THREE_AUTHORITY_AGREEMENT=OK`. On any `REFUSED` /
   `PARTIAL ACTIVATION` output: STOP — never mint a new timestamp; follow the
   recovery matrix in `HTF_BASELINE_SHORT_PHASE2_ISSUE197_FROZEN_TIMESTAMP_AUTHORITY_V1.md` §4.1.
   The initial claim first seals the **claim receipt**
   `htf_phase2_activation.json.claim_receipt.json` (printed as
   `CLAIM_RECEIPT_SEALED=...`) — the trusted record-identity binding written
   exactly once BEFORE the database commit (§7, and §4.3 of the design doc).
   If the database commit succeeded but the activation JSON is missing, the
   recovery is `claim --repair-activation-json` with the SAME record — this
   **delayed repair** also works after the acceptance window has expired
   (PR #200 FIX-1): it requires the unchanged DB record and its matching
   immutable `freeze_registry` row **and** an exact `record_id` +
   `record_sha256` match against the sealed claim receipt (PR #200 FIX-4) — a
   record minted later, even with the identical `freeze_ts` and fingerprint,
   is refused. It performs no activation and fails closed before any write on
   the slightest mismatch, and also when the claim receipt is missing (the
   original identity cannot be established — STOP and escalate).
5. Read-only confirmation (`... verify --record ... --activation ...`) plus the
   §4 SQL checks; keep the output in the change log. `verify` is READ-ONLY and
   additionally requires the freeze-registry evidence row
   (`FREEZE_REGISTRY_EVIDENCE=OK` inside `THREE_AUTHORITY_AGREEMENT=OK`).
   **Observer-still-off check** (expected after claim AND after verify):

   ```sql
   SELECT status, freeze_ts, observer_enabled
     FROM research.htf_baseline_short_phase2_experiment;
   SELECT count(*) FROM research.htf_baseline_short_phase2_observation;
   ```

   Expected: `status = RUNNING`, `observer_enabled = FALSE`, count `0` — the
   claim/verify cycle must never enable the observer or write observations;
   anything else is a hard STOP.
6. **Issue #198 only — observe-only startup** (a separate
   `GO_FOR_OBSERVE_ONLY_STARTUP`, NOT part of this activation):

   ```sql
   UPDATE research.htf_baseline_short_phase2_experiment
      SET observer_enabled = TRUE
    WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
   ```

7. Verify the effective systemd unit (§13) and run the fail-closed checks
   (§14) **before** creating the flag file.
8. Create the unit gate flag file (never automated; the LAST authorization
   step before start):

   ```bash
   install -d /opt/trad_bot/etc && touch /opt/trad_bot/etc/htf_phase2_observer_enabled
   ```

9. Start the service **disabled-by-default unit** — and only this unit:

   ```bash
   systemctl daemon-reload
   systemctl start trad-bot-htf-baseline-short-observer
   systemctl status trad-bot-htf-baseline-short-observer --no-pager
   ```

   Never `systemctl enable` this unit. It is started manually, per activation,
   and must remain start-on-demand only.

**STOP conditions (hard):** pinned fingerprint drift; `operator_decisions_pending() != []`;
any `REFUSED`/`PARTIAL ACTIVATION` from mint/claim/verify — including any
`REFUSED (admin connection)`: missing admin credentials, observer credentials
supplied to the admin channel, wrong password, a login that may not
`SET ROLE` to `trad_bot_htf_p2_admin`, admin-role/observer-RBAC drift, wrong
cluster/database target (fix the channel in step 2, never at claim time);
DB registration not `PENDING` before the claim; freeze registry non-empty
without a matching record; any three-authority disagreement; unit or flag
file present before step 8; `observer_enabled` anything but `FALSE` after the
claim; any pre-freeze row in the observation table.

## 4. Post-activation health check

* `journalctl -u trad-bot-htf-baseline-short-observer -n 100` — no
  `fail closed`, no traceback; heartbeat lines present.
* `SELECT count(*), max(signal_time) FROM research.htf_baseline_short_phase2_observation;`
  — rows appear only after `signal_time > freeze_ts`.
* Verify strict freeze: zero rows with `signal_time <= freeze_ts` or
  `first_touch_time <= freeze_ts` (the CHECK constraints make this impossible,
  but verify explicitly).
* Verify Point-B still `RUNNING` with its original freeze.
* Verify scanner/paper/live untouched (`systemctl is-active trad-bot-scanner
  trad-bot-paper` unchanged, no paper/live writes attributable to the observer).

## 5. Rollback / emergency stop (observer only)

* Emergency stop (observation writes stop immediately) — **only** the observer
  unit is stopped; no other service is restarted or reloaded:

  ```bash
  systemctl stop trad-bot-htf-baseline-short-observer
  rm -f /opt/trad_bot/etc/htf_phase2_observer_enabled
  ```

  ```sql
  UPDATE research.htf_baseline_short_phase2_experiment
     SET observer_enabled = FALSE
   WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
  ```

  (`TRUE → FALSE` is always allowed by the DB guard trigger.)
* Do **not** delete or edit observation rows — they are immutable by design.
  Corrections are new rows under a new experiment version.
* The experiment can be `CANCELLED` (terminal) but never rewound; a restart of
  the study requires a **new experiment version and a new freeze**.

## 6. Prohibitions until PHASE_2_GO (unchanged, plus explicit list)

* no automatic activation; no freeze reuse; no experiment-ID reuse;
* no historical events as OOS observations;
* no retrospective PnL tuning; no parameter changes after freeze without a new
  version; no execution/expectancy metrics in this study;
* no `systemctl enable/start/restart` of the Phase-2 observer on the
  production VPS before `PHASE_2_GO`;
* no installation of the unit into `/etc/systemd/system` on the production
  VPS as part of development/CI tasks;
* no changes to scanner/paper/live/evaluator, no migrations 074/075 applied by
  this task, no production DB roles or secrets created, no changes to
  `APPROVED_FREEZE_TS`, no activation of the experiment registration.

## 7. Freeze authorities (remediation CP2) and the activation config

Startup is gated on THREE independently maintained freeze values plus the
protocol fingerprint, compared across FOUR applicable holders (record /
database / activation config / running code). Any missing source or pairwise
disagreement refuses startup (fail closed, zero observations):

1. **Approved frozen protocol + operator freeze record** — the FROZEN protocol
   payload (`PROTOCOL_STATUS = "FROZEN"`, pinned `FROZEN_PROTOCOL_FINGERPRINT`)
   carries NO timestamp in Git (issue #197: merge/CI/deploy latency must never
   decide the freeze). The timestamp authority is the immutable, self-hashed
   **freeze record** `/opt/trad_bot/etc/htf_phase2_freeze_record.json`
   (schema `HTF_PHASE2_FREEZE_RECORD_V1`), minted at the GO by
   `tools/research/htf_baseline_short_phase2_freeze_record_v1.py` and passed as
   `--freeze-record` (required together with `--use-registration`; without it
   the gate refuses).
2. **Durable experiment registration** — `freeze_ts` / `protocol_fingerprint`
   in `research.htf_baseline_short_phase2_experiment`, written once by
   `research.htf_baseline_short_phase2_activate(...)` via the `claim` tool
   (§3). `freeze_registry` additionally forbids re-used timestamps.
3. **Runtime activation configuration** — an operator-deployed JSON file, e.g.
   `/opt/trad_bot/etc/htf_phase2_activation.json`:

   ```json
   {
     "experiment_id": "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
     "freeze_ts": "<the same approved ISO timestamp>",
     "protocol_fingerprint": "<64-hex fingerprint of the frozen protocol>"
   }
   ```

   written by `claim` and passed as `--activation-config`.

The three values are maintained by different people/artifacts; nothing copies
one into another automatically at RUNTIME, so a stale or divergent source is
always detectable. The `record_sha256` of the freeze record (recorded in the
change log at mint time) makes any post-approval edit detectable as well.
Record IDENTITY — which of several minted records the claim actually
committed — is bound only by the claim receipt (PR #200 FIX-4): the
`record_sha256` self-hash is tamper evidence and never proof that a record
belongs to the original GO.

## 8. Timestamp skew tolerance (remediation CP1)

`research.htf_baseline_short_phase2_activate` enforces:

* `freeze_ts >= now() - 60 seconds` — retroactive activation is rejected
  (`P2_FREEZE_TS_IN_PAST`); the 60-second window exists solely to absorb
  clock skew between the operator console and the database server;
* `freeze_ts <= now() + 5 minutes` — far-future freezes are rejected
  (`P2_FREEZE_TS_MUST_NOT_BE_FAR_FUTURE`);
* `freeze_ts` must be unclaimed in
  `research.htf_baseline_short_phase2_freeze_registry` (PRIMARY KEY on
  `freeze_ts`) — reusing any previously claimed timestamp, including
  timestamps claimed by other experiment families registered there, fails
  with `P2_FREEZE_TS_ALREADY_CLAIMED`. Timestamps claimed by other families
  must be recorded in this registry at their own activation so the prohibition
  is enforced structurally, not by convention.

## 9. Corrupt checkpoint recovery

`read_checkpoint` is fail-closed: a corrupted, truncated or legacy-schema
checkpoint raises and the runner exits with `EXIT_STATE_ERROR` (4) without
writing anything. Recovery procedure:

1. Verify no observer process is running (`systemctl status`, `ps`).
2. Move the checkpoint file aside (do NOT edit it in place):

   ```bash
   mv /opt/trad_bot/var/htf_phase2/state.json \
      /opt/trad_bot/var/htf_phase2/state.json.broken-$(date +%Y%m%d%H%M%S)
   ```

3. Restart the observer. It replays from the beginning of the available
   source; the durable guarantee makes this safe — already-persisted events
   are suppressed by `UNIQUE (experiment_id, source_event_id)` + `ON CONFLICT
   DO NOTHING` (at-least-once with idempotent writes; exactly-once is NOT
   claimed).
4. If the writer lease file is left over from a crash, takeover happens
   automatically only when the recorded owner PID is verifiably dead; a live
   owner is never stolen from. For unreadable lock metadata, verify manually
   that no writer process exists and delete the `*.lease` file.

## 10. Runtime limitations (updated after issue #184)

The observer reads **closed 5m candles from Bybit** through the explicit
`--candle-source bybit` selection in the unit (issue #181/#184). The former
CSV-only limitation is removed; CSV (`FileCandleSource`, `SYMBOL_5m.csv`)
remains the frozen source for **offline replay and deterministic tests only**
and is still the Python-level default — it is never selected by the unit.

Still NOT part of the deployed observer (unchanged expectations):

* native 1H provenance (current HTF bars are aggregated from 5m;
  `provenance` records this per event);
* dynamic symbol universe management (unit defaults: `BTCUSDT,ETHUSDT`);
* monitoring/alerting wiring and log rotation;
* any paper/live routing — the observer is observe-only by design and by
  import-isolation tests.

The Bybit source itself provides: closed-bar filtering via verified exchange
server-time ASOF, bounded pagination, retry with backoff, causal lookback at
resume, durable cursor-aware backfill and fail-closed quarantine on any
partial/missing 1H hour (resume is automatic once the source is complete).
Public read-only market data only — **no exchange API keys** are used or
required.

Do NOT claim production live-streaming readiness beyond the above without the
remaining integrations and their tests.

## 11. Candle source and effective ExecStart (issue #184)

Effective `ExecStart` of
`deploy/systemd/trad-bot-htf-baseline-short-observer.service`:

```text
ExecStart=/opt/trad_bot/.venv/bin/python -m app.research.htf_baseline_short_phase2_observer_runner_v1 --candle-source bybit --checkpoint /opt/trad_bot/var/htf_phase2/state.json --poll-seconds 300 --use-registration --activation-config /opt/trad_bot/etc/htf_phase2_activation.json --freeze-record /opt/trad_bot/etc/htf_phase2_freeze_record.json --log-level INFO
```

* `--candle-source bybit` — explicit RESEARCH_ONLY source of closed 5m Bybit
  candles. The runner accepts `csv|bybit`; `bybit` constructs
  `BybitPhase2CandleSource`.
* `--data-dir` is intentionally **absent** — it is a CSV-only parameter and is
  not required in Bybit mode. Without it, an accidental removal of
  `--candle-source bybit` fails closed (`"--data-dir is required for
  --candle-source csv"`) instead of silently replaying stale CSV files.
* The CSV default inside Python is unchanged: offline replay and the
  deterministic test matrix keep working exactly as before.
* `--once` is intentionally absent (the runner loops);
  `--use-registration` keeps the THREE freeze authorities gate active;
  `--freeze-record` (issue #197) supplies the operator freeze record — the
  timestamp carrier of authority 1. Without a valid record the gate refuses
  startup (exit 3).
* The runner exit-code contract: `0` OK/stop requested, `3` EXIT_NOT_RUNNABLE
  (fail-closed refusal), `4` EXIT_STATE_ERROR, `5` EXIT_WRITER_ERROR. The unit
  sets `RestartPreventExitStatus=3`, so a refusal never restart-loops.

## 12. Configuration files, credentials and file permissions (issue #184)

All artifacts below are operator-created, never automated, never committed:

| Artifact | Owner / Mode | Content requirements |
| --- | --- | --- |
| `/opt/trad_bot/etc/htf_phase2_observer_enabled` | `tradbot:tradbot` 0600 | empty flag file; exists = allowed to start |
| `/opt/trad_bot/etc/htf_phase2_observer.env` | `tradbot:tradbot` 0600 | `HTF_P2_DB_HOST/PORT/NAME/USER` (dedicated observer role) + `HTF_P2_DB_PASSWORD` or preferably `HTF_P2_DB_PASSWORD_FILE` pointing to a 0600 password file |
| `/opt/trad_bot/secrets/htf_phase2_admin_password` + operator shell env | operator `0600` | the ADMIN channel for `mint`/`claim`/`verify` (issue #205): `HTF_P2_ADMIN_DB_HOST/PORT/NAME/USER` (an operator LOGIN that is a member of `trad_bot_htf_p2_admin`) + `HTF_P2_ADMIN_DB_PASSWORD_FILE` (0600) and preferably `HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER`; never inside the observer unit, never in Git |
| `/opt/trad_bot/etc/htf_phase2_activation.json` | `tradbot:tradbot` 0600 | `experiment_id`, `freeze_ts`, `protocol_fingerprint` (§7) |
| `/opt/trad_bot/etc/htf_phase2_activation.json.claim_receipt.json` | `tradbot:tradbot` 0600 | the claim receipt (schema `HTF_PHASE2_CLAIM_RECEIPT_V1`): binds `record_id` + `record_sha256` of the ONE freeze record the original claim committed; sealed exactly once BEFORE the DB commit and never overwritten (PR #200 FIX-4) |
| `/opt/trad_bot/etc/htf_phase2_freeze_record.json` | `tradbot:tradbot` 0600 | the immutable freeze record (schema `HTF_PHASE2_FREEZE_RECORD_V1`), minted at the GO; its `record_sha256` is recorded in the change log at mint time (§3, §7) |
| `/opt/trad_bot/var/htf_phase2/` (checkpoint, lease) | `tradbot:tradbot` 0700 | created by the operator; `UMask=0077` keeps runtime files private |

Credentials rules (enforced in code, not only by convention):

* only `HTF_P2_DB_*` variables are read — the shared `/opt/trad_bot/.env` is
  **never** loaded and scanner-style variables (`DB_USER`, `DB_PASSWORD`,
  `PGPASSWORD`) can never satisfy the loader;
* `HTF_P2_DB_USER` must name the **dedicated PostgreSQL observer role**;
  shared/admin roles (`postgres`, `trad_bot`, `analytics_*`) are refused;
* a missing password refuses the connection (no trust/peer fallback); the
  password is never logged;
* the freeze tool (`mint`/`claim`/`verify`) reads ONLY `HTF_P2_ADMIN_DB_*`
  (issue #205) — observer credentials are refused by its loader and can never
  open the admin channel; the admin loader likewise never reads `.env` or
  scanner variables and never logs the password;
* the admin role `trad_bot_htf_p2_admin` stays `NOLOGIN` (membership +
  controlled `SET ROLE` only) and the admin tool refuses to run if that role
  has been converted to `LOGIN`, to a superuser, or if observer RBAC has been
  expanded; every session is verified against the real server report
  (`current_database()`, `inet_server_addr()`, `inet_server_port()`,
  `session_user`) before any artifact is written;
* no hardcoded passwords or API keys exist in the unit, the runner or the
  repository; real production secrets must be created only on the VPS, only at
  activation time.

## 13. Effective systemd unit verification (before any start)

Run on the target host (or an isolated Linux environment) and keep the output
in the change log:

```bash
# syntax
systemd-analyze verify deploy/systemd/trad-bot-htf-baseline-short-observer.service

# effective ExecStart and gates as systemd sees them
systemctl cat trad-bot-htf-baseline-short-observer
systemctl show trad-bot-htf-baseline-short-observer -p ExecStart -p Restart -p RestartPreventExitStatus -p ConditionPathExists
```

Cross-check the effective `ExecStart` against §11 and with the CLI:

```bash
/opt/trad_bot/.venv/bin/python -m app.research.htf_baseline_short_phase2_observer_runner_v1 --help
```

If `systemd-analyze verify` cannot be executed in the current environment,
record the step as **`NOT_VERIFIED`** — never as `PASS` — and re-run it on the
target host before activation.

## 14. Fail-closed verification (before creating the flag file)

Expected refusal behavior (deterministic, covered by
`tests/test_htf_baseline_short_phase2_systemd_bybit_wiring_v1.py`):

* missing `/opt/trad_bot/etc/htf_phase2_activation.json` → exit `3`, **zero
  observations**, no database session opened;
* missing/tampered `/opt/trad_bot/etc/htf_phase2_freeze_record.json` → exit
  `3` (the registration gate refuses without the operator freeze record; a
  tampered record is detected by its `record_sha256`);
* any freeze/fingerprint disagreement between the three authorities → exit
  `3`, zero observations;
* missing/unsafe observer credentials → exit `3`;
* without the flag file the unit does not start at all
  (`ConditionPathExists`), so a mere unit installation can never activate the
  observer;
* a refusal never creates the flag file, a freeze timestamp or an activation
  config — those exist only as operator actions.

Manual pre-flight (after the flag file exists, at activation time only):
`systemctl start` once and immediately confirm in the journal that startup
either proceeded past the gate or refused with exit `3` — a refusal is the
expected safe outcome and requires operator action, not retries.

## 15. Safe future deploy sequence (summary)

1. PR merged to `main` (no activation side effects), CI `Main PR Safety` green.
2. VPS: `git fetch && git switch main && git pull --ff-only origin main`.
3. VPS: install the unit file into `/etc/systemd/system/` **without**
   `systemctl enable`, then `systemctl daemon-reload`.
4. VPS: `systemd-analyze verify` + `systemctl show` checks (§13); record
   `NOT_VERIFIED` honestly if any check cannot run.
5. Operator prepares the env file and directory modes (§12). The activation
   config and the freeze record are produced by `mint`/`claim` at the GO (§3),
   never in advance.
6. Fail-closed checks (§14) with the flag file **absent** — the unit must not
   start.
7. `PHASE_2_GO` decision, then — and only then — the activation sequence (§3).
8. Emergency stop touches **only** `trad-bot-htf-baseline-short-observer`
   (§5); scanner/paper/evaluator are never restarted as part of observer
   operations.
