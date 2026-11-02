# ISSUE231 — isolated Scanner-only canary configuration (`SCANNER_CONFIG_PATH`)

Status: `IMPLEMENTED / TESTED / PR READY / NOT DEPLOYED / LIMITER OFF`
Scope: development + tests only. Nothing in this document has been executed
against production; no service, `config.yaml`, `.env`, systemd unit, database
or trading gate is modified by this change.

## 1. Why this exists

R1–R3 of GLOBAL_BYBIT_RATE_LIMITER_V1 are code-complete and **default-off**.
The remaining risk for a first canary is configuration coupling: `Paper`,
`Research`, `HTF` and `Analytics` all read the *same* shared
`/opt/trad_bot/config.yaml`. Flipping
`global_bybit_rate_limiter_enabled: true` in that file would arm the shared
limiter for **every** consumer at its next restart — not for Scanner alone.

`scanner_runner.py` therefore gains one optional environment variable that
changes **only the config-file source for the Scanner process**:

| `SCANNER_CONFIG_PATH` | Config source | Result |
|---|---|---|
| unset | `DEFAULT` → `<repo>/config.yaml` (production) | byte-identical behaviour to today, limiter OFF |
| set to a valid absolute JSON path | `OVERRIDE` → that file | Scanner-only settings, every other consumer untouched |

`.env` handling (API keys, PostgreSQL credentials, `TRADING_MODE`, …) is
**not** touched by the variable: only the JSON/YAML config-file source
changes, and the global limiter is never auto-enabled.

## 2. Override validation (fail-closed)

Validation runs in `resolve_scanner_config_path()` **before**
`load_settings()` reads the file and **before** `main()` constructs a
`BybitClient` — therefore before any Bybit request can be issued.

| Condition | Behaviour |
|---|---|
| variable absent | `DEFAULT` source, historical behaviour |
| empty / whitespace-only value | `ScannerConfigError` |
| relative path (`config.json`, `./x.json`, `C:x.json`) | `ScannerConfigError` |
| path does not exist | `ScannerConfigError` — **no fallback** to `config.yaml` |
| path is a directory | `ScannerConfigError` |
| not readable by the service user | `ScannerConfigError` |
| malformed JSON / invalid settings | `ScannerConfigError` (wraps the original `ValueError`) |
| path unstatable (`OSError`) | `ScannerConfigError` |

`ScannerConfigError` subclasses `ValueError`, so the historical
`load_settings()` error contract is preserved while the message now names
the failing source and path.

Accepted absolute forms are checked with `PurePosixPath` **and**
`PureWindowsPath`, so `/srv/trad_bot/scanner_canary.json` (Linux),
`C:\srv\…` / `C:/srv/…` and UNC paths are absolute on both platforms, while
the process WorkingDirectory is never consulted.

## 3. Startup logging

One `INFO` line is written at Scanner start (plus a `CRITICAL` line on
failure):

```
scanner config: source=OVERRIDE config=/opt/trad_bot/scanner_canary.json \
  env_file=/opt/trad_bot/.env cwd=/opt/trad_bot \
  global_limiter=ON allow_env=DENIED \
  state_path=/var/lib/trad_bot/state/global_bybit_limiter_v1.json \
  universe_mode=dynamic top_n=50 symbols=[...]```

Reported fields: source (`DEFAULT`/`OVERRIDE`), resolved config path,
`.env` path, limiter state (`ON`/`OFF`), ENV-activation permission
(`ALLOWED`/`DENIED`) and the shared state path. API keys, secrets, tokens,
passwords and `.env` contents are never logged (regression-tested).

## 4. Future canary procedure (NOT executed — requires an operator GO)

> This section is a runbook only. No canary file is created, no service is
> restarted and the limiter stays **OFF** until a separate operator GO.

