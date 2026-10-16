# ME_R_V3_IMMUTABLE_RELEASE_AND_SYSTEMD_ISOLATION_PREPARATION_V1

Issue: #154. Status: **DRAFT / REVIEW ONLY / NOT DEPLOYED / NOT ACTIVATED**.

## Verified starting point (2026-10-16)

- Production `/opt/trad_bot`: HEAD `5f7a99a6`, tracked local edit to `app/research/prospective_registry.json`. Scanner, paper and research evaluator active. **Never pull/reset/clean/copy over this tree.**
- Isolated PostgreSQL database `trad_bot_me_r_v3_oos` contains migrations 068–072. Dedicated `market.candle` and `dds.instrument` exist; instrument/candle/source_event/variant_outcome rows were all zero immediately after migration. These facts are operator VPS outputs, not independent live checks.
- Backup archive: `/var/backups/me_r_v3_issue154/trad_bot_me_r_v3_oos_20261016T074515Z.dump`, SHA256 `249cbbec83f6f4efc8f976e9d27af07c900d22b838fe55da0d8fd74a41795b9c`. Restore drill: 5 research tables, 19 triggers, 44 constraints, 0 sources/outcomes and parity with source on measured counts. Dump excludes ownership/ACL restoration.
- Dedicated OS and DB role: `trad_bot_me_r_v3_oos`; systemd service and timer **not installed**. Config `/etc/trad-bot/me-r-v3-oos.env` absent.
- Frozen protocol: version 3.0.0, source scanner 1.0.0, `FROZEN_AT=2026-10-16T12:00:00Z`, `ACTIVATION_AT=2026-10-19T12:00:00Z`, approved fingerprint `ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a`. Cutoff is a strict observation boundary, **not a start instruction**.
- Universe: 34 fixed symbols, SHA256 `19c5807e510e75ff1e19bdca8bdad469e05a9c2198266ab27821570425a28bf8`. Ambiguous underlying categories still need confirmation against exchange metadata.
- Statistics: PR #148 merged to main; `METHOD_NOT_APPROVED`, `HOLDOUT_NO_GO` remain.

## Proposed release contract (no deployment in this PR)

1. Build from a **specific reviewed main commit** to a new **root-owned read-only** `/opt/trad_bot_me_r_v3_release` tree. Do not use a mutable Git checkout, `git pull`, symlink to production source, or a `current` symlink that can switch silently. Record immutable source commit, normalized source archive SHA256, unit/timer SHA256, Python version, dependency lock/hash manifest and venv provenance before deployment. If later updates are needed, prepare a new separately reviewed release/cutover; no in-place edit while enabled.
2. Create and validate a **separate** `/opt/trad_bot_me_r_v3_release/.venv`, never sharing `/opt/trad_bot/.venv`. A dependency freeze or lock must be verified prior to installation; this PR **does not claim dependency isolation has been proven**. Audit import-time side effects of `app.config.Settings`, `BybitClient` and telemetry for files outside the release, production credentials, or production DB.
3. Release directories and files must be non-writable by `trad_bot_me_r_v3_oos` (e.g. root:root), readable/traversable by that user, and never writable by existing trading-service accounts. No source edits, credential files or generated artifacts inside release. `PYTHONDONTWRITEBYTECODE=1`.
4. The service in this PR has `WorkingDirectory=/opt/trad_bot_me_r_v3_release` and `ExecStart=/opt/trad_bot_me_r_v3_release/.venv/bin/python ...isolated_oos --once`; requires `/etc/trad-bot/me-r-v3-oos.env` (no optional `-` prefix). It sets `ME_EARLY_MAE_V3_CAPTURE_ENABLED=false` by default; an independently approved env must explicitly opt in to run. **No service start or timer enable is authorized here.**
5. Before any installation, verify environment-file owner/mode, contents **privately** (never in tickets/logs), approved timestamps/fingerprint, 34-symbol CSV exactly, PostgreSQL local socket identity, least privilege and no access to `trad_bot`, and fail-closed behavior with missing or incorrect config. An env file by itself is not approval to activate.
6. Verify unit syntax with `systemd-analyze verify`, calendar with `systemd-analyze calendar '*-*-* *:0/5:30 UTC'`; confirm unit `disabled/inactive` after any future **separately authorized** installation. Timer `Persistent=false` prevents catch-up, but a cycle and source processing must still enforce strict cutoff, measured availability <=300s, and no backfill.
7. Run offline static tests and a **read-only** natural market preflight under the separate OS account **before** considering activation. Recheck whether the 34-symbol 5m/15m/1h fetch completes within the allowed window; one successful sample is not long-run latency proof. Reconfirm all existing production services and DB unchanged.
8. Production dirty checkout remains untouched; any future rollout must include release provenance and rollback plan. Use **separate operator approvals** for (A) copying/building release + installing unit **disabled** and (B) actually starting timer/capture. No production DB DDL needed under this PR (071–072 already applied by operator).
9. Stop/rollback of *future activated* runner: `systemctl disable --now trad-bot-me-r-v3-oos.timer` and inspect in-flight oneshot service separately. Preserve collected evidence and DB; never automatically drop/clean tables, alter OOS baselines, or restart existing services.

## Blocking gates prior to any deploy GO

- [ ] Code review/tests CI PASS for this PR; no unresolved threads
- [ ] Exact release snapshot/manifest matches merged commit and isolated tests; dependency set documented and independently reproducible
- [ ] Source import-time side effects assessed (config/telemetry/bybit) and distinct OS/DB permissions verified
- [ ] Mandatory env config prepared securely and invalid/missing configuration fail-closed tested
- [ ] Full symbol underlying classification and natural read-only API preflight PASS
- [ ] systemd-analyze syntax and behavior PASS for release paths
- [ ] Independent deploy-only operator GO; timer remains disabled
- [ ] Later independent activation GO after post-deploy checks

**Final state of this PR: DEPLOY_NO_GO / ACTIVATION_NO_GO / HOLDOUT_NO_GO.**
