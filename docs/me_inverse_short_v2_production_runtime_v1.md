# ME_LONG_V2_INVERSE_SHORT_V2 — Production Runtime Runbook

Standalone, systemd-managed service that keeps the **frozen inverse-SHORT
paper contour (V2)** running on real Bybit market data — with activation
**OFF**, no backfill and no trading side effects.

```
python -m app.paper.me_long_v2_inverse_short_v2.production_runtime
```

> **Scope of this runbook.** Installing the service inert (flag `0`) and
> verifying it is routine. Everything from migration 087 on the production
> database onwards is a **separately approved decision**: each step below
> carries explicit **STOP** conditions. This document never authorizes
> activation, Scanner changes, VPS deployment or any production write.

---

## 1. Architecture

```
trad-bot-me-inverse-short-v2-runtime.service   (systemd, User=tradbot)
        │  EnvironmentFile=/etc/trad-bot/me-inverse-short-v2-runtime.env
        ▼
production_runtime.main()   (module app.paper.me_long_v2_inverse_short_v2)
        ├── RuntimeInstanceLock       ← OS lock: exactly ONE process (exit 3)
        ├── ensure_writable(log/heartbeat/lock dirs)  ← fail fast
        ├── connect_pg_store()        ← DEDICATED PostgreSQL connection
        └── ProductionRuntime.run()   ← loop, SIGTERM, telemetry, heartbeat
                 └── InverseShortContour.run_cycle(...)   (frozen V2 spec)
                          ├── refresh_activation()   (two-factor, fail closed)
                          ├── engine.check_exits()   (STOP → TP → TIMEOUT …)
                          ├── engine.open_entries()  ← P1-2 admission gate
                          └── engine.mark_to_market()
```

**Two-factor activation (unchanged concept, V2 surface):**

1. runtime flag `ME_INVERSE_SHORT_V2_ENABLED=1` **and**
2. activation row `RUNNING` whose `protocol_fingerprint` equals
   `protocol_fingerprint()` (pinned `5a9047ca3621cefb4d53b45275ad6348887440568cb055f843f85e2eb1abdacc`)
   with a non-NULL `freeze_ts`.

`DRAINING` = EXIT_ONLY: manage existing positions, never open new ones.

**Event source:** the pre-gate `_me_inverse_short_source_events`
publication made by `ScannerOrchestrator.scan_all_with_stats` **before**
the direction gate / expectancy filter, delivered by the production
scanner loop to `_capture_me_inverse_short_v2_source`
(`ROUTE_PRE_FILTER_SOURCE`). The V2 capture is independent of V1: own
flag, own store connection, own error type
(`ME_INVERSE_SHORT_V2_SOURCE_CAPTURE_ERROR`), fail-open.

**Entry admission (database-enforced):** `INSERT_POSITION_SQL` itself
requires — inside one PostgreSQL statement with row locks
(`FOR UPDATE OF ev, act`, `ON CONFLICT … DO NOTHING`):

* `statement_timestamp() - signal_time < 120 minutes` (the frozen TTL,
  evaluated at the **database** clock, so a TTL that elapses between the
  Python check and the INSERT is still refused);
* event `CAPTURED`, consistent fingerprint/activation identity, strict
  `signal_time > freeze_ts`, activation `RUNNING`, no existing position.

TTL expiry and capacity disposal race the admission through row locks;
exactly one valid outcome results (never EXPIRED-with-position).

**Isolation:** the V2 release tree, lock file
(`var/me_inverse_short_v2_runtime.lock`), heartbeat
(`var/me_inverse_short_v2_runtime_heartbeat.json`), log
(`logs/me_inverse_short_v2_runtime.log`), env file and unit are all
distinct from the V1 runtime — the preflight's `V1_ISOLATION_VALID` check
compares the two unit files directive by directive.

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
| `InverseShortContour.run_cycle()` ordering | `app/paper/me_long_v2_inverse_short_v2/runner.py` |
| Engine: TP 10 % / SL 10 % / 7200 min hold, STOP_FIRST, fees/slippage/funding, outbox, reconciliation | `engine.py` (frozen spec) |
| Two-factor activation (`ME_INVERSE_SHORT_V2_ENABLED` + RUNNING row) | `activation.py` |
| Prospective-only capture + route constants | `capture.py` + `scanner_runner.py` |
| Frozen protocol + fingerprint | `protocol.py` |
| PostgreSQL store, admission gate, reconnect/ambiguous-COMMIT adoption | `store.py` |
| Source-path audit (static, shared with preflight) | `source_path.py` |
| Dedicated PG store with reconnect | `store.connect_pg_store` |
| Settings / Bybit client / rate limiter | `app.config`, `app.exchange.bybit_client` (unchanged) |

