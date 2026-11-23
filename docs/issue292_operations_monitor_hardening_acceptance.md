# Issue #292 — Operations monitor hardening / final acceptance

Status: **OPEN / PARTIAL ACCEPTANCE**. This is a plan and operator evidence ledger, **not a claim that any production hardening code has shipped**.

## Existing verified evidence (2026-11-23, operator VPS output)

- Dedicated `trad-bot-monitor.service` remained active since 2026-11-20 13:07:37 UTC, PID 204968, `NRestarts=0`.
- Four service-sample series: 2693 samples each in a rolling 24h window; maximum interval 33.2–33.4s; zero gaps >90s; latest cursors ~6–30s.
- Exact 60m UTC [08:29,09:29) reconciled per-unit journal/DB CALL totals: Scanner 2424/2424; OOS2 V2 3969/3969; Inverse V3 4003/4003; **10396/10396**.
- Separate longer check [2026-11-22 08:00, 2026-11-23 08:00) had Scanner journal 45790 vs DB 57708 (delta 11918). Mismatch confined to early ~5h where journald was no longer available. Earliest available Scanner record was 2026-11-22 12:57:32 UTC; journald configured with `SystemMaxUse=300M`. This is **SOURCE HISTORY UNAVAILABLE**, not evidence of duplicate DB ingestion. Exact full-24h reconciliation remains unproven.
- Grafana Operations dashboard V3 21 panels, SQL classification corrected on deployed JSON panels 14/20. Actual 24h observed: SKIP=109, 10006=99, other ERROR=2. The V3 deployed file SHA256 was `a95f84ce8fa539610bbd765c747891bee2bee6ab79ec8b97c7ca6e6ddb819f1e`. Confirmed screenshot of final error/time and collector freshness panels; no claim about future refreshes.
- Dashboard JSON-only drill 21→11→21 and checksum restore succeeded; trading service and collector PIDs did not change. This **does not** verify Grafana API 11→21 response snapshots.
- Systemd: Scanner ON (under separate #290), OOS2 V2 OFF, inverse V3 OFF, legacy OOS2 V1 disabled/inactive and PID-pinned state UNKNOWN. This is not a wire-rate limiter effectiveness test.
- `monitoring.api_metric_sec` ~39MB and ~148k aggregate rows after ~69h. No retention timer discovered. Monitor reads via `journalctl --after-cursor`; `commit_batch` groups events and cursor in a PostgreSQL transaction. Malformed cursor returned exit 1 and no output. **Deleted-but-well-formed cursor path not tested.**

## Required implementation — source parity first

The running source is `/opt/tradbot-monitor/trad_bot_monitor.py`. This file has **not** been located as a versioned source in repository `main`. Before any code change:
1. Capture the exact deployed file **to a development workstation/review branch**, ensuring no credentials are embedded; record SHA256 and Python version.
2. Commit it under `monitoring/operations/` (or a scoped dedicated module) as a separate source-parity change, with **no behavior modification**.
3. Review the entire implementation, its permissions and `systemd` configuration; only then implement and test fixes. **Do not deploy code reconstructed from partial pasted snippets**.

## Cursor gap fail-closed contract

- Treat `journalctl` stdout alone, exit=0, or empty output from `--after-cursor` as **insufficient proof** that the stored cursor remains addressable.
- Before processing resumed records, prove exact cursor match in available journal (e.g. compare `__CURSOR` values), with bounded work and an explicit `UNKNOWN` outcome on timeout/scan cap.
- If stored cursor is absent, journal truncated since the last committed cursor, or source completeness cannot be proven: **fail closed**, no advancing cursor or increments for that unit, emit an explicit `SOURCE_GAP_DETECTED` evidence record with unit, timestamp, detection type, earliest/latest available journal timestamps and last committed cursor metadata (avoid raw log payload and secrets).
- Do not infer missing event counts from counters; no blind reset to tail and no retroactive backfill. Allow prospective resumption only after an operator-approved new boundary, with immutable gap record and original evidence retained.
- Atomic cursor+metric write, bounded line capture, at-least-once retries with idempotent recovery, restart behavior, and de-duplication must all be tested. Test malformed cursor; valid but rotated-away cursor; empty journal; restart; DB failure; 3000-line cap; journal read timeout; duplicate delivery.
- Do not alter trading services, API consumer configs, limiter JSON/locks, OOS registry/freezes, paper/live processes, or any research eligibility data.

## Retention contract — needs approval before implementation

- Choose explicit retention for `monitoring.api_metric_sec` and `monitoring.service_sample`, plus storage budget, before introducing deletion. Example candidate **30d (not approved)**; journal raw retention is independently constrained by `SystemMaxUse=300M`.
- Maintenance job must use bounded batches, selective timestamp index, metrics about deleted count/oldest row and runtime, isolated service identity, dry-run mode, and deployment/rollback runbook; avoid interfering with ingestion.
- Never delete `monitoring.journal_cursor` as part of retention. Keep audit gap records longer than aggregate retention as agreed.
- Verify before/after table size, collector latency, locks, no unbounded SQL, and routine autovacuum behavior. No unconditional deletes on live DB before operator GO.

## Remaining acceptance checks

- [ ] Source parity and review on a branch; unit/integration tests for lost-valid-cursor fail-closed and recovery.
- [ ] 24h scanner cadence: count completed cycles, failed/skipped symbols, cycle duration baseline vs follow-up; distinguish journal availability limitations.
- [ ] Reconcile full 24h **only when source covers it**, otherwise explicit source-unavailable verdict and evidence (no fabricated PASS).
- [ ] Verify 10006, HTTP429, pre-send SKIP, retries and ERROR categories against real or safe synthetic records; no intentional live Bybit rate limits.
- [ ] Confirm collector limiter-state freshness and queue-wait stats **only if structured telemetry exists**.
- [ ] Version-control deployed Grafana V3 JSON after exact VPS parity check, validate all 21 panels/query semantics; authenticated Grafana API verification of provisioned revision.
- [ ] Review retention option and obtain separate operator GO before production maintenance.
- [ ] Isolated monitor-only deployment, backup, rollback, health validation and evidence in #292. Keep #290 canary decision entirely separate.

## Safe next operator commands (READ-ONLY)

```bash
sudo sha256sum /opt/tradbot-monitor/trad_bot_monitor.py
sudo stat -c '%n %U:%G %a %s bytes' /opt/tradbot-monitor/trad_bot_monitor.py
sudo -u postgres psql -d trad_bot -X -P pager=off -c "
SELECT unit_name, updated_at, NOW()-updated_at AS age
FROM monitoring.journal_cursor ORDER BY unit_name;"
systemctl show trad-bot-monitor.service -p MainPID -p NRestarts -p ActiveState
```

There is **no VPS deploy approval** implicit in this document or in the draft PR. Issue #292 remains open until hardening acceptance, or explicit owner decision to split unfinished criteria into a separate issue.
