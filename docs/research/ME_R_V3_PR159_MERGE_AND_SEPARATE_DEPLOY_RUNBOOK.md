# PR #159 — merge readiness and separate controlled deployment

State: **MERGE REVIEW PREPARED / UNIVERSE APPROVAL PENDING / CAPTURE NOT AUTHORIZED**.

## Verified evidence (2026-10-16)

- Isolated VPS checkout: 8af03afa validated, 213 passed, 3 skipped.
- Dedicated PostgreSQL SQL 068..072: applied to `trad_bot_me_r_v3_oos_pg_test`.
- Live pg8000 smoke: 2 passed. One-shot atomic E2E: 1 passed.
- Natural Bybit read-only market preflight for BTCUSDT and ETHUSDT at 05:02 UTC:
  closed 5m/15m/1h context PASS; 5m source delays 153.538s and 154.266s.
- systemd-analyze verify: no errors; timer calendar validated; timer not installed,
  service inactive.
- These are **technical** results, not permission to start OOS.

## Immutable protocol boundaries (Issue #151)

- FROZEN_AT: `2026-10-16T12:00:00Z`
- ACTIVATION_AT: `2026-10-19T12:00:00Z`
- APPROVED runtime fingerprint:
  `ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a`

Activation is a **strict prospective inclusion cutoff**. It does not schedule,
authorize, or imply a start of service. No backfill if deployment happens later.

## Universe selection — NOT YET FROZEN

Run `/opt/trad_bot/.venv/bin/python
tools/research/me_r_v3_universe_candidate_issue154.py` from the PR worktree,
as read-only. Proposal: top 50 active Bybit linear USDT perpetuals,
24h turnover >=10M USDT, sorted by descending turnover and then symbol.
If fewer than 50 satisfy the rule, fail closed. The generated output is a
candidate snapshot **only**, not an approved runtime universe.

Review complete ordered symbol list, observation timestamp, liquidity
and eligibility, then obtain explicit operator approval and commit the
exact fixed ordered symbols plus SHA256 and source snapshot. The deployed
`ME_R_V3_OOS_SYMBOLS` must match the immutable approved list byte-for-byte
(no runtime dynamic selection). Re-run market preflight on the **entire**
approved list; assess 50-symbol API latency and 300s source availability
at the chosen timer offset. Fail closed for missed/late samples.

## Merge gate (separate from deployment)

1. Review changed files and base-branch divergence and GitHub checks.
2. Re-run tests after this PR's latest commits; check sample API handling,
   fail-closed source gaps and immutable identity behavior.
3. Approve and commit fixed universe manifest, then re-run universe tests.
4. No unresolved code review comments or CI failures; human review.
5. Mark PR ready for review only after items above. Merge only after
   reviewing integration against main. Merge does not authorize deployment.

## Deployment gate — separate, explicit operator authorization required

**Preparatory read-only steps only**:
- Record VPS service health, HEAD, role permissions and database identity.
- Verify `trad_bot_me_r_v3_oos` is an approved clean, separate DB;
  ensure SQL 068..070 are present and SQL 071..072 are unapplied.
- Ensure approved fingerprint and dates match the fixed spec and approved list.
- Assess actual systemd restrictions, env-file ownership, clock/NTP integrity,
  API throughput, observability and incident rollback.
- Review how to reconcile VPS main while preserving its local divergent edits;
  **never** `git reset --hard`.
- Capture a separate signed GO decision before any migration or installation.

**Only after separate GO**:
- Merge approved PR, safely deploy to main; apply SQL 071..072 only
  in `trad_bot_me_r_v3_oos` and verify role.
- Stage separately approved `/etc/trad-bot/me-r-v3-oos.env` with read-restricted
  permissions and approved values; verify it does not point to production.
- Stage systemd files, `daemon-reload`, verify units remain disabled.
- A second independent GO is required before enabling timer or performing
  capture. Explicit cutoff is not auto-start permission.
- On anomaly: stop and disable new timer only; preserve V3 evidence,
  existing scanner/paper/evaluator services untouched.

Current decision: **REVIEW PREP / DEPLOY NO_GO / ACTIVATE NO_GO**.