---

## 3. Installation — 11 steps with STOP conditions

### Step 1 — Pin the reviewed release

```bash
sudo mkdir -p /opt/trad_bot_me_inverse_short_v2_release
sudo git -C /opt/trad_bot_me_inverse_short_v2_release clone <repo-url> .
sudo git -C /opt/trad_bot_me_inverse_short_v2_release checkout <reviewed-tag-or-commit>
```

* **STOP** if the commit is not the one named in the accepted code review,
  if `git status` is dirty, or if the reviewed fingerprint pin
  (`PINNED_PROTOCOL_FINGERPRINT` in the preflight) does not match
  `python -c "from app.paper.me_long_v2_inverse_short_v2.protocol import
  protocol_fingerprint; print(protocol_fingerprint())"` from that tree.

### Step 2 — Python environment of this release

```bash
sudo python3 -m venv /opt/trad_bot_me_inverse_short_v2_release/.venv
sudo /opt/trad_bot_me_inverse_short_v2_release/.venv/bin/pip install -r \
    /opt/trad_bot_me_inverse_short_v2_release/requirements.txt
sudo /opt/trad_bot_me_inverse_short_v2_release/.venv/bin/pip check
```

* The unit's `ExecStart` uses exactly this interpreter.
* **STOP** if `pip check` reports conflicts — notably `scramp==1.5.0`
  breaks PostgreSQL connectivity (`AttributeError: make_channel_binding`);
  the runtime needs `pg8000==1.31.5` + `scramp==1.4.18`. Never modify the
  Scanner / Paper / Research Evaluator environments.

### Step 3 — Writable runtime/log directories

```bash
sudo mkdir -p /opt/trad_bot_me_inverse_short_v2_release/{logs,var}
sudo chown -R tradbot:tradbot /opt/trad_bot_me_inverse_short_v2_release/{logs,var}
sudo chmod 0750 /opt/trad_bot_me_inverse_short_v2_release/{logs,var}
sudo -u tradbot touch /opt/trad_bot_me_inverse_short_v2_release/logs/.write_probe \
  && sudo -u tradbot rm /opt/trad_bot_me_inverse_short_v2_release/logs/.write_probe \
  && echo "logs dir writable OK"
```

* These paths are **V2-only** — never the V1 tree
  `/opt/trad_bot_me_inverse_short_release`.
* **STOP** if the write probe fails (the runtime exits 1 at startup —
  the `PermissionError` lesson from the original deployment).

### Step 4 — Migration 087 (production database — separate approval)

```bash
# Review first (read-only):
sudo -u postgres psql -d trad_bot -c \
  "SELECT to_regclass('dds.me_long_v2_inverse_short_v2_activation'),
          to_regclass('dds.me_long_v2_inverse_short_v2_event');"

# Apply ONLY under an approved migration window, in numeric order:
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 -f \
    sql/migrations/087_me_long_v2_inverse_short_v2.sql

# Verify:
sudo -u postgres psql -d trad_bot -c \
  "SELECT experiment_id, status, protocol_fingerprint, freeze_ts
     FROM dds.me_long_v2_inverse_short_v2_activation;"
# expect exactly one row: ME_LONG_V2_INVERSE_SHORT_V2 / READY_TO_START / NULL / NULL
```

* Migration 087 is transactional (`BEGIN … COMMIT`) and idempotent-safe to
  inspect: it creates the `activation`/`event`/`position`/`close_outbox`
  tables with the four-state event CHECK, the
  `signal_time > activation_freeze_ts` CHECK, the position FK + UNIQUE
  `event_key`, and the immutability trigger on entry facts.
* **STOP** if the migration is not the next unapplied number, if the
  activation row is not `READY_TO_START`, if any V2 table already exists
  with data, or if the approved window has not been granted. **Applying
  migration 087 to production is never part of an install-only pass.**
* **STOP** if migration 085/086 (V1) are absent — 087 does not replace
  them and V1 must remain untouched.

### Step 5 — Runtime environment file (activation OFF)

```bash
sudo mkdir -p /etc/trad-bot
sudo cp deploy/env/me-inverse-short-v2-runtime.env.example \
    /etc/trad-bot/me-inverse-short-v2-runtime.env
sudo chmod 0640 /etc/trad-bot/me-inverse-short-v2-runtime.env
sudo chown root:tradbot /etc/trad-bot/me-inverse-short-v2-runtime.env
sudo $EDITOR /etc/trad-bot/me-inverse-short-v2-runtime.env   # DB creds; keep ENABLED=0
```

