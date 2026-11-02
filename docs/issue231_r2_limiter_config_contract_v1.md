# ISSUE231 R2 — GLOBAL_BYBIT_RATE_LIMITER_V1 configuration contract

Status: `ISSUE231_R2_PATCH_READY_FOR_REVIEW`
Scope: configuration only. No token-bucket, breaker, fairness, retry or
accounting logic changed; the limiter stays **default-off** and is **not
enabled anywhere in production** by this change.

## 1. Activation precedence (documented, deterministic, fail-closed)

| # | Source | Condition | Effect |
|---|--------|-----------|--------|
| 1 | ENV `GLOBAL_BYBIT_RATE_LIMITER_ENABLED` | config file sets `"global_bybit_rate_limiter_allow_env": true` **and** the ENV var is set | ENV value wins over YAML (`true/1/yes/on` ⇒ on, `false/0/no/off` ⇒ off, anything else ⇒ `ValueError`) |
| 2 | config.yaml `"global_bybit_rate_limiter_enabled"` | always evaluated | strict bool: literal `true`/`false` only; strings/numbers/null ⇒ `ValueError` (Issue 231 R1 preserved) |
| 3 | built-in default | when neither source provides a value | `False` (default-off) |

Rules that make conflicts predictable:

* **Two-factor ENV gate.** The ENV variable may only activate the limiter
  when the *config file* also grants permission with
  `"global_bybit_rate_limiter_allow_env": true`. The permission flag is
  read **from the config file only** — it can never be granted by an ENV
  variable (a same-named ENV var is ignored).
* **Without the permission the ENV variable is not a configuration
  source.** It is ignored for activation (a warning naming the variable is
  logged) — a stray value can neither switch the limiter on nor break
  startup.
* **Config-file type errors are always fatal.** Precedence only decides
  between *valid* values; a malformed YAML/JSON value raises `ValueError`
  even when a permitted ENV value would win.
* **Permission flag is strict boolean** (`parse_strict_bool_flag`): only
  literal `true`/`false` in the config file; `"true"`, `1`, `null`, … ⇒
  explicit `ValueError`.

Example (dev/staging activation, both factors required):

```json
{
  "global_bybit_rate_limiter_allow_env": true,
  "global_limiter_state_path": "/var/lib/trad_bot/state/global_bybit_limiter_v1.json"
}
```

```ini
GLOBAL_BYBIT_RATE_LIMITER_ENABLED=true
```

## 2. Validated parameters

All keys are read by `LimiterConfig.from_settings()` and validated
**inside `load_settings()`** — an invalid value raises `ValueError` at
settings-load time, before any `BybitClient` is constructed, before any
shared-state file is opened and before any HTTP request could be issued.

| Key | Type (strict) | Default | Valid range / rule |
|-----|---------------|---------|--------------------|
| `global_bybit_rate_limiter_enabled` | bool | `false` | literal `true`/`false` |
| `global_bybit_rate_limiter_allow_env` | bool | `false` | literal `true`/`false`, config file only |
| `global_limiter_state_path` | string | `var/state/global_bybit_limiter_v1.json` | non-empty, no NUL; absolute required when enabled in `trading_mode: live` |
| `global_limiter_g_rps` | number | `8.0` | finite, `> 0` |
| `global_limiter_burst` | number | `12.0` | finite, `>= 1` |
| `global_limiter_lock_timeout_s` | number | `5.0` | finite, `> 0` |
| `global_limiter_demand_ttl_s` | number | `120.0` | finite, `> 0` |
| `global_limiter_breaker_cooldowns_s` | list of numbers | `[5.0, 30.0, 120.0]` | non-empty, each finite and `>= 0` |
| `global_limiter_recovery_success_target` | integer | `20` | whole number `>= 1` |
| `global_limiter_probe_lease_s` | number | `30.0` | finite, `> 0` |
| `global_limiter_registry_capacity` | integer | `8192` | whole number `>= ceil(2 · G · max_lifetime_s)` |
| `global_limiter_max_lifetime_s` | number | `60.0` | finite, `> 0` |
| `global_limiter_max_wait_s` | number | `30.0` | finite, `> 0` |
| `global_limiter_pending_liveness_ttl_s` | number | `15.0` | finite, `> 0` |
| `global_limiter_record_retry_attempts` | integer | `3` | whole number `>= 1` |
| `global_limiter_record_retry_backoff_s` | number | `0.05` | finite, `>= 0` |

Strictness notes:

* Booleans are rejected for numeric keys (`true` is not `1`).
* Strings (`"8"`), `null` and containers are rejected — never coerced.
* `NaN` / `Infinity` are rejected (JSON parses them literally and they
  silently poison every range comparison).
* Integer keys accept an integral float (`20.0` → `20`) and reject
  fractional floats (`20.5`).
* No arbitrary upper caps were invented for G/B: consistency is enforced
  through the registry-capacity rule (`capacity >= ceil(2·G·max_lifetime)`),
  so an absurd G with a small registry fails explicitly.

## 3. Shared state path safety

* **Production (`trading_mode: live`) + enabled ⇒ absolute path required.**
  A relative (or default) path is rejected with an explicit error; the
  operator must point `global_limiter_state_path` at a shared absolute
  location (e.g. `/var/lib/trad_bot/state/...`).
