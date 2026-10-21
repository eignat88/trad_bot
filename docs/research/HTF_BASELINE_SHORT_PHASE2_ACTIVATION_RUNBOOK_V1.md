# HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1

**Status: FUTURE USE ONLY. Nothing in this runbook was executed. Phase 2 is not
activated. This document exists so that a future, separately authorized
activation is explicit, ordered and auditable.**

Every step below requires explicit operator instruction. No step is automated,
none is part of CI, merge or deployment.

---

## 0. Preconditions (all must hold)

1. PR for `feature/htf-issue176-phase2-runtime-repair-v1` reviewed and merged
   to `main` (merge **must not** activate anything).
2. Repeat readiness audit (Gate A–K) completed with verdict
   `READY_FOR_OPERATOR_REVIEW` — and separately approved.
3. Protocol `HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1` reviewed; the six
   `OPERATOR_APPROVAL_REQUIRED` fields decided; the approved fingerprint
   recorded.
4. Deployment `main → VPS` performed through the normal pipeline; VPS working
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

Freeze timestamp selection rules:

* choose a **fresh** UTC timestamp at/after the moment activation readiness is
  reached — **never** `2026-10-06T14:00:00Z` (Point-B), never a past timestamp;
* `signal_time > freeze_ts` and `first_touch_time > freeze_ts` are strict;
* everything at or before the freeze is ineligible forever.

1. Activate the experiment (sets status RUNNING, freeze, fingerprint — still
   `observer_enabled = FALSE`):

   ```sql
   SELECT research.htf_baseline_short_phase2_activate(
       'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1',
       '<FREEZE_TS>', '<FROZEN_FINGERPRINT>');
   ```

2. Copy the same `<FREEZE_TS>` and `<FROZEN_FINGERPRINT>` into the registry
   entry used by the observer runtime so all three freeze sources agree. Any
   mismatch makes the runner fail closed (exit 3, no observations).
3. Enable the observer (still a separate action):

   ```sql
   UPDATE research.htf_baseline_short_phase2_experiment
      SET observer_enabled = TRUE
    WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
   ```

4. Create the unit gate flag file (never automated):

   ```bash
   install -d /opt/trad_bot/etc && touch /opt/trad_bot/etc/htf_phase2_observer_enabled
   ```

5. Start the service **disabled-by-default unit**:

   ```bash
   systemctl daemon-reload
   systemctl start trad-bot-htf-baseline-short-observer
   systemctl status trad-bot-htf-baseline-short-observer --no-pager
   ```

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

## 5. Rollback / emergency stop

* Emergency stop (observation writes stop immediately):

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

## 6. Prohibitions (unchanged)

* no automatic activation; no freeze reuse; no experiment-ID reuse;
* no historical events as OOS observations;
* no retrospective PnL tuning; no parameter changes after freeze without a new
  version; no execution/expectancy metrics in this study.

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

## 10. Runtime limitations (remediation CP5) — CSV-only source

The observer currently reads candles from CSV files (`SYMBOL_5m.csv`) via
`FileCandleSource`. It proves the pipeline but is **not** a live-streaming
production observer. Missing before live operation:

* a live Bybit kline source (pagination, backfill, rate limits, forming-bar
  filtering, exchange-clock skew handling);
* gap policy for live streams (the runner QUARANTINES on any partial/missing
  1H hour and resumes automatically when the source is completed);
* native 1H provenance (current HTF bars are aggregated from 5m;
  `provenance` records this per event);
* dynamic symbol universe management;
* `HTF_P2_DB_*` environment mapping for the deployed `.env` (the unit's
  `EnvironmentFile` must provide `HTF_P2_DB_HOST/PORT/USER/NAME`);
* monitoring/alerting wiring and log rotation.

Do NOT claim production live-streaming readiness without those integrations
and their tests.