* **STOP** if the file contains any `ME_INVERSE_SHORT_V1_*` key (the
  preflight fails `V1_ISOLATION_VALID`), if `ME_INVERSE_SHORT_V2_ENABLED`
  is not `0`, or if the file is world-readable.

### Step 6 — Scanner-side environment (the SECOND process)

The capture hook runs **inside the production Scanner process**
(`EnvironmentFile=/opt/trad_bot/.env` per
`deploy/systemd/trad-bot-scanner.service`). The runtime's flag alone
never enables capture.

```bash
# Append the fragment (deploy/env/me-inverse-short-scanner.env.example):
#   ME_INVERSE_SHORT_V2_ENABLED=0
#   DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD  == runtime env file, verbatim
```

* **STOP** if the Scanner's DB tuple differs from the runtime's (the
  preflight fails `V2_SOURCE_CAPTURE_READY` → `SCANNER_RUNTIME_DB_MISMATCH`), if
  `ME_INVERSE_SHORT_V2_ENABLED` is `1` at preflight time, or if you would
  need to restart the Scanner to add the key — a Scanner restart requires
  its own explicit approval and is **not** part of this install.

### Step 7 — Install the systemd unit (starts inert)

```bash
sudo cp deploy/systemd/trad-bot-me-inverse-short-v2-runtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trad-bot-me-inverse-short-v2-runtime.service
```

* **STOP** if `ConditionPathExists` fails (wrong release path), if the
  unit refuses the environment file, or if the service lands in a restart
  loop (check `journalctl -u … -n 50`; exit 3 means a stray manual
  instance holds the lock — never restart-loop it).

### Step 8 — Verify the inert runtime (flag OFF, zero writes)

```bash
systemctl status trad-bot-me-inverse-short-v2-runtime.service
journalctl -u trad-bot-me-inverse-short-v2-runtime.service -n 20 --no-pager \
  | grep me_inverse_short_runtime_cycle

# Heartbeat (mtime older than a few intervals while "active = loop stuck):
stat -c '%y %n' /opt/trad_bot_me_inverse_short_v2_release/var/me_inverse_short_v2_runtime_heartbeat.json

# Expected with activation OFF: entries=0, entry_refusals contains
# ACTIVATION:DISABLED_FLAG, and NO rows written:
sudo -u postgres psql -d trad_bot -c \
  "SELECT status, protocol_fingerprint, freeze_ts
     FROM dds.me_long_v2_inverse_short_v2_activation;
   SELECT count(*) FROM dds.me_long_v2_inverse_short_v2_position;
   SELECT status, count(*) FROM dds.me_long_v2_inverse_short_v2_event
     GROUP BY status;"
```

* **STOP** if any V2 event/position/outbox row exists, if the activation
  row is not `READY_TO_START` with NULL fingerprint/freeze, if telemetry
  reports `activated=true`, or if the heartbeat does not advance.

### Step 9 — Read-only preflight: all six checks must PASS

```bash
cd /opt/trad_bot_me_inverse_short_v2_release
.venv/bin/python scripts/me_inverse_short_v2_preflight.py \
  --env-file /etc/trad-bot/me-inverse-short-v2-runtime.env \
  --scanner-env-file /opt/trad_bot/.env
```

Exit code `0` prints `"result": "READY_FOR_ACTIVATION"` — the ONLY value
that may proceed to an activation request. The six independent checks:

| Check | Meaning |
|---|---|
| `DB_READY` | dedicated credentials reach the DB; migration-087 schema present; read-only transaction only |
| `V2_RUNTIME_CONFIG_READY` | dedicated env file present/valid, flag `0`, port/DB name sane |
| `V2_SOURCE_CAPTURE_READY` | static audit proves the production loop delivers the pre-gate publication to BOTH hooks, AND the Scanner process's flag/DB tuple are consistent (two-process check) |
| `V2_ACTIVATION_READY` | `READY_TO_START`, fingerprint/freeze NULL, event/position/outbox empty |
| `PROTOCOL_FINGERPRINT_VALID` | deployed fingerprint equals the reviewed pin; paper/prospective/no-backfill/direction invariants hold |
| `V1_ISOLATION_VALID` | no V1 key in the V2 env; V2 unit paths/env/exec disjoint from the V1 unit; V1 capture hook still present |

* Each check reports `PASS` / `FAIL` / `NOT_VERIFIED`; **`NOT_VERIFIED`
  never auto-passes** — `READY_FOR_ACTIVATION` requires all six `PASS`.
* The tool never writes, never activates, prints no credentials, and
  always rolls back its read-only transaction.
* **STOP** if any check is not `PASS`, if the fingerprint pin differs, or
  if the report contains anything but `PASS` for a mandatory check.

