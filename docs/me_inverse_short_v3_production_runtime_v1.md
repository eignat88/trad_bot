# ME_LONG_V2_INVERSE_SHORT_V3 — production runtime V1 (Issue #285)

**Scope:** `ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1` — the V3
successor contour of `ME_LONG_V2_INVERSE_SHORT_V2`: the SAME frozen
detector (`MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` / `LONG`, inverse `SHORT`),
the SAME frozen execution geometry (TP/SL 10%, 7200 min, `ENTRY_TTL` 120
min, paper sizing 10 000 / 0.5% / 20%, fees 0.00055, slippage 0.0005,
`STOP_FIRST`) and the NEW entry guards and capacity (Issue #285):

* **12 positions counted over BOTH contours** — `OPEN V2 + OPEN V3 <= 12`;
* **1 OPEN position per symbol over BOTH contours** (enforced by a partial
  `UNIQUE (symbol) WHERE status = 'OPEN'` index inside V3 + READ-ONLY checks
  of the V2 ledger at admission time);
* **12h per-symbol entry cooldown** from the last COMMITTED `entered_at`
  (V2 or V3, ANY status — a CLOSED position keeps cooling its symbol down;
  refused signals never write `entered_at` and never extend the timer);
* **V2→V3 drain semantics** — V3 admits new entries ONLY while V2 itself
  admits none (`DRAINING` / `CANCELLED`); an unreadable or unexpected V2
  state FAILS CLOSED for entries and NEVER for exits.

This contour is paper-only and prospective-only. It never places real
orders, never writes `dds.paper_trade`, and never changes the ME LONG V2
detector, gates, scoring, regime, source, or any V2 / OOS-2 state.

**Identity**

| Item | Value |
| --- | --- |
| strategy / experiment id | `ME_LONG_V2_INVERSE_SHORT_V3` |
| protocol version | `3.0.0` |
| protocol fingerprint | `c10eb00f2777c2ca9b42d465ccbad4dc7908d86676b678fddcff5722f6f80aa4` (pinned; guard parameters 12/True/12 are part of it) |
| migration | `sql/migrations/090_me_long_v2_inverse_short_v3.sql` |
| tables | `dds.me_long_v2_inverse_short_v3_{activation,event,position,close_outbox}` |
| runtime flag (first factor) | `ME_INVERSE_SHORT_V3_ENABLED` |
| source-capture error sink | `ME_INVERSE_SHORT_V3_SOURCE_CAPTURE_ERROR` |
| release path | `/opt/trad_bot_me_inverse_short_v3_release` |
| systemd unit | `deploy/systemd/trad-bot-me-inverse-short-v3-runtime.service` |
| env file | `/etc/trad-bot/me-inverse-short-v3-runtime.env` (`deploy/env/me-inverse-short-v3-runtime.env.example`) |

---

## 1. Deployment sequence (all steps are separate, explicit approvals)

1. **Install the release tree** at `/opt/trad_bot_me_inverse_short_v3_release`
   (own tree — never the scanner's, never the V1/V2/OOS-2 trees) with its own
   `.venv`; create `logs/` and `var/` owned `tradbot:tradbot`, mode 0750.
2. **Apply migration 090** (separately approved DB change). The migration is
   additive and V3-only: 4 new tables, guard indexes, immutability trigger,
   one inert `READY_TO_START` seed row. It is idempotent
   (`CREATE ... IF NOT EXISTS`) and never touches V2 / OOS-2 / production
   tables. Verify with:
   `python scripts/me_inverse_short_v3_preflight.py --env-file /etc/trad-bot/me-inverse-short-v3-runtime.env`
3. **Install the env file** (mode 0640, `root:tradbot`) with
   `ME_INVERSE_SHORT_V3_ENABLED=0`. Starting the service now performs NO
   captures, NO entries, NO writes beyond telemetry.
4. **Run the preflight** (section 2). Every check must be PASS/WARN/SKIP.
5. **Drain V2 first** (issue contract): V2 → `DRAINING` via the V2 tooling
   (`scripts/me_inverse_short_v2_activate.py --drain`). The V3 activation
   tool REFUSES `--activate` while V2 still admits.
6. **Fix the activation** (separately approved):
   `python scripts/me_inverse_short_v3_activate.py --activate --freeze-ts <FRESH_UTC> --env-file ...`
   — writes ONLY `dds.me_long_v2_inverse_short_v3_activation` (status
   `RUNNING` + pinned fingerprint + `freeze_ts`, atomically). `FRESH_UTC`
   must be within 2 minutes of execution; naive/future timestamps refused.
   Only source events strictly AFTER `freeze_ts` can ever be captured
   (prospective-only boundary, enforced by SQL CHECK).
7. **Enable the flag** (`ME_INVERSE_SHORT_V3_ENABLED=1`) and start the unit.
   Order matters: row first (frozen), flag second.

Rollback at any point before step 6 is trivial (nothing is active). After
step 6 see section 5.

## 2. Preflight

`scripts/me_inverse_short_v3_preflight.py` is read-only and exits non-zero
on any FAIL:

* static: migration 090 present/newest/unique, strict guard config equals
  the frozen `12/True/12`, fingerprint pinned, geometry/TTL/fees parity with
  V2, `scanner_runner` delivers source events to the V3 hook
  (`SOURCE_CAPTURE_READY`), deployment artifacts present, unit/tools carry
  V3-only identity;
* database (with `--env-file`): migration 090 objects + guard indexes +
  trigger, activation state fails closed (or matches the pinned
  fingerprint), V2 precondition (`DRAINING`/`CANCELLED`), `OPEN V2 + OPEN
  V3 <= 12`, legacy V2 duplicate OPEN rows reported READ-ONLY.

`--json` gives machine-readable output. DB checks never write.

## 3. Runtime behaviour and telemetry

* **Entrypoint:** `python -m app.paper.me_long_v2_inverse_short_v3.production_runtime --interval 10`
  (single instance enforced by a lock file under `var/`; exit code 3 = another
  instance).
* **Logs:** journald (`SyslogIdentifier=trad-bot-me-inverse-short-v3-runtime`)
  plus `logs/` in the release tree. Look for
  `me inverse short v3 runtime starting: ... guards={'max_open_positions': 12,
  'single_active_symbol': True, 'symbol_entry_cooldown_hours': 12}` and the
  admission-refusal lines (`refused: SYMBOL_ALREADY_OPEN` / `SYMBOL_COOLDOWN_12H`
  / `deferred: ENTRY_CAP` / `fail closed`).
* **Admission telemetry:** every cycle's `engine.snapshot()["entry_guards"]`
  (published in the runtime heartbeat) carries the cross-contour state
  (`open_total`, `v2_status`) and per-outcome counters (`admitted`,
  `adopted`, `refused_symbol_open`, `refused_cooldown`, `deferred_cap`,
  `deferred_peer`, `not_admissible`) — export/plot these.
* **Grafana (suggested panels):** OPEN V2+V3 vs cap 12; admissions vs
  refusals by reason; per-symbol `entered_at` age vs the 12h boundary;
  `CAPTURED` event backlog age vs `ENTRY_TTL` 120 min; V2 activation status.
  Base query set:
  ```sql
  SELECT (SELECT COUNT(*) FROM dds.me_long_v2_inverse_short_v3_position WHERE status='OPEN')
       + (SELECT COUNT(*) FROM dds.me_long_v2_inverse_short_v2_position WHERE status='OPEN') AS open_total;
  SELECT status_reason, COUNT(*) FROM dds.me_long_v2_inverse_short_v3_event
   WHERE status IN ('SYMBOL_REJECTED','CAPACITY_REJECTED','EXPIRED')
   GROUP BY 1;
  ```
* **Source capture:** the scanner delivers pre-gate source events to the
  `_capture_me_inverse_short_v3_source` hook (fail-open; own flag, own store
  connection, own `ME_INVERSE_SHORT_V3_SOURCE_CAPTURE_ERROR` sink). Runtime
  proof: `scanner_runner.me_inverse_short_v3_source_wiring()`.

## 4. Entry-guard operational semantics (frozen contract)

| Situation | Outcome | Event disposition |
| --- | --- | --- |
| same symbol already OPEN (V2 or V3) | refuse (terminal) | `SYMBOL_REJECTED` / `SYMBOL_ALREADY_OPEN` |
| last committed `entered_at` (V2 or V3, any status) within 12h | refuse (terminal) | `SYMBOL_REJECTED` / `SYMBOL_COOLDOWN_12H` |
| exactly 12h since last `entered_at` | **admissible** (boundary) | — |
| `OPEN V2 + OPEN V3 >= 12` | defer (retry) | stays `CAPTURED`, disposed at TTL as `CAPACITY_REJECTED` / `ENTRY_CAP` |
| event age ≥ 120 min | refuse (terminal) | `EXPIRED` / `ENTRY_TTL` (or `CAPACITY_REJECTED` when the cap was binding) |
| V2 state unreadable / not `DRAINING`/`CANCELLED` | fail closed for entries | stays `CAPTURED` (TTL sweep owns the fate) |
| any of the above for an OPEN position's exit path | **exits unaffected** | — |

The decision is taken in ONE serialized transaction (fixed lock order:
advisory admission lock → event row → activation row) and judged at
`statement_timestamp()` — the DB-side clock, not the client's.

## 5. Rollback / stop

**O1 — stop entries only (soft):** `ME_INVERSE_SHORT_V3_ENABLED=0` and/or
`python scripts/me_inverse_short_v3_activate.py --drain` (RUNNING → DRAINING,
EXIT_ONLY: existing positions still manage their exits). No V2/OOS-2 impact.

**O2 — full stop:** `systemctl stop trad-bot-me-inverse-short-v3-runtime`.
Restart-safe: OPEN positions restore from the DB with verbatim
`entered_at`/`deadline_at`; durable close snapshots replay exactly
(`close_outbox`).

**O3 — schema rollback (destructive, separate approval only):** drop ONLY
the four `dds.me_long_v2_inverse_short_v3_*` tables. V2/OOS-2/production
data is untouched by construction (migration 090 is additive).

V2 is never stopped, rolled back or modified by any V3 rollback step.

## 6. Isolation guarantees (acceptance)

* migration 090 never ALTERs/DROPs/writes V2 / OOS-2 / V1 / production /
  research tables, rows, fingerprints or freeze timestamps;
* the V3 admission reads `dds.me_long_v2_inverse_short_v2_position` /
  `..._activation` READ-ONLY (SELECT-only SQL, asserted by tests);
* V2 legacy duplicate OPEN rows may run out and can never be re-created
  (partial UNIQUE index); the V2 loader holds the `23505` seam if/when the
  V2 fork is ever hardened;
* V2/OOS-2 suites are untouched by this issue's changes (regression
  baseline asserted by failing-ID set equality).
