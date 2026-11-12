# ME Inverse SHORT V2 — systemd drop-in aware preflight (implementation specification)

Status: **SPEC ONLY / FIX_REQUIRED / DRAFT PR**. This document defines the expected change; it does **not** implement the fix.

## Production evidence (2026-11-11, `trad-bot-01`)

- Active Scanner: `trad-bot-scanner.service`, real process `/opt/trad_bot/.venv/bin/python /opt/trad_bot_pr267_release/scanner_runner.py`, cwd `/opt/trad_bot_pr267_release`, V1 capture ON.
- Base unit file: `ExecStart=/opt/trad_bot/.venv/bin/python /opt/trad_bot/scanner_runner.py`; `WorkingDirectory=/opt/trad_bot`; `EnvironmentFile=/opt/trad_bot/.env`.
- Multiple ordered drop-ins override `ExecStart` and `WorkingDirectory`. Effective `systemctl show` points to `/opt/trad_bot_pr267_release`, not the base unit.
- Candidate Scanner checkout `/opt/trad_bot_scanner_v2_release` staged at `956331eb135ca00475d4e0f5fb2638cd51988baf`, with its own symlink-based venv, imports and compilation checked.
- V2 runtime separately installed, OFF (`ME_INVERSE_SHORT_V2_ENABLED=0`), migration 087 applied; V2 DB `READY_TO_START` with NULL fingerprint/freeze and 0 events/positions/outbox.
- `scripts/me_inverse_short_v2_preflight.py` currently parses the **base** unit file for structural checks, while running-process checks involve effective systemd metadata. This creates false `SCANNER_UNIT_CHECKOUT_MISMATCH` after a valid drop-in switch.
- Scanner's `/opt/trad_bot/.env` does not yet contain `ME_INVERSE_SHORT_V2_ENABLED`; an override-only setting does **not** satisfy the existing separate env-file preflight contract.
- Existing Scanner service drop-ins include canary config, ME SHORT A fill capture, SRR fill capture and V1 inverse SHORT capture; preserve all.

## Goal

Make V2 preflight validate the *effective* installed Scanner systemd unit configuration, including ordered drop-in overrides, and the *actual running Scanner process*. No relaxation of fail-closed gates and no change to V1 or trading logic.

## Requirements

1. Read effective `ExecStart`, `WorkingDirectory`, and `EnvironmentFiles` from `systemctl show trad-bot-scanner.service` (or a demonstrably equivalent systemd-aware source); do not trust the base unit alone when drop-ins exist. Validate `FragmentPath`, `DropInPaths`, unit/load state and missing/ambiguous values. Handle `ExecStart=` resets and overrides. Do not parse `ExecStart` structured output via naïve whitespace splitting.
2. Validate effective executable, script and cwd against explicit `--scanner-root`; `ExecStart` Python must reside in a genuine checkout-local `<venv>/bin/python*` with `pyvenv.cfg`; allow Linux interpreter symlink to system Python, reject a foreign/escaped venv (preserve PR #275 P2).
3. Preserve effective unit ↔ running `/proc/<pid>/cmdline`, `/proc/<pid>/cwd`, interpreter, process start vs audited source mtimes and active MainPID checks; distinguish active loaded effective config from **pending** disk edits not yet loaded by systemd; no false readiness when stale process still runs previous release.
4. Confirm the Scanner source audit reads `--scanner-root` (actual release checkout), not `/opt/trad_bot` and not the V2 *runtime* release.
5. `scanner_unit_env` must verify the effective EnvironmentFile really includes `--scanner-env-file`. Preserve existing scanner env-file parsing and V2 flag=0 requirement and DB tuple parity. Do not mark PASS based only on `Environment=ME_INVERSE_SHORT_V2_ENABLED=0` in a drop-in.
6. Preserve V1 isolation audit and capture, capture-source V1/V2 split, six readiness gates, flags, frozen fingerprints, `freeze_ts`, all paper/live behavior.
7. Fail closed: missing, unreadable or ambiguous systemd/process evidence `NOT_VERIFIED`; a clear path/process mismatch `FAIL`. Only `PASS` when effective loaded unit, running process, checked-out source, Python venv and scanner env have all been confirmed.
8. Add deterministic unit tests with a fake/mock systemctl output and a real filesystem fixture: base unit + multiple ordered drop-ins, `ExecStart=` resets, space-safe argv handling, good process, stale process, mismatched cwd and Python, missing unit, orphaned/modified drop-in, wrong env, absent V2 flag, invalid/foreign venv, correct Linux symlink-based venv. Retain existing tests.
9. Update `docs/me_inverse_short_v2_production_runtime_v1.md` to document release-based scanner path and effective unit verification, read-only diagnostics, expected PASS/FAIL/NOT_VERIFIED, and separate explicit Scanner switch GO.
10. Keep preflight read-only; no production writes, systemd restart/reload, DB mutation, Bybit orders, migration, activation or backfill.

## Acceptance

- Fixture matching production: base unit points `/opt/trad_bot`; later drop-in points `/opt/trad_bot_scanner_v2_release`; effective systemctl state, process cmdline and cwd agree; symlink venv is valid; source audit and Scanner .env flag=0 pass ⇒ `V2_SOURCE_CAPTURE_READY=PASS` and `V1_ISOLATION_VALID=PASS` (given the other prerequisites).
- Old Scanner process running despite proposed new drop-in ⇒ never PASS.
- Base unit differs but loaded effective state is correct ⇒ no false structural FAIL.
- Effective unit points elsewhere than `--scanner-root` ⇒ FAIL.
- Multiple drop-ins / masking reset / unreadable effective metadata ⇒ conservative result, never fabricated PASS.
- Existing four CI workflows green; focused tests green; no new failing test IDs versus established baseline.

## Deployment separation / explicit operator gates

This PR is **code + tests + runbook only**. It must NOT modify `/opt/trad_bot`, `/opt/trad_bot_pr267_release`, `/opt/trad_bot_scanner_v2_release`, `/etc/systemd/system`, `/opt/trad_bot/.env`, the DB, or any running VPS service. The staging override `/tmp/me_inverse_short_v2_scanner_switch/zzzzzzzz-scanner-v2-release.conf` is **not installed**. After merged, a separate operator GO is needed for updating the release, Scanner env, and restarting Scanner; V2 activation requires a different explicit GO.
