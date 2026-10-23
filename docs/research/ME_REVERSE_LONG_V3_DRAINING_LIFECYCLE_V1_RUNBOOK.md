# Issue #189 — ME Reverse Long V3 controlled drain (operator runbook)

Scope: ONLY `MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS`. No paper/live or other OOS status changes.

## Deploy preflight
- CI must PASS; PR must be merged; `git status --short` must be clean.
- Verify scanner/paper/evaluator active, V3 status RUNNING and `ME_SHORT_GEOM_A_EXECUTION_V1` RUNNING.
- Record Git SHA, UTC time, observation count, maximum created_at/signal_time, and dedicated outcome status counts; save off-VPS backup/snapshot.
- Execute migration 076 with `psql -v ON_ERROR_STOP=1` (installs guard; does NOT change lifecycle status).
- Restart scanner and research evaluator only after code + migration are both deployed; confirm V3 evaluator explicit startup opt-in remains configured and logs show evaluation cycles.

## Operator GO: atomic STOP_CAPTURE / DRAINING
```sql
BEGIN;
SET LOCAL lock_timeout = '10s';
UPDATE research.prospective_experiment
SET status = 'DRAINING'
WHERE experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'
  AND status = 'RUNNING'
RETURNING experiment_id, status, started_at;
-- Must return EXACTLY ONE row; otherwise ROLLBACK.
COMMIT;
```

V3 observation writer locks experiment row `FOR SHARE` after checking RUNNING; the update cannot commit until those writers finish. Once committed, subsequent V3 capture is rejected by DB guard. Explicitly verify `status=DRAINING`, freeze timestamp unchanged, and V3 observations cease to grow. Do NOT change JSON registry to DRAINING: original frozen registry activation identity stays RUNNING, DB status permits dedicated evaluator drain.

## Drain
- V3 must remain listed in active prospective evaluator routing at DRAINING; check V3 dedicated evaluator logs and `checked/finalized/errors`.
- Check outcome coverage via `research.me_reverse_long_v3_execution_outcome`, not `research.prospective_outcome`.
- Gate: every V3 observation has exactly one dedicated outcome with `status='FINALIZED'`; zero PENDING, COVERAGE_INVALID, TEMPORAL_UNVERIFIED, INVALID_GEOMETRY, missing.
- If candle coverage remains bad or errors appear, leave DRAINING and investigate. Never auto-mark failed outcomes FINALIZED.
- Verify no new observation counts after activation, and other OOS continue RUNNING.

## Operator GO: terminal CANCELLED
```sql
BEGIN;
SET LOCAL lock_timeout = '10s';
UPDATE research.prospective_experiment
SET status = 'CANCELLED'
WHERE experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'
  AND status = 'DRAINING'
RETURNING experiment_id, status;
-- Must return EXACTLY ONE row; otherwise ROLLBACK.
COMMIT;
```
DB trigger rejects this transition if any V3 dedicated outcome is not FINALIZED or missing. After commit, evaluator no longer routes V3. Restart scanner/evaluator, verify terminal status remains, no new V3 observations, and `ME_SHORT_GEOM_A_EXECUTION_V1` still RUNNING. Preserve all historical observations/outcomes.

Never modify unrelated experimental records or reopen V3 on the same experiment_id; create a new frozen/versioned protocol if future research is needed.
