# ME_LONG_V2_INVERSE_SHORT_V1 — Production Runtime (Issue #268)

Standalone, systemd-managed service that keeps the **frozen PR #267
prospective SHORT paper contour** running on real Bybit market data — with
activation **OFF**, no backfill and no trading side effects.

```
python -m app.paper.me_long_v2_inverse_short_v1.production_runtime
```

---

## 1. Architecture

```
trad-bot-me-inverse-short-runtime.service   (systemd, User=tradbot)
        │
        ▼
production_runtime.main()
        ├── RuntimeInstanceLock          ← OS lock: exactly ONE process
        ├── ensure_writable(log/heartbeat/lock dirs)   ← fail fast (PermissionError lesson)
        ├── load_settings() + MarketDataConfig.from_env()   ← fail fast on bad values
        ├── connect_pg_store()           ← DEDICATED PostgreSQL connection (bounded retry)
        ├── BybitClient(settings)        ← retry budget + API telemetry + GLOBAL rate limiter
        │        └── MarketDataProvider  ← freshness / no-lookahead / honest triage
        └── ProductionRuntime.run()      ← loop, SIGTERM/SIGINT, telemetry, heartbeat
                 └── InverseShortContour.run_cycle(prices, funding, bars)   ← PR #267, untouched
                          ├── refresh_activation()        (two-factor, fail closed)
                          ├── engine.check_exits()        (outbox replay → dirty funding →
                          │       STOP → TP → TIMEOUT → funding settlement)
                          ├── engine.open_entries()       (FULL mode only, all fail-closed gates)
                          └── engine.mark_to_market()
```