### Step 10 — Activation (SEPARATE APPROVED GO — documentation only)

Only after an explicit, separate activation decision:

```bash
# 1. Confirm preflight still READY_FOR_ACTIVATION (Step 9) and capture a
#    FRESH current UTC boundary (never a timestamp copied from docs).
# 2. Read-only status first:
.venv/bin/python scripts/me_inverse_short_v2_activate.py --show \
  --env-file /etc/trad-bot/me-inverse-short-v2-runtime.env
# 3. After GO, atomically fix RUNNING + fingerprint + freeze:
#    .venv/bin/python scripts/me_inverse_short_v2_activate.py --activate \
#      --freeze-ts <FRESH_UTC_ISO8601> \
#      --env-file /etc/trad-bot/me-inverse-short-v2-runtime.env
# 4. Set ME_INVERSE_SHORT_V2_ENABLED=1 in the runtime env AND separately in
#    the Scanner's environment; restart ONLY this dedicated unit (a Scanner
#    restart needs its own approval).
# 5. Verify: activation_mode=FULL, strictly signal_time > freeze_ts on every
#    event, capture counters (captured/duplicates/errors) moving, admission
#    gate refusing stale signals (DB-authoritative TTL), paper-only rows.
```

* **STOP** if preflight is not `READY_FOR_ACTIVATION`, if the freeze
  timestamp is not fresh, if `--re-freeze` would be needed (never on the
  first activation), if any pre-freeze event can exist (migration 087
  CHECK forbids it), or if the Scanner flag cannot be enabled without
  modifying trading filters/gates.
* **STOP** if the observed entry behaviour differs from the frozen spec —
  changing the execution spec requires a NEW review (the fingerprint must
  never change silently).

### Step 11 — Stop / drain / rollback

```bash
# Graceful stop (finishes the current cycle, exits 0, releases the lock):
sudo systemctl stop trad-bot-me-inverse-short-v2-runtime.service

# Drain (RUNNING -> DRAINING: EXIT_ONLY, fingerprint/freeze preserved):
.venv/bin/python scripts/me_inverse_short_v2_activate.py --drain \
  --env-file /etc/trad-bot/me-inverse-short-v2-runtime.env

# Rollback / uninstall — the service is fully self-contained:
sudo systemctl disable --now trad-bot-me-inverse-short-v2-runtime.service
sudo rm /etc/systemd/system/trad-bot-me-inverse-short-v2-runtime.service
sudo systemctl daemon-reload
# Optional: remove the release tree, /etc/trad-bot/me-inverse-short-v2-runtime.env
# and /opt/trad_bot_me_inverse_short_v2_release/{logs,var}.
```

* **STOP** while draining: do NOT set the runtime flag to `0` while any
  position or close-outbox entry is pending — it removes EXIT_ONLY
  management of live positions. Drain first, verify exits settle, then
  disable.
* Rollback never touches the V1 runtime, the Scanner, the production
  Paper service or the Research Evaluator, and never deletes PostgreSQL
  state (events/positions are history; a re-install resumes read-only
  recovery exactly where it stopped).
* Migration 087 is **not** rolled back on service uninstall: dropping
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
  error type `ME_INVERSE_SHORT_V2_SOURCE_CAPTURE_ERROR` in
  `dds.scanner_error`. Zero events with zero errors while the Scanner
  flag is `0` is expected; zero events with flag `1` is an incident.
* An absence of new source signals is **not** a PASS — verify the Scanner
  is actually publishing `_me_inverse_short_source_events`.

## 5. Tests

```bash
# No PostgreSQL required (wiring audit, preflight contract, deploy assets,
# path isolation, contour unit suites):
.venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_v2_source_capture.py \
    tests/test_me_inverse_short_v2_deployment_v1.py \
    tests/test_me_long_v2_inverse_short_v2_entry_ttl_v1.py \
    tests/test_me_long_v2_inverse_short_v2_isolation_v1.py -q

# Real-PostgreSQL admission suite (SEPARATE test database only; skipped
# unless TEST_DB_HOST / TEST_DB_UNIX_SOCK is set; refuses a database named
# like production; restores the activation row in teardown):
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=5433 TEST_DB_NAME=trad_bot_migration_test \
  .venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_v2_pg_admission_it_v1.py -q
```

No test writes to the production database.

## 6. Prohibitions (unchanged by any step above)

* No merge/activation/VPS deploy from an install-only pass; migration 087
  on production, V2 activation and Scanner changes each need their own
  explicit approval.
* No changes to V1 (unit, env, capture, tables), no backfill, no real
  Bybit orders, no trading-filter/scoring/regime changes, no touching the
  Paper service or Research Evaluator, no freeze-time recycling.
