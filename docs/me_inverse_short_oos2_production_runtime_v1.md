# ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — Production Runtime Runbook

Standalone, systemd-managed service that keeps the **frozen inverse-SHORT
paper contour (OOS-2, Issue #276)** running on real Bybit market data —
with activation **OFF**, no backfill and no trading side effects.

```
python -m app.paper.me_long_v2_inverse_short_oos2_v1.production_runtime
```

> **Scope of this runbook.** Installing the service inert (flag `0`) and
> verifying it is routine. Everything from migration 088 on the production
> database onwards is a **separately approved decision**: each step below
> carries explicit **STOP** conditions. This document never authorizes
> activation, Scanner changes, VPS deployment or any production write.

> **Forbidden production actions for THIS task:** no migration is applied
> to any database by this task, no activation is performed, and no
> freeze (`freeze_ts`) change is made — migration 088, the activation step
> and any freeze operation each require their own explicit, separate
> approval. Everything below documents the flow; it does not grant it.

---

## 1. Architecture

```
trad-bot-me-inverse-short-oos2-runtime.service   (systemd, User=tradbot)
        │  EnvironmentFile=/etc/trad-bot/me-inverse-short-oos2-runtime.env
        ▼
production_runtime.main()   (module app.paper.me_long_v2_inverse_short_oos2_v1)
        ├── RuntimeInstanceLock       ← OS lock: exactly ONE process (exit 3)
        ├── ensure_writable(log/heartbeat/lock dirs)  ← fail fast
        ├── connect_pg_store()        ← DEDICATED PostgreSQL connection
        └── ProductionRuntime.run()   ← loop, SIGTERM, telemetry, heartbeat
                 └── InverseShortContour.run_cycle(...)   (frozen OOS-2 spec)
                          ├── refresh_activation()   (two-factor, fail closed)
                          ├── engine.check_exits()   (STOP → partial TP ladder → TIMEOUT …)
                          ├── engine.open_entries()  ← admission gate (TTL + capacity)
                          └── engine.mark_to_market()
```

**Two-factor activation (unchanged concept, OOS-2 surface):**

1. runtime flag `ME_INVERSE_SHORT_OOS2_ENABLED=1` **and**
2. activation row `RUNNING` whose `protocol_fingerprint` equals
   `protocol_fingerprint()` (pinned `1d1e7b4d87c6125183887c8f054a4edb1ff3f0711e70da1feeb9b7e249d7b7ec`)
   with a non-NULL `freeze_ts`.

`DRAINING` = EXIT_ONLY: manage existing positions, never open new ones.

**Frozen OOS-2 trading spec (this contour's identity):**

* **Capacity = 12** concurrent open positions — the OOS-2-LOCAL constant
  `protocol.MAX_OPEN_POSITIONS = 12`, **NOT** `Settings.max_open_positions`
  (the repository-wide setting is deliberately not consulted; capacity
  refusals are recorded as `CAPACITY_REJECTED` with `status_reason='ENTRY_CAP'`).
* **Entry signal TTL: 120 minutes** (`ENTRY_MAX_AGE_MINUTES`, evaluated at
  the **database** clock by `INSERT_POSITION_SQL`, so a TTL that elapses
  between the Python check and the INSERT is still refused); expired events
  are terminally `EXPIRED` (`status_reason='ENTRY_TTL'`).
* **Max hold: 7200 minutes** (`MAX_HOLD_MINUTES`) → `TIMEOUT` terminal exit.
* **Partial TP ladder (three stages, each leg fires AT MOST ONCE):**
  * TP1 **+5 %** closes **50 % of the ORIGINAL quantity** (position stays
    OPEN, keeps its capacity slot; remainder = 50 % of original);
  * TP2 **+7 %** closes **75 % of the THEN-REMAINING quantity = 37.5 % of
    the original** (remainder = 12.5 % of original);
  * TP3 **+10 %** closes **all remaining = 12.5 % of the original**;
  * SL **−10 %** closes **all remaining** quantity;
  * `INTRABAR_POLICY = "STOP_FIRST"`: if the stop is touched in the same
    bar as any TP, the remaining quantity closes at the stop.
* Every executed leg (partial or terminal) is recorded in the **new**
  `dds.me_long_v2_inverse_short_oos2_v1_close_leg` ledger
  (`PRIMARY KEY (position_key, leg_seq)`, exactly-once via
  `ON CONFLICT DO NOTHING`); the terminal leg is staged in the same
  transaction as `dds.me_long_v2_inverse_short_oos2_v1_close_outbox`.

**Event source:** the pre-gate `_me_inverse_short_source_events`
publication made by `ScannerOrchestrator.scan_all_with_stats` **before**
the direction gate / expectancy filter, delivered by the production
scanner loop to `_capture_me_inverse_short_oos2_source`
(`ROUTE_PRE_FILTER_SOURCE`). The OOS-2 capture is independent of V1 and V2:
own flag (`ME_INVERSE_SHORT_OOS2_ENABLED`), own store connection, own
error type (`ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR`), fail-open.
The V1 hook `_capture_me_inverse_short_source` is preserved untouched.

**Entry admission (database-enforced):** `INSERT_POSITION_SQL` itself
requires — inside one PostgreSQL statement with row locks
(`FOR UPDATE OF ev, act`, `ON CONFLICT … DO NOTHING`):

* `statement_timestamp() - signal_time < 120 minutes` (the frozen TTL);
* event `CAPTURED`, consistent fingerprint/activation identity, strict
  `signal_time > freeze_ts`, activation `RUNNING`, no existing position.

TTL expiry and capacity disposal race the admission through row locks;
exactly one valid outcome results (never EXPIRED-with-position).

**Isolation:** the OOS-2 release tree, lock file
(`var/me_inverse_short_oos2_runtime.lock`), heartbeat
(`var/me_inverse_short_oos2_runtime_heartbeat.json`), log
(`logs/me_inverse_short_oos2_runtime.log`), env file and unit are all
distinct from the V1 AND the V2 runtime — the preflight's
`V1_ISOLATION_VALID` check compares the unit files directive by
directive, and this contour never shares a lock, table or flag with them.

### Exit codes (the unit relies on this contract)

| Code | Meaning | systemd |
|------|---------|---------|
| 0 | clean stop (`--once`, SIGTERM/SIGINT, `--max-cycles`) | no restart |
| 1 | fatal startup/runtime error | `Restart=on-failure` |
| 2 | CLI usage error | no restart |
| 3 | another instance holds the lock | `RestartPreventExitStatus=3` |

---

## 2. Components reused (nothing re-implemented)

| Component | Source |
|---|---|
| `InverseShortContour.run_cycle()` ordering | `app/paper/me_long_v2_inverse_short_oos2_v1/runner.py` |
| Engine: three-stage partial TP ladder (TP1 +5 % / TP2 +7 % / TP3 +10 %), SL −10 %, 120 min entry TTL, 7200 min hold, STOP_FIRST, capacity 12 (local), fees/slippage/funding, close-leg ledger, outbox, reconciliation | `engine.py` (frozen spec) |
| Two-factor activation (`ME_INVERSE_SHORT_OOS2_ENABLED` + RUNNING row) | `activation.py` |
| Prospective-only capture + route constants | `capture.py` + `scanner_runner.py` |
| Frozen protocol + fingerprint (capacity 12 = `MAX_OPEN_POSITIONS`) | `protocol.py` |
| PostgreSQL store, admission gate, reconnect/ambiguous-COMMIT adoption | `store.py` |
| Source-path audit (static, shared with preflight) | `source_path.py` |
| Dedicated PG store with reconnect | `store.connect_pg_store` |
| Settings / Bybit client / rate limiter | `app.config`, `app.exchange.bybit_client` (unchanged) |

---

## 3. Installation — 12 steps with STOP conditions

### Step 1 — Pin the reviewed release

```bash
sudo mkdir -p /opt/trad_bot_me_inverse_short_oos2_release
sudo git -C /opt/trad_bot_me_inverse_short_oos2_release clone <repo-url> .
sudo git -C /opt/trad_bot_me_inverse_short_oos2_release checkout <reviewed-tag-or-commit>
```

* **STOP** if the commit is not the one named in the accepted code review,
  if `git status` is dirty, or if the reviewed fingerprint pin
  (`PINNED_PROTOCOL_FINGERPRINT` in the preflight) does not match
  `python -c "from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import
  protocol_fingerprint; print(protocol_fingerprint())"` from that tree.

### Step 2 — Python environment of this release

```bash
sudo python3 -m venv /opt/trad_bot_me_inverse_short_oos2_release/.venv
sudo /opt/trad_bot_me_inverse_short_oos2_release/.venv/bin/pip install -r \
    /opt/trad_bot_me_inverse_short_oos2_release/requirements.txt
sudo /opt/trad_bot_me_inverse_short_oos2_release/.venv/bin/pip check
```

* The unit's `ExecStart` uses exactly this interpreter.
* **STOP** if `pip check` reports conflicts — notably `scramp==1.5.0`
  breaks PostgreSQL connectivity (`AttributeError: make_channel_binding`);
  the runtime needs `pg8000==1.31.5` + `scramp==1.4.18`. Never modify the
  Scanner / Paper / Research Evaluator environments.

### Step 3 — Writable runtime/log directories

```bash
sudo mkdir -p /opt/trad_bot_me_inverse_short_oos2_release/{logs,var}
sudo chown -R tradbot:tradbot /opt/trad_bot_me_inverse_short_oos2_release/{logs,var}
sudo chmod 0750 /opt/trad_bot_me_inverse_short_oos2_release/{logs,var}
sudo -u tradbot touch /opt/trad_bot_me_inverse_short_oos2_release/logs/.write_probe \
  && sudo -u tradbot rm /opt/trad_bot_me_inverse_short_oos2_release/logs/.write_probe \
  && echo "logs dir writable OK"
```

* These paths are **OOS-2-only** — never the V1 tree
  `/opt/trad_bot_me_inverse_short_release` and never the V2 tree
  `/opt/trad_bot_me_inverse_short_v2_release`.
* **STOP** if the write probe fails (the runtime exits 1 at startup —
  the `PermissionError` lesson from the original deployment).

### Step 4 — Migration 088 (production database — separate approval)

```bash
# Review first (read-only):
sudo -u postgres psql -d trad_bot -c \
  "SELECT to_regclass('dds.me_long_v2_inverse_short_oos2_v1_activation'),
          to_regclass('dds.me_long_v2_inverse_short_oos2_v1_event'),
          to_regclass('dds.me_long_v2_inverse_short_oos2_v1_close_leg');"

# Apply ONLY under an approved migration window, in numeric order:
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 -f \
    sql/migrations/088_me_long_v2_inverse_short_oos2_v1.sql

# Verify:
sudo -u postgres psql -d trad_bot -c \
  "SELECT experiment_id, status, protocol_fingerprint, freeze_ts
     FROM dds.me_long_v2_inverse_short_oos2_v1_activation;"
# expect exactly one row: ME_LONG_V2_INVERSE_SHORT_OOS2_V1 / READY_TO_START / NULL / NULL
```

* Migration 088 is transactional (`BEGIN … COMMIT`) and idempotent-safe to
  inspect: it creates the `activation`/`event`/`position`/`close_leg`/
  `close_outbox` tables with the four-state event CHECK, the
  `signal_time > activation_freeze_ts` CHECK, the namespaced position
  (`ME_LONG_V2_INVERSE_SHORT_OOS2_V1:<event_key>`), the partial-ladder
  invariants (`tp2 => tp1`, `tp3 => tp2`, remainder 100 % / 50 % / 12.5 %),
  the durable close-leg ledger
  (`dds.me_long_v2_inverse_short_oos2_v1_close_leg`, exactly-once per
  `(position_key, leg_seq)`), and the immutability trigger on entry facts.
* **STOP** if the migration is not the next unapplied number, if the
  activation row is not `READY_TO_START`, if any OOS-2 table already exists
  with data, or if the approved window has not been granted. **Applying
  migration 088 to production is never part of an install-only pass — and
  this task applies NO migration at all.**
* **STOP** if migration 085/086 (V1) or 087 (V2) are absent — 088 does not
  replace them and V1/V2 must remain untouched.

### Step 5 — Runtime environment file (activation OFF)

```bash
sudo mkdir -p /etc/trad-bot
sudo cp deploy/env/me-inverse-short-oos2-runtime.env.example \
    /etc/trad-bot/me-inverse-short-oos2-runtime.env
sudo chmod 0640 /etc/trad-bot/me-inverse-short-oos2-runtime.env
sudo chown root:tradbot /etc/trad-bot/me-inverse-short-oos2-runtime.env
sudo $EDITOR /etc/trad-bot/me-inverse-short-oos2-runtime.env   # DB creds; keep ENABLED=0
```

* **STOP** if the file contains any `ME_INVERSE_SHORT_V1_*` key (the
  preflight fails `V1_ISOLATION_VALID`), if `ME_INVERSE_SHORT_OOS2_ENABLED`
  is not `0`, or if the file is world-readable.

### Step 6 — Scanner-side environment (the SECOND process)

The capture hook runs **inside the production Scanner process**
(`EnvironmentFile=/opt/trad_bot/.env` per
`deploy/systemd/trad-bot-scanner.service`). The runtime's flag alone
never enables capture.

```bash
# Append the fragment (deploy/env/me-inverse-short-scanner.env.example):
#   ME_INVERSE_SHORT_OOS2_ENABLED=0
#   DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD  == runtime env file, verbatim
# (the fragment is SHARED: the pre-existing ME_INVERSE_SHORT_V2_ENABLED=0
#  line stays exactly as it is — only the OOS-2 flag line is appended)
```

* **STOP** if the Scanner's DB tuple differs from the runtime's (the
  preflight fails `OOS2_SOURCE_CAPTURE_READY` → `SCANNER_RUNTIME_DB_MISMATCH`), if
  `ME_INVERSE_SHORT_OOS2_ENABLED` is `1` at preflight time, or if you would
  need to restart the Scanner to add the key — a Scanner restart requires
  its own explicit approval and is **not** part of this install.

### Step 7 — Install the systemd unit (starts inert)

```bash
sudo cp deploy/systemd/trad-bot-me-inverse-short-oos2-runtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trad-bot-me-inverse-short-oos2-runtime.service
```

* **STOP** if `ConditionPathExists` fails (wrong release path), if the
  unit refuses the environment file, or if the service lands in a restart
  loop (check `journalctl -u … -n 50`; exit 3 means a stray manual
  instance holds the lock — never restart-loop it).

### Step 8 — Verify the inert runtime (flag OFF, zero writes)

```bash
systemctl status trad-bot-me-inverse-short-oos2-runtime.service
journalctl -u trad-bot-me-inverse-short-oos2-runtime.service -n 20 --no-pager \
  | grep me_inverse_short_runtime_cycle

# Heartbeat (mtime older than a few intervals while "active = loop stuck):
stat -c '%y %n' /opt/trad_bot_me_inverse_short_oos2_release/var/me_inverse_short_oos2_runtime_heartbeat.json

# Expected with activation OFF: entries=0, entry_refusals contains
# ACTIVATION:DISABLED_FLAG, and NO rows written:
sudo -u postgres psql -d trad_bot -c \
  "SELECT status, protocol_fingerprint, freeze_ts
     FROM dds.me_long_v2_inverse_short_oos2_v1_activation;
   SELECT count(*) FROM dds.me_long_v2_inverse_short_oos2_v1_position;
   SELECT count(*) FROM dds.me_long_v2_inverse_short_oos2_v1_close_leg;
   SELECT status, count(*) FROM dds.me_long_v2_inverse_short_oos2_v1_event
     GROUP BY status;"
```

* **STOP** if any OOS-2 event/position/close-leg/outbox row exists, if the
  activation row is not `READY_TO_START` with NULL fingerprint/freeze, if
  telemetry reports `activated=true`, or if the heartbeat does not advance.

### Step 9 — Scanner source installation and effective runtime verification

The OOS-2 release (`/opt/trad_bot_me_inverse_short_oos2_release`) and the
production Scanner (`/opt/trad_bot`, `trad-bot-scanner.service`) are
SEPARATE installations: a fully ready OOS-2 release proves nothing about the
Scanner that must actually deliver `_me_inverse_short_source_events`.
Scanner readiness in the preflight is `OOS2_SOURCE_CAPTURE_READY`
(Scanner-side, audited against `--scanner-root`); release readiness is the
`V2_RUNTIME_CODE_READY` concept (carried by the `runtime_code_ready`
subcheck) inside `OOS2_RUNTIME_CONFIG_READY`.
Safe sequence — any Scanner update or restart is a **separate
operational GO**, never part of an OOS-2 activation approval:

1. Check the current production Scanner version
   (`git -C /opt/trad_bot rev-parse HEAD`, `stat -c '%y' /opt/trad_bot/scanner_runner.py`,
   `systemctl show trad-bot-scanner.service -p ExecStart,MainPID,FragmentPath`).
2. Confirm the required OOS-2 source capture is present in the TARGET
   Scanner release (both hooks + `_capture_me_inverse_short_oos2_source`
   delivery + `ROUTE_PRE_FILTER_SOURCE` routing — the preflight's
   `--scanner-root` audit proves this on the checkout).
3. Prepare the updated Scanner tree independently of the OOS-2 runtime
   (staged copy/checkout — installing it is NOT yet allowed).
4. Verify compatibility with the existing V1/V2: V1 capture hook still
   present, V1 delivery untouched, scanner flags
   `ME_INVERSE_SHORT_V1_ENABLED` / `ME_INVERSE_SHORT_V2_ENABLED`
   unchanged, OOS-2 scanner flag still `0`.
5. Obtain a **separate, explicit GO** to install/restart the production
   Scanner (this document/task does NOT grant it).
6. Only after that GO: install, restart ONLY `trad-bot-scanner.service`,
   then verify the effective version of the RUNNING process
   (`systemctl show … -p MainPID`, `/proc/<pid>/cmdline`,
   start time vs source mtimes) — sources updated after process start
   mean the process may still hold pre-update code.
7. Confirm OOS-2 source routing on the running checkout (read-only
   preflight, see the command below: `OOS2_SOURCE_CAPTURE_READY=PASS`).
8. Only after 1–7 allow the further OOS-2 activation preflight (Step 10).

Read-only preflight for this step (never writes, never restarts):

```bash
cd /opt/trad_bot_me_inverse_short_oos2_release
.venv/bin/python scripts/me_inverse_short_oos2_preflight.py \
  --env-file /etc/trad-bot/me-inverse-short-oos2-runtime.env \
  --scanner-env-file /opt/trad_bot/.env \
  --scanner-root /opt/trad_bot \
  --scanner-unit /etc/systemd/system/trad-bot-scanner.service
```

Note: reading the running process metadata (`/proc/<pid>/cwd`) requires
sufficient privileges (root or the Scanner's user); without them the
preflight honestly reports `SCANNER_PROCESS_NOT_VERIFIABLE`
(`NOT_VERIFIED` — never a silent PASS), it never guesses and never
restarts the Scanner to make verification easier.

Note — Python virtualenv: standard Linux virtualenvs are supported,
including the usual **symlink-based** interpreter
(`/opt/trad_bot/.venv/bin/python → python3 → /usr/bin/python3.12`).
The preflight validates the location of the **virtualenv itself** — an
absolute `ExecStart` interpreter path lexically inside `--scanner-root`
(`..`-traversal safe), the `<venv>/bin/python*` layout and a well-formed
`pyvenv.cfg`, with the venv/bin directories resolving inside the
checkout — not the final system-interpreter target behind the symlink.
Copy-based virtualenvs are equally valid; a foreign interpreter, a
directory-symlink substitution of the venv, a missing/invalid
`pyvenv.cfg` or a missing executable block activation (`FAIL`) or stay
`NOT_VERIFIED` (`ready_for_activation=false`). All of these checks read
path metadata and the venv configuration only — they never execute any
interpreter, never touch the production virtualenv, and never restart or
reconfigure the Scanner.

Example — old Scanner checkout (OOS-2 release fully ready), the
`OOS2_SOURCE_CAPTURE_READY` compound stays blocked:

```json
"OOS2_RUNTIME_CONFIG_READY": {"status": "PASS", "reason": ""},
"OOS2_SOURCE_CAPTURE_READY": {"status": "FAIL",
  "reason": "scanner_source_routing:OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"}
```

Example — Scanner checkout, systemd unit and running process all
confirmed (and no source changed after process start):

```json
"OOS2_SOURCE_CAPTURE_READY": {"status": "PASS", "reason": ""}
```

Example — checkout/unit/process metadata not confirmable (unit not
installed, host without systemd metadata, sources newer than the running
process): `{"status": "NOT_VERIFIED", "reason": "…:SCANNER_PROCESS_NOT_VERIFIABLE"}`
— `NOT_VERIFIED` never counts as satisfied; `ready_for_activation` stays
`false`.

* **STOP** if `--scanner-root` does not match the unit's
  `ExecStart`/`WorkingDirectory`, if the running process cmdline/cwd
  differs from the unit, if audited sources are newer than the process
  start time, if the Scanner unit file is unreadable, or if the release
  is ready while the Scanner checkout fails its source-routing audit.
* **STOP** if the Scanner's Python virtualenv cannot be validated —
  interpreter path relative/traversing outside `--scanner-root`, venv or
  `bin` directory substituted by a link to a foreign environment, missing
  or malformed `pyvenv.cfg`, missing/non-executable interpreter, or a
  symlink target belonging to another virtualenv. A standard
  symlink-based Linux venv pointing at the system interpreter is CORRECT
  and does not stop the run; anything unconfirmable stays `NOT_VERIFIED`
  and `ready_for_activation` stays `false`.
* **STOP** and report honestly: confirming the exact *loaded bytecode*
  is impossible without restarting the Scanner or adding extra
  diagnostics — the preflight uses read-only `systemctl show`/`/proc`
  metadata (process↔unit agreement + source freshness) and never
  pretends more. Do NOT treat the static AST audit as runtime proof.
* **STOP** if any of the steps above (install/restart of the production
  Scanner) was not covered by its own explicit GO. This task performs
  NONE of them.

### Step 10 — Read-only preflight: all six checks must PASS

```bash
cd /opt/trad_bot_me_inverse_short_oos2_release
.venv/bin/python scripts/me_inverse_short_oos2_preflight.py \
  --env-file /etc/trad-bot/me-inverse-short-oos2-runtime.env \
  --scanner-env-file /opt/trad_bot/.env \
  --scanner-root /opt/trad_bot \
  --scanner-unit /etc/systemd/system/trad-bot-scanner.service
```

Exit code `0` prints `"result": "READY_FOR_ACTIVATION"` — the ONLY value
that may proceed to an activation request. The six independent checks:

| Check | Meaning |
|---|---|
| `DB_READY` | dedicated credentials reach the DB; migration-088 schema present (`OOS2_SCHEMA_MISSING` otherwise); read-only transaction only |
| `OOS2_RUNTIME_CONFIG_READY` | RELEASE-side: dedicated env file present/valid, flag `0`, port/DB name sane, AND the release passes the static source audit (`runtime_code_ready` = the `V2_RUNTIME_CODE_READY` concept) |
| `OOS2_SOURCE_CAPTURE_READY` | SCANNER-side: the ACTUAL checkout from `--scanner-root` is audited (pre-gate publication to BOTH hooks, fail-open, OOS-2-flag-only, no trading-gate dependency), agrees with the systemd unit (`ExecStart`/`WorkingDirectory`/Python env/`EnvironmentFile`), the running process matches the unit and predates no audited source, and the Scanner flag/DB tuple are consistent (two-process check) |
| `OOS2_ACTIVATION_READY` | `READY_TO_START`, fingerprint/freeze NULL, event/position/close-leg/outbox empty |
| `PROTOCOL_FINGERPRINT_VALID` | deployed fingerprint equals the reviewed pin; paper/prospective/no-backfill/direction invariants hold |
| `V1_ISOLATION_VALID` | no V1 key in the OOS-2 env; OOS-2 unit paths/env/exec disjoint from the V1 and V2 units; V1 capture hook still present in the ACTUAL Scanner checkout |

* Each check reports `PASS` / `FAIL` / `NOT_VERIFIED`; **`NOT_VERIFIED`
  never auto-passes** — `READY_FOR_ACTIVATION` requires all six `PASS`.
* The tool never writes, never activates, never restarts the Scanner,
  prints no credentials, and always rolls back its read-only transaction.
* **STOP** if any check is not `PASS`, if the fingerprint pin differs, or
  if the report contains anything but `PASS` for a mandatory check.

### Step 11 — Activation (SEPARATE APPROVED GO — documentation only)

Only after an explicit, separate activation decision (**this task performs
NO activation and NO freeze change**):

```bash
# 1. Confirm preflight still READY_FOR_ACTIVATION (Step 10) and capture a
#    FRESH current UTC boundary (never a timestamp copied from docs).
# 2. Read-only status first:
.venv/bin/python scripts/me_inverse_short_oos2_activate.py --show \
  --env-file /etc/trad-bot/me-inverse-short-oos2-runtime.env
# 3. After GO, atomically fix RUNNING + fingerprint + freeze:
#    .venv/bin/python scripts/me_inverse_short_oos2_activate.py --activate \
#      --freeze-ts <FRESH_UTC_ISO8601> \
#      --env-file /etc/trad-bot/me-inverse-short-oos2-runtime.env
# 4. Set ME_INVERSE_SHORT_OOS2_ENABLED=1 in the runtime env AND separately in
#    the Scanner's environment; restart ONLY this dedicated unit (a Scanner
#    restart needs its own approval).
# 5. Verify: activation_mode=FULL, strictly signal_time > freeze_ts on every
#    event, capture counters (captured/duplicates/errors) moving, admission
#    gate refusing stale signals (DB-authoritative TTL) and capacity refusals
#    at the local constant 12, partial-ladder legs booking into
#    dds.me_long_v2_inverse_short_oos2_v1_close_leg, paper-only rows.
```

* **STOP** if preflight is not `READY_FOR_ACTIVATION`, if the freeze
  timestamp is not fresh, if `--re-freeze` would be needed (never on the
  first activation), if any pre-freeze event can exist (migration 088
  CHECK forbids it), or if the Scanner flag cannot be enabled without
  modifying trading filters/gates.
* **STOP** if the observed entry behaviour differs from the frozen spec —
  the 120-minute TTL, the 12-position local capacity and the
  50 % / 37.5 % / 12.5 % ladder are fingerprint-frozen; changing the
  execution spec requires a NEW review (the fingerprint must never change
  silently).

### Step 12 — Stop / drain / rollback

```bash
# Graceful stop (finishes the current cycle, exits 0, releases the lock):
sudo systemctl stop trad-bot-me-inverse-short-oos2-runtime.service

# Drain (RUNNING -> DRAINING: EXIT_ONLY, fingerprint/freeze preserved):
.venv/bin/python scripts/me_inverse_short_oos2_activate.py --drain \
  --env-file /etc/trad-bot/me-inverse-short-oos2-runtime.env

# Rollback / uninstall — the OOS-2 service is fully self-contained:
#   1. set ME_INVERSE_SHORT_OOS2_ENABLED=0 in
#      /etc/trad-bot/me-inverse-short-oos2-runtime.env (and, if it was
#      ever enabled, in the Scanner environment), then
#   2. stop the OOS-2 unit only:
sudo systemctl disable --now trad-bot-me-inverse-short-oos2-runtime.service
sudo rm /etc/systemd/system/trad-bot-me-inverse-short-oos2-runtime.service
sudo systemctl daemon-reload
# Optional: remove the release tree, /etc/trad-bot/me-inverse-short-oos2-runtime.env
# and /opt/trad_bot_me_inverse_short_oos2_release/{logs,var}.
```

* **STOP** while draining: do NOT set the runtime flag to `0` while any
  position or close-outbox entry is pending — it removes EXIT_ONLY
  management of live positions. Drain first, verify exits settle, then
  disable.
* **Rollback never touches the V1 unit
  (`trad-bot-me-inverse-short-runtime.service`), the V2 unit
  (`trad-bot-me-inverse-short-v2-runtime.service`), the Scanner, the
  production Paper service or the Research Evaluator** — rollback means
  `ME_INVERSE_SHORT_OOS2_ENABLED=0` / stop
  `trad-bot-me-inverse-short-oos2-runtime.service` and nothing else, and
  it never deletes PostgreSQL state (events/positions/close legs are
  history; a re-install resumes read-only recovery exactly where it
  stopped).
* Migration 088 is **not** rolled back on service uninstall: dropping
  tables destroys paper history and is a separate approved decision.

---

## 4. Monitoring

* One structured `me_inverse_short_runtime_cycle` telemetry line per cycle
  (`entries`, `exits`, `entry_refusals`, `activation_mode`, DB/API errors,
  pending close-outbox, `durability_risk`).
* Heartbeat file mtime = liveness; stale mtime with `active` status =
  stuck loop.
* Capture health lives in the Scanner process: counters
  `captured/duplicates/disabled/not_active/pre_freeze/invalid/errors` and
  error type `ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR` in
  `dds.scanner_error`. Zero events with zero errors while the Scanner
  flag is `0` is expected; zero events with flag `1` is an incident.
* An absence of new source signals is **not** a PASS — verify the Scanner
  is actually publishing `_me_inverse_short_source_events`.
* Grafana: import/provision
  `monitoring/grafana/dashboards/me-inverse-short-oos-2.json`
  (**uid `me-inverse-short-oos-2`**, title "Trad Bot — ME Inverse SHORT
  OOS-2") — panels cover activation/freeze/fingerprint, open positions
  (of 12), TP progress, partial-TP legs vs closed positions, source
  capture / expired / capacity-rejected / errors and the equity curve.

## 5. Tests

```bash
# No PostgreSQL required (wiring audit, preflight contract, deploy assets,
# path isolation, contour unit suites):
.venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_oos2_source_capture.py \
    tests/test_me_inverse_short_oos2_deployment_v1.py \
    tests/test_me_long_v2_inverse_short_oos2_entry_ttl_v1.py \
    tests/test_me_long_v2_inverse_short_oos2_isolation_v1.py \
    tests/test_me_long_v2_inverse_short_oos2_ladder_v1.py -q

# Real-PostgreSQL admission suite (SEPARATE test database only; skipped
# unless TEST_DB_HOST / TEST_DB_UNIX_SOCK is set; refuses a database named
# like production; restores the activation row in teardown):
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=5433 TEST_DB_NAME=trad_bot_migration_test \
  .venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_oos2_pg_admission_it_v1.py -q
```

No test writes to the production database.

## 6. Prohibitions (unchanged by any step above)

* No merge/activation/VPS deploy from an install-only pass; **this task
  applies NO migration, performs NO activation and makes NO freeze
  (`freeze_ts`) change** — migration 088 on production, OOS-2 activation
  and Scanner changes each need their own explicit approval.
* No changes to V1 or V2 (units, env files, capture hooks, tables),
  no backfill, no real Bybit orders, no trading-filter/scoring/regime
  changes, no touching the Paper service or Research Evaluator, no
  freeze-time recycling.