* **No WorkingDirectory dependency.** Relative dev/paper paths are
  anchored at the *repository root* (derived from the module location,
  never `os.getcwd()`), lexically normalised. Every service process
  resolves the same state/lock/witness files regardless of its start
  directory or a later `chdir`.
* **Multi-user access.** When the limiter is enabled, `load_settings()`
  verifies that an already-existing parent directory (and an existing
  state file) is writable by the current service user; a non-writable
  location fails at load time instead of at first runtime transaction. A
  not-yet-existing directory is accepted (created on first acquire).
* **World-writable directory ⇒ rejected.** On POSIX an enabled limiter
  refuses to start when the state directory has the world-write bit set
  (any local account could tamper with the shared bucket).
* **Symlinked state file ⇒ rejected** at load time; at runtime every
  state/lock/witness open uses `O_NOFOLLOW`, so a planted symlink fails
  closed (`ELOOP`) instead of being followed. (Windows has no
  `O_NOFOLLOW`: symlink following there remains a documented
  single-user-platform limitation.)
* When the limiter is **disabled**, no path probing happens at all
  (default-off keeps the zero-I/O contract).

### 3.1 Multi-user deployment scheme (POSIX, e.g. tradbot + tradbot-analytics)

The persistence layer supports ONE shared `state_path` for several
service users with these **operator-provided** prerequisites (this patch
documents them; it performs **no** user/group/systemd/directory changes
itself):

```sh
# 1. one shared group holding both service users
groupadd tradbot
usermod -aG tradbot tradbot-analytics

# 2. a group-owned SETGID directory (2770): every file created inside
#    inherits the tradbot group, so ownership flaps between writers
#    without ever losing group access
install -d -o tradbot -g tradbot -m 2770 /var/lib/trad_bot/state

# 3. identical ABSOLUTE state path in every consumer's config.yaml
#    "global_limiter_state_path": "/var/lib/trad_bot/state/global_bybit_limiter_v1.json"

# 4. one-time migration of leftovers created by older builds (deploy
#    step — NOT performed automatically: the code never chmods a file it
#    did not create)
chgrp tradbot /var/lib/trad_bot/state/global_bybit_limiter_v1*
chmod 660  /var/lib/trad_bot/state/global_bybit_limiter_v1*
```

Rights contract enforced by the code (no umask assumptions):

| File | Created by | Mode after publication | Notes |
|------|------------|------------------------|-------|
| `*.json` (state) | `O_CREAT\|O_EXCL` private `0600`, `fchmod(0660)` **before** the atomic `os.replace` | `0660` | every update preserves mode `0660` + the setgid-inherited group; a foreign existing file is **never** re-moded (no implicit migration) |
| `*.json.lock` | same create-then-publish strategy | `0660` | existing lock inode is opened without create → flock mutual exclusion across UIDs; never replaced |
| `*.witness` | same | `0660` | `FileNotFoundError` ⇒ “not held” (existing contract); `EACCES`/`EPERM`/`ELOOP` ⇒ **treated as held** (fail-closed, no second recovery probe) |

* `UMask=002` in the service units is recommended but **not relied
  upon** — shared rights are applied with `fchmod` on the creating
  process's own descriptor (umask cannot weaken them).
* Failure behaviour stays fail-closed: `lock_unavailable`,
  `witness_failed`, `read_failed`, `write_failed` → `STATE_UNAVAILABLE`
  deny → `BybitRateLimitError (not sent)` with **zero HTTP attempts and
  no fallback** to the process-local limiter.
* Startup ordering caveat (residual, fail-closed): if both services start
  simultaneously and one observes the other's file during the
  microsecond private-`0600` window, that first request is denied
  (`lock_unavailable`) and succeeds on the next attempt.

## 4. Failure behavior

Every contract violation raises `ValueError` with the offending key name
at one of three choke points:

1. `load_settings()` — ENV gate, YAML strict bool, parameter
   types/ranges, state-path access;
2. `LimiterConfig.from_settings()` — build-time re-validation (defense in
   depth for direct `Settings(...)` construction);
3. `LimiterConfig.__post_init__()` — state-path text/normalisation.

No limiter state file is opened, no lock is taken and **no HTTP request
is issued** for any invalid configuration (transport poisoned in tests).

## 5. Tests

`tests/test_issue231_r2_config_contract_v1.py` — default-off, permitted
activation, denied activation, ENV/YAML conflicts, permission isolation,
wrong types, ranges (G/B/TTL/breaker/registry/…), production-absolute and
cwd-independence of the state path, shared-path writability, and
no-unexpected-HTTP assertions. R1's `tests/test_issue231_strict_limiter_flag_v1.py`
remains unchanged and must keep passing.

`tests/test_issue231_r2_multi_user_persistence_v1.py` — multi-user
persistence contract (§3.1): `0660` group-shared modes after creation and
after every atomic `os.replace`, umask `022`/`002` independence,
`EACCES`/`EPERM` fault injection (fail-closed deny, no second recovery
probe), `FileNotFoundError` witness contract, symlink rejection for
state/lock/witness, world-writable directory rejection at load, zero-HTTP
no-fallback on persistence errors, and a root-gated Linux integration
with two distinct UIDs in an isolated temporary tree.