1. **Prepare the canary file** as a *full copy* of the production config —
   never the minimal illustration JSON:

   ```bash
   sudo -u tradbot cp /opt/trad_bot/config.yaml /opt/trad_bot/scanner_canary.json
   sudo -u tradbot chmod 640 /opt/trad_bot/scanner_canary.json
   ```

   Then edit **only** these three keys inside `scanner_canary.json`:

   ```json
   {
     "global_bybit_rate_limiter_enabled": true,
     "global_bybit_rate_limiter_allow_env": false,
     "global_limiter_state_path": "/var/lib/trad_bot/state/global_bybit_limiter_v1.json"
   }
   ```

   Everything else (symbols, scanner universe, trading mode, DB settings,
   direction gates, scoring, expectancy, DCA, execution policies) must stay
   identical to production.

2. **Verify the shared state directory is ready** (already prepared for
   R1–R3, mode `2770`, group `tradbot`, members `tradbot` and
   `tradbot-analytics`):

   ```bash
   ls -ld /var/lib/trad_bot/state        # drwxrwx--- tradbot tradbot
   ```

3. **Dry-run the Scanner config resolution** (read-only, no restart):

   ```bash
   sudo -u tradbot env SCANNER_CONFIG_PATH=/opt/trad_bot/scanner_canary.json \
     /opt/trad_bot/.venv/bin/python - <<'PY'
   import scanner_runner
   path, source = scanner_runner.resolve_scanner_config_path()
   settings = scanner_runner._load_runner_settings()
   print("source", source, "path", path)
   print("limiter", settings.global_bybit_rate_limiter_enabled,
         "allow_env", settings.global_bybit_rate_limiter_allow_env,
         "state_path", settings.global_limiter_state_path)
   PY
   ```

   Expected: `source OVERRIDE`, `limiter True`, `allow_env False`, the
   production state path. Any error aborts here with
   `ScannerConfigError` — nothing has been restarted.

4. **Arm the Scanner service only.** Add the variable to the *Scanner*
   unit (drop-in — the base unit file is unchanged):

   ```bash
   sudo systemctl edit trad-bot-scanner
   ```

   ```ini
   [Service]
   Environment=SCANNER_CONFIG_PATH=/opt/trad_bot/scanner_canary.json
   ```

   Then `sudo systemctl daemon-reload && sudo systemctl restart trad-bot-scanner`.

5. **Confirm isolation.** `journalctl -u trad-bot-scanner -n 50` must show
   `source=OVERRIDE`, `global_limiter=ON`; `journalctl -u trad-bot-paper`
   (and Research/HTF/Analytics) must show `source=DEFAULT`,
   `global_limiter=OFF`. Paper, Research, HTF and Analytics must **not** be
   restarted for this canary.

6. **Observe** the shared limiter state file
   `/var/lib/trad_bot/state/global_bybit_limiter_v1.json` and the existing
   Issue #231 dashboards/logs for the agreed observation window.

## 5. Rollback procedure

Rollback is a Scanner-only, configuration-only operation — no code revert,
no DB change, no data migration.

```bash
sudo systemctl edit --delete-unit-env trad-bot-scanner   # or delete the drop-in
sudo systemctl daemon-reload
sudo systemctl restart trad-bot-scanner
sudo rm -f /opt/trad_bot/scanner_canary.json             # optional
```

Expected after rollback:

* the Scanner logs `source=DEFAULT … global_limiter=OFF`;
* the shared limiter state file stops being written by Scanner;
* every other service is unaffected because it never read the canary file.

If the Scanner fails to start during the canary (bad override path, bad
JSON, unreadable file), it exits non-zero with a `CRITICAL`
`scanner config load FAILED: source=…` line and never contacts Bybit;
fixing or removing `SCANNER_CONFIG_PATH` restores the previous behaviour.

## 6. Safety guarantees

* Production `config.yaml` still contains
  `global_bybit_rate_limiter_enabled: false` (default-off preserved).
* No automatic limiter activation: `SCANNER_CONFIG_PATH` selects a file,
  it never flips a flag.
* No changes to trading gates, regime filters, scoring, strategies,
  Paper/Live order logic, OOS protocols, freeze timestamps, checkpoints,
  `BybitClient`, token bucket, breaker, fairness, retry accounting,
  frozen ME R V3, systemd units, `.env`, or the database.
* Regression coverage: `tests/test_issue231_scanner_config_path_v1.py`
  (27 cases) plus the existing Issue #231 R1/R2/R3 suites.
