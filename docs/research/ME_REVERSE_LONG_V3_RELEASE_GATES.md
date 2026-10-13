# ME Reverse LONG V3 — Release Gates

Experiment: MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS

## Scope

Research-only prospective shadow experiment.

Merging the implementation PR does not authorize production
deployment, OOS activation, or paper/live trading.

## Mandatory controls

1. V3 must remain INACTIVE by default.
2. The V3 evaluator requires explicit CLI opt-in.
3. V3 activation requires matching registry freeze timestamp
   and database started_at.
4. Evaluator activation is startup-bound: an explicitly
   approved activation requires a controlled evaluator restart.
5. Production migration 067 requires separate operator approval.
6. No paper/live routing is authorized.
7. Existing OOS experiments must remain unaffected.
8. GitHub CI must pass, or a documented CI exception must
   be explicitly accepted before merge.

## Independent approvals

- PR merge: separate decision.
- Production deployment: separate decision.
- Migration 067: separate decision.
- V3 prospective OOS activation: separate decision.
- Paper/live enablement: not authorized.

## Validation evidence

- V3/V2 regression: 180 passed on isolated VPS worktree.
- PostgreSQL observer INSERT and deduplication: PASS.
- PostgreSQL boundary: PASS.
- Lifecycle concurrency: PASS on isolated test schema.
- Observer to dedicated FINALIZED outcome: PASS in test DB
  using synthetic candles and simulated evaluation time.
- Production schema audit: READ-ONLY PASS.

These results do not prove a profitable trading edge.