**Per-cycle order (frozen, unchanged from PR #267):**

1. activation refresh (runtime also reads the raw row for telemetry);
2. recovery/reconciliation (engine recovery at construction; `flush_pending_closes`
   + dirty-funding retry run first inside `check_exits`);
3. open-position exits + close-outbox replay;
4. funding settlement (tri-state: observed rate / confirmed 0 / UNKNOWN);
5. new prospective entries **only** in FULL (`RUNNING`) mode;
6. persistence (store commits) + structured telemetry + heartbeat.

**Event source:** only `dds.me_long_v2_inverse_short_v1_event`, written by the
EXISTING independent pre-filter source capture in `scanner_runner.py`
(original `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` LONG signals, research
direction SHORT). The runtime never writes events, never reads
`dds.scanner_setup`, and never backfills: the engine's strict
`signal_time > freeze_ts` gate plus the migration-085 CHECK make historical
events impossible.

### Market data policy (per stream)

| Stream   | Source (via `BybitClient`)          | Max age (env)                          | On violation |
|----------|-------------------------------------|----------------------------------------|--------------|
| Price    | `GET /v5/market/tickers` (`lastPrice`) | `ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S` (30 s) | excluded → entries fail closed, exits HOLD |
| OHLC bar | `GET /v5/market/kline` (interval `ME_INVERSE_SHORT_MD_BAR_INTERVAL`, default 5m) | `ME_INVERSE_SHORT_MD_BAR_MAX_AGE_S` (600 s) | no bar handed to the engine → intrabar check falls back to the observed price |
| Funding  | `GET /v5/market/funding/history` (percent) | `ME_INVERSE_SHORT_MD_FUNDING_MAX_AGE_S` (3600 s) | rate = UNKNOWN → obligation preserved, nothing booked; an **empty history list is `MISSING`, never a confirmed `0.0`** |

* **No lookahead:** a bar qualifies only when `open_ms + interval_ms <= now_ms`
  (closed at/before the observation time); off-grid or malformed bars are
  rejected. The bar range handed to the engine is the closed bar **widened by
  the live quote** (both observable now — a touch during the forming bar is
  never missed, a future value is never used).
* **Return-time freshness (Codex R2 P1):** the price triage runs when the
  cycle starts, but later OHLC/funding requests (retries included) can take
  seconds — immediately before the snapshot is returned every usable
  observation is re-checked against the current UTC clock and anything no
  longer provably fresh is excluded and reported `STALE` (e.g. with
  `PRICE_MAX_AGE_S=30`, a quote followed by a 60 s kline request can never
  reach the engine as `OK`).
* **Whole-interval entry clipping (Codex R2 P1):** a candle is handed to the
  exit engine only when its WHOLE interval lies after the entry —
  `open_ms >= max(entered_at)` on that symbol. A candle that straddled the
  entry (e.g. opened 12:00, SHORT entered 12:00:30) may hold a high/low
  printed *before* the position existed — it can never close that position;
  a candle without a known `open_ms` is inadmissible too. Otherwise the
  engine falls back to the observed price only — a conservative delay
  (detected when the next candle closes), never a fabricated fill. Covered
  by `TestPreEntryBarClipping` (failing-then-passing, incl. the straddle,
  restart and multi-position cases).
* **No substitution:** `STALE` / `MISSING` / `API_ERROR` / `INVALID` values are
  excluded, never replaced by guesses; every (symbol, stream) carries its
  status into telemetry (`market_data_statuses`).
* **One clock:** everything is timestamped with a single injected UTC clock;
  a negative age (clock moved backwards) is never "fresh".
* **No limiter bypass:** the provider performs no HTTP of its own — every
  request goes through `BybitClient` (retry budget, `ApiTelemetry`, shared
  `GlobalBybitRateLimiter`).

### Exit codes

| Code | Meaning | systemd |
|------|---------|---------|
| 0 | clean stop (`--once`, SIGTERM/SIGINT, `--max-cycles`) | no restart |
| 1 | fatal startup/runtime error | `Restart=on-failure` |
| 2 | CLI usage error | no restart |
| 3 | another instance holds the lock | `RestartPreventExitStatus=3` |

---

## 2. Components reused (nothing re-implemented)

| Component | Source (PR #267 / project) |
|---|---|
| `InverseShortContour.run_cycle()` — full cycle ordering | `app/paper/me_long_v2_inverse_short_v1/runner.py` |
| Engine: TP 10 % / SL 10 % / 7200 min, STOP_FIRST, fees 0.00055, slippage 0.0005, funding, outbox, accounting reconciliation | `engine.py` (unchanged) |
| Two-factor activation (`ME_INVERSE_SHORT_V1_ENABLED` + RUNNING row/fingerprint/freeze) | `activation.py` (unchanged) |
| Prospective-only source capture | `capture.py` + `scanner_runner.py` (unchanged) |
| Frozen protocol + fingerprint | `protocol.py` (unchanged) |
| Dedicated PG store with reconnect / ambiguous-COMMIT handling | `store.py::connect_pg_store` (unchanged) |
| Bybit API: retry policy, telemetry, global rate limiter | `app/exchange/bybit_client.py` (unchanged) |
| Settings | `app/config` (unchanged) |

**New files only:** `market_data.py`, `production_runtime.py`, this runbook,
the systemd unit, the env example, and tests.

---

## 3. Installation (prepare BEFORE first start)

```bash
# 1) Dedicated release checkout (separate from /opt/trad_bot_pr267_release)
sudo mkdir -p /opt/trad_bot_me_inverse_short_release
sudo git -C /opt/trad_bot_me_inverse_short_release clone <repo-url> .
sudo git -C /opt/trad_bot_me_inverse_short_release checkout <release-tag-or-commit>

# 2) Python environment of THIS release — the unit's ExecStart uses
#    exactly this interpreter (/opt/trad_bot_me_inverse_short_release/.venv/
#    bin/python), so create it HERE before installing the unit:
sudo python3 -m venv /opt/trad_bot_me_inverse_short_release/.venv
sudo /opt/trad_bot_me_inverse_short_release/.venv/bin/pip install -r \
    /opt/trad_bot_me_inverse_short_release/requirements.txt

# 3) Writable runtime/log directories — the earlier deployment failure was
#    `PermissionError: /opt/trad_bot_pr267_release/logs`; create and VERIFY
#    them BEFORE the first start (the runtime also probes them and refuses
#    to start with a clear message if they are not writable):
sudo mkdir -p /opt/trad_bot_me_inverse_short_release/{logs,var}
sudo chown -R tradbot:tradbot /opt/trad_bot_me_inverse_short_release/{logs,var}
sudo chmod 0750 /opt/trad_bot_me_inverse_short_release/{logs,var}
sudo -u tradbot touch /opt/trad_bot_me_inverse_short_release/logs/.write_probe \
    && sudo -u tradbot rm /opt/trad_bot_me_inverse_short_release/logs/.write_probe \
    && echo "logs dir writable OK"

# 4) Environment file (activation stays OFF)
#    DB_PASSWORD=CHANGE_ME in the example is a PLACEHOLDER: set the real
#    credential only on the server, keep the file mode 0640 (root:tradbot)
#    and never commit/paste real credentials — the runtime never logs
#    settings, passwords or API secrets (asserted by tests).
sudo mkdir -p /etc/trad-bot
sudo cp deploy/env/me-inverse-short-runtime.env.example \
    /etc/trad-bot/me-inverse-short-runtime.env
sudo chmod 0640 /etc/trad-bot/me-inverse-short-runtime.env
sudo chown root:tradbot /etc/trad-bot/me-inverse-short-runtime.env
sudo $EDITOR /etc/trad-bot/me-inverse-short-runtime.env   # DB credentials; keep ENABLED=0

# 5) Database prerequisites (already true on trad-bot-01 for PR #267)
#    migrations 084/085/086 applied; dds.me_long_v2_inverse_short_v1_* tables exist;
#    activation row = READY_TO_START (fail-closed).

# 6) Install + start the unit (starting it is inert while ENABLED=0)
sudo cp deploy/systemd/trad-bot-me-inverse-short-runtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trad-bot-me-inverse-short-runtime.service
```

## 4. Verification

```bash
systemctl status trad-bot-me-inverse-short-runtime.service
journalctl -u trad-bot-me-inverse-short-runtime.service -f

# One structured telemetry line per cycle (all Issue §3.7 fields):
journalctl -u trad-bot-me-inverse-short-runtime.service -n 20 --no-pager \
  | grep me_inverse_short_runtime_cycle

# Heartbeat (diagnoses a hung runtime: mtime older than a few intervals
# while the unit is "active" means the loop is stuck):
stat -c '%y %n' /opt/trad_bot_me_inverse_short_release/var/me_inverse_short_runtime_heartbeat.json
cat /opt/trad_bot_me_inverse_short_release/var/me_inverse_short_runtime_heartbeat.json

# Expected with activation OFF: activation_mode=NONE, entries=0,
# entry_refusals=["ACTIVATION:DISABLED_FLAG"], no rows written:
sudo -u postgres psql -d trad_bot -c \
  "SELECT status, protocol_fingerprint, freeze_ts FROM dds.me_long_v2_inverse_short_v1_activation;
   SELECT count(*) FROM dds.me_long_v2_inverse_short_v1_position;
   SELECT status, count(*) FROM dds.me_long_v2_inverse_short_v1_event GROUP BY status;"
```

Manual single run (diagnostics, uses the same lock):

```bash
sudo -u tradbot /opt/trad_bot_me_inverse_short_release/.venv/bin/python \
  -m app.paper.me_long_v2_inverse_short_v1.production_runtime --once
```

## 5. Stop / rollback

```bash
# Graceful stop (finishes the current cycle, exits 0, releases the lock)
sudo systemctl stop trad-bot-me-inverse-short-runtime.service

# Rollback / uninstall — the service is fully self-contained:
sudo systemctl disable --now trad-bot-me-inverse-short-runtime.service
sudo rm /etc/systemd/system/trad-bot-me-inverse-short-runtime.service
sudo systemctl daemon-reload
# Optional: remove the release tree, env file and runtime dirs.
# Open positions/events are NOT deleted: state lives in PostgreSQL and a
# re-install resumes exactly where it stopped (recovery is read-only).
```

Rollback never touches `/opt/trad_bot_pr267_release`, the Scanner, the
production Paper service or the Research Evaluator — those are separate
units and separate processes.

## 6. Activation state (what this issue deliberately does NOT do)

* Activation row stays **`READY_TO_START`** (fingerprint `NULL`,
  `freeze_ts NULL`) — no production activation, no freeze, no fingerprint
  write, no `ME_INVERSE_SHORT_V1_ENABLED=1`.
* With the flag at `0` the runtime starts, logs `ACTIVATION:DISABLED_FLAG`
  refusals, performs **zero** writes and (with no open positions) **zero**
  market-data requests.
* Future clean prospective OOS start (SEPARATE approval, not part of #268):
  1. deploy this service with `ME_INVERSE_SHORT_V1_ENABLED=0` and verify §4;
  2. run `scripts/me_inverse_short_v1_activate.py --env-file /etc/trad-bot/me-inverse-short-runtime.env --activate --freeze-ts <UTC>`
     (fixes the CURRENT fingerprint + freeze atomically);
  3. set `ME_INVERSE_SHORT_V1_ENABLED=1` in `/etc/trad-bot/me-inverse-short-runtime.env`;
  4. `systemctl restart trad-bot-me-inverse-short-runtime.service`;
  5. verify `activation_mode=FULL`, `signal_time > freeze_ts` on every event,
     and that no pre-freeze row can exist (migration-085 CHECK);
  6. drain later with `scripts/me_inverse_short_v1_activate.py --env-file /etc/trad-bot/me-inverse-short-runtime.env --drain`
     (RUNNING → DRAINING: exits only, fingerprint/freeze preserved).

## 7. Tests

```bash
# Runtime + market-data suites (no PostgreSQL required)
.venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_market_data_v1.py \
    tests/test_me_long_v2_inverse_short_production_runtime_v1.py -q

# PR #267 contour regression (durability, ambiguous commit, funding,
# reconnect, duplicate delivery, dry-run, ...)
.venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_v1.py \
    tests/test_me_long_v2_inverse_short_*.py tests/test_me_inverse_short_source_redelivery_v1.py -q

# Optional real-PostgreSQL integration (separate TEST DB only; skipped
# unless TEST_DB_HOST / TEST_DB_UNIX_SOCK is configured). No psql binary
# is needed: migrations 085/086 are applied through the test connection
# itself. The suite refuses a database named like production
# (PRODUCTION_DB_NAME guard) and restores the activation row in teardown:
TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 TEST_DB_NAME=trad_bot_migration_test \
  .venv/bin/python -m pytest tests/test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py -v
```

No test writes to the production database (guarded by `TEST_DB_*` + the
skipif contract used across this repository, plus the explicit
`current_database()` production-name guard in this suite).

---

## 8. PR #271 — read-only preactivation audit (requires separate approval to start)

The deployment described above has been verified in production with the service
**active/enabled but inert**: flag `0`, DB row `READY_TO_START`, stored
fingerprint/freeze NULL, events/positions/outbox `0/0/0`.

The dedicated runtime uses `pg8000==1.31.5` and **`scramp==1.4.18`**.
`scramp==1.5.0` breaks PostgreSQL connectivity with
`AttributeError: make_channel_binding`. Install from the updated
`requirements.txt` in a clean isolated release venv and run `pip check`.
Do not replace the Scanner/Paper/Research Evaluator environments.

### Read-only preflight

Run from the dedicated release checkout as an account allowed to read the
`root:tradbot 0640` environment file:

```bash
cd /opt/trad_bot_me_inverse_short_release
.venv/bin/python scripts/me_inverse_short_v1_preflight.py \
  --env-file /etc/trad-bot/me-inverse-short-runtime.env
```

**No activation occurs**. The audit explicitly reads that EnvironmentFile
rather than depending on `load_settings()` fallback. It refuses absent,
duplicated, malformed or non-OFF configuration, unexpected DB name, missing or
already-frozen activation row, and any existing event/position/outbox rows.
The dedicated connection issues only `BEGIN TRANSACTION READ ONLY` and
`SELECT` statements and rolls back. The report never includes DB credentials.
`PREACTIVATION_DB_PASS` means only that the **dedicated runtime DB and
static protocol** meet these constraints. It does *not* mean the entire
end-to-end ingestion is proven.

### Critical blocker: capture runs in a DIFFERENT process

`scanner_runner.py::_capture_me_inverse_short_source()` checks
`ME_INVERSE_SHORT_V1_ENABLED` inside **the production Scanner process**.
Enabling the flag in the dedicated runtime's EnvironmentFile alone **does not
enable source capture**. No source events then arrive; zero OOS accumulation
would be expected despite the runtime showing `FULL`.

Before separate activation approval the operator MUST independently confirm:

1. The deployed, running Scanner release includes the PR #267 independent
   `_me_inverse_short_source_events` pre-gate/prefilter source hook and
   `_capture_me_inverse_short_source` wiring. The candidate must be original
   `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` / `LONG`; paper side is `SHORT`.
2. The Scanner's actual effective process environment can be set to
   `ME_INVERSE_SHORT_V1_ENABLED=1` without modifying trading filters/gates.
   Verify the actual systemd unit and environment source. This is **not**
   the dedicated runtime env and may require an explicitly approved Scanner
   restart; never assume inheritance.
3. Verify the scanner's source universe from its effective `config.yaml`
   and runtime scanner configuration; **no fixed symbol allowlist is hardcoded
   in this inverse-short contour**. Check symbols seen in source events after
   activation, not guessed symbols.
4. Verify the shared global Bybit limiter is enabled for the isolated runtime
   and that source capture can reach the same production `trad_bot` database;
   check permissions and capture error telemetry without leaking secrets.
5. Agree on monitoring/rollback, the expected rate of new signals and a time
   window for watching the first genuine *post-freeze* event.

If any item is unverified, **STOP**. Do not write a freeze, enable flags or
restart existing production services.

### Operator sequence, only AFTER an explicit separate GO decision

1. Freeze the reviewed code/spec and confirm the preflight above is PASS,
   activation is exactly `READY_TO_START`, stored fingerprint/freeze NULL,
   and all OOS tables empty. Capture the intended *new* UTC boundary at the
   actual activation time; never recycle a sample timestamp from documentation.
2. Use the existing `scripts/me_inverse_short_v1_activate.py --show` with
   the **dedicated runtime database environment explicitly loaded**. It calls
   `load_settings()` and **does not itself read the systemd EnvironmentFile**;
   a bare invocation could point to the wrong database. Verify the reported
   experiment ID and current fingerprint first. Never paste DB passwords into
   terminal commands or logs.
3. Only after confirming the Scanner's effective flag can be enabled and
   the source stream is available, execute the explicitly approved
   `--activate --freeze-ts <NEW_UTC_TIMESTAMP>` once with those same dedicated
   DB settings. The script atomically fixes `RUNNING`, current fingerprint
   and freeze_ts. **Do not use `--re-freeze`.**
4. Enable `ME_INVERSE_SHORT_V1_ENABLED=1` for the dedicated runtime **and
   separately for the Scanner's effective process environment** through the
   existing approved deployment procedures. Restart only the dedicated unit
   and (if explicitly authorized and necessary) the Scanner service.
   Do not alter Scanner trading gates, Paper, or Research Evaluator.
5. Check both activation factors, `FULL` telemetry, scanner source capture
   status (errors/duplicates/captured), event lineage and strictly
   `signal_time > freeze_ts`, first paper-only positions, ongoing cycle/DB/API
   errors and pending close-outbox. An absence of new source signals is
   not a PASS; diagnose whether the scanner is actually emitting them.
6. On a stop decision: first disable source intake safely in the Scanner,
   drain `RUNNING -> DRAINING` via `--drain` using the same verified
   database settings, and ensure the runtime stays enabled in EXIT_ONLY
   mode to manage existing positions/outbox. Do **not** immediately disable
   the runtime flag while positions or closes are pending; it prevents
   exit-only management. Preserve freeze, fingerprint and all event history.

**Not performed by PR #271:** freeze/activation, Scanner env changes, any
production DB write, VPS deploy, or production restart. Activation is an
independent controlled change.

### Explicit operator CLI shape (documentation ONLY; separate GO required)

```bash
cd /opt/trad_bot_me_inverse_short_release
.venv/bin/python scripts/me_inverse_short_v1_activate.py --show \
  --env-file /etc/trad-bot/me-inverse-short-runtime.env

# AFTER separate written GO, use a FRESH CURRENT UTC timestamp:
# .venv/bin/python scripts/me_inverse_short_v1_activate.py --activate \
#   --freeze-ts <FRESH_UTC_ISO8601> \
#   --env-file /etc/trad-bot/me-inverse-short-runtime.env

# Drain only with exit-only recovery plan (do not disable runtime mid-close):
# .venv/bin/python scripts/me_inverse_short_v1_activate.py --drain \
#   --env-file /etc/trad-bot/me-inverse-short-runtime.env
```

Run as a user who can read the protected EnvironmentFile. No shell sourcing
of database secrets is needed. Scanner's separately effective flag remains
a mandatory operational hold point.
