# HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1

**Status: FUTURE USE ONLY. Nothing in this runbook was executed. Phase 2 is not
activated. This document exists so that a future, separately authorized
activation is explicit, ordered and auditable.**

Every step below requires explicit operator instruction. No step is automated,
none is part of CI, merge or deployment.

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

1. Apply any approved parameter decisions to
   `app/research/htf_baseline_short_phase2_protocol_v1.py` **locally**, run the
   full test matrix, and commit.
2. Compute and record the fingerprint:

   ```bash
   python -c "from app.research.htf_baseline_short_phase2_protocol_v1 import protocol_fingerprint; print(protocol_fingerprint())"
   ```

3. Record `FROZEN + <commit sha> + <fingerprint> + <operator> + <date>` in the
   change log. The protocol is now frozen; any later change needs a new
   version, a new experiment version and a new freeze.

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
issued.** The section records the future, ordered procedure.

Freeze timestamp selection rules:

* choose a **fresh** UTC timestamp at/after the moment activation readiness is
  reached — **never** `2026-10-06T14:00:00Z` (Point-B), never a past timestamp;
* `signal_time > freeze_ts` and `first_touch_time > freeze_ts` are strict;
* everything at or before the freeze is ineligible forever.

1. Prepare the two operator-maintained configuration artifacts (§12) — the
   dedicated secrets file `htf_phase2_observer.env` and the runtime activation
   config `htf_phase2_activation.json`. Neither is ever generated
   automatically; neither may be committed to Git.
2. Activate the experiment (sets status RUNNING, freeze, fingerprint — still
   `observer_enabled = FALSE`):

   ```sql
   SELECT research.htf_baseline_short_phase2_activate(
       'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1',
       '<FREEZE_TS>', '<FROZEN_FINGERPRINT>');
   ```

3. Copy the same `<FREEZE_TS>` and `<FROZEN_FINGERPRINT>` into the runtime
   activation config used by the observer so all three freeze sources agree
   (protocol constant / DB registration / activation config). Any mismatch
   makes the runner fail closed (exit 3, no observations).
4. Enable the observer (still a separate action):

   ```sql
   UPDATE research.htf_baseline_short_phase2_experiment
      SET observer_enabled = TRUE
    WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
   ```

5. Verify the effective systemd unit (§13) and run the fail-closed checks
   (§14) **before** creating the flag file.
6. Create the unit gate flag file (never automated; the LAST authorization
   step before start):

   ```bash
   install -d /opt/trad_bot/etc && touch /opt/trad_bot/etc/htf_phase2_observer_enabled
   ```

7. Start the service **disabled-by-default unit** — and only this unit:

   ```bash
   systemctl daemon-reload
   systemctl start trad-bot-htf-baseline-short-observer
   systemctl status trad-bot-htf-baseline-short-observer --no-pager
   ```

   Never `systemctl enable` this unit. It is started manually, per activation,
   and must remain start-on-demand only.

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

Startup is gated on THREE independently maintained freeze values plus two
fingerprints. Any missing source or pairwise disagreement refuses startup
(fail closed, zero observations):

1. **Approved frozen protocol** — `APPROVED_FREEZE_TS` in
   `app/research/htf_baseline_short_phase2_protocol_v1.py`, set in the commit
   that freezes the protocol (operator action).
2. **Durable experiment registration** — `freeze_ts` / `protocol_fingerprint`
   in `research.htf_baseline_short_phase2_experiment`, written once by
   `research.htf_baseline_short_phase2_activate(...)` (§3).
3. **Runtime activation configuration** — an operator-deployed JSON file, e.g.
   `/opt/trad_bot/etc/htf_phase2_activation.json`:

   ```json
   {
     "experiment_id": "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1",
     "freeze_ts": "<the same approved ISO timestamp>",
     "protocol_fingerprint": "<64-hex fingerprint of the frozen protocol>"
   }
   ```

   passed as `--activation-config` to the observer (required together with
   `--use-registration`).

The three values are maintained by different people/artifacts; nothing copies
one into another automatically, so a stale or divergent source is always
detectable.

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
ExecStart=/opt/trad_bot/.venv/bin/python -m app.research.htf_baseline_short_phase2_observer_runner_v1 --candle-source bybit --checkpoint /opt/trad_bot/var/htf_phase2/state.json --poll-seconds 300 --use-registration --activation-config /opt/trad_bot/etc/htf_phase2_activation.json --log-level INFO
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
  `--use-registration` keeps the THREE freeze authorities gate active.
* The runner exit-code contract: `0` OK/stop requested, `3` EXIT_NOT_RUNNABLE
  (fail-closed refusal), `4` EXIT_STATE_ERROR, `5` EXIT_WRITER_ERROR. The unit
  sets `RestartPreventExitStatus=3`, so a refusal never restart-loops.

## 12. Configuration files, credentials and file permissions (issue #184)

All artifacts below are operator-created, never automated, never committed:

| Artifact | Owner / Mode | Content requirements |
| --- | --- | --- |
| `/opt/trad_bot/etc/htf_phase2_observer_enabled` | `tradbot:tradbot` 0600 | empty flag file; exists = allowed to start |
| `/opt/trad_bot/etc/htf_phase2_observer.env` | `tradbot:tradbot` 0600 | `HTF_P2_DB_HOST/PORT/NAME/USER` (dedicated observer role) + `HTF_P2_DB_PASSWORD` or preferably `HTF_P2_DB_PASSWORD_FILE` pointing to a 0600 password file |
| `/opt/trad_bot/etc/htf_phase2_activation.json` | `tradbot:tradbot` 0600 | `experiment_id`, `freeze_ts`, `protocol_fingerprint` (§7) |
| `/opt/trad_bot/var/htf_phase2/` (checkpoint, lease) | `tradbot:tradbot` 0700 | created by the operator; `UMask=0077` keeps runtime files private |

Credentials rules (enforced in code, not only by convention):

* only `HTF_P2_DB_*` variables are read — the shared `/opt/trad_bot/.env` is
  **never** loaded and scanner-style variables (`DB_USER`, `DB_PASSWORD`,
  `PGPASSWORD`) can never satisfy the loader;
* `HTF_P2_DB_USER` must name the **dedicated PostgreSQL observer role**;
  shared/admin roles (`postgres`, `trad_bot`, `analytics_*`) are refused;
* a missing password refuses the connection (no trust/peer fallback); the
  password is never logged;
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
5. Operator prepares the env file, activation config and directory modes
   (§12).
6. Fail-closed checks (§14) with the flag file **absent** — the unit must not
   start.
7. `PHASE_2_GO` decision, then — and only then — the activation sequence (§3).
8. Emergency stop touches **only** `trad-bot-htf-baseline-short-observer`
   (§5); scanner/paper/evaluator are never restarted as part of observer
   operations.
