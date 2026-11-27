# H1-E limiter integration — implementation checklist (Issue #328)

**Status:** specification only; NO implementation in this commit. PR is intentionally DRAFT.

See [#328](https://github.com/eignat88/trad_bot/issues/328), parent [#324](https://github.com/eignat88/trad_bot/issues/324).

## Agent deliverables

- Verify actual H1-E worker and BybitClient startup path in current main.
- Load approved live limiter contract explicitly; fail closed if disabled, missing, incompatible or wrong path.
- Pin shared state to `/var/lib/trad_bot/state/global_bybit_limiter_v1.json` for the intended VPS deployment, without introducing a separate state file.
- Validate shared semantic fingerprint and permissions before HTTP.
- Document cross-UID ACL for state, lock, witness and atomic temp files, with future-file inheritance and rotation.
- Add offline tests for wrong/disabled config, mismatched fingerprint, denied filesystem access, withheld grant, restart and cross-process quota.
- Update isolated H1-E deployment runbook, keeping worker disabled.

## Hard gates

Research only. Do not alter production scanner/paper/trading gates or #305 freeze. No VPS deployment, permissions changes, DDL, service start, API calls, backfill or H1-E activation without separate authorization.

## PR completion

Replace this checklist with a code diff, tests, and reproducible evidence. Remain draft until tests and review pass.