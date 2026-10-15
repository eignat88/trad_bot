# ME_R_V3_FREEZE_ACTIVATION_FINGERPRINT_V1

Issue: #151
Experiment: ME_R_LONG_EARLY_MAE_EXIT_OOS_V3
Protocol version: 3.0.0
Scanner version: 1.0.0
Base commit: e7ff34d32e913b101100db487b26ec516c1ce09c
Guard hardening commit: 29f34954f1ee6ee1b8893debd064fbc694e6a190

## Immutable specification

SHA-256:
b626700a04b679d67fa4db029e4968dd9feba62123d5145f428e22a87c2e5ed3

Canonical JSON:
sort_keys=True, separators=(",", ":"), ensure_ascii=True

Verification: PASS.

## Guard hardening

- Runtime fingerprint comparison added before capture.
- Non-UTC boundary offsets rejected.
- Source candle close_at <= activation_at rejected.
- Detection time <= activation_at rejected.
- Historical rejection implemented in capture and repository.
- Frozen trading parameters unchanged.

## Regression evidence

185 passed, 0 failed.
Python: /opt/trad_bot/.venv/bin/python
Production database integration: NOT VERIFIED.
Natural runtime fetch provenance: NOT VERIFIED.
Concurrent PostgreSQL insert behavior: NOT VERIFIED.

## Restart and deduplication

- Deterministic source identity: code reviewed.
- Duplicate source identity: SQL constraints present.
- Existing source comparison: code reviewed.
- Restart entry geometry: unit-tested.
- Concurrent retry/restart DB integrity: NOT VERIFIED.

## Runtime activation boundaries

FROZEN_AT: PENDING_OPERATOR_APPROVAL
ACTIVATION_AT: PENDING_OPERATOR_APPROVAL
APPROVED_RUNTIME_FINGERPRINT: PENDING

Required: FROZEN_AT < ACTIVATION_AT.
Strict source cutoff: source_candle_close_at > ACTIVATION_AT.
Strict detection cutoff: detected_at > ACTIVATION_AT.
Historical backfill: FORBIDDEN.

## NO_GO conditions

- Missing or invalid UTC timestamps.
- Frozen/activation ordering violation.
- Missing or mismatched runtime fingerprint.
- Pre-activation source candle or detection.
- Missing source fetch provenance.
- Incomplete source candle coverage.
- Unresolved duplicate identity conflict.

## Rollback

- Keep V3 capture disabled until separately authorized.
- Do not activate the V3 runner or systemd service.
- Do not modify production DB, registry, paper or live.
- On later activation failure, disable future V3 capture.
- Preserve existing research evidence without rewriting finalized rows.

## Verdict

CODE_PREFLIGHT_PASS
FREEZE_APPROVAL_PENDING
PRODUCTION_ACTIVATION_NO_GO

This document does not authorize deployment or OOS activation.

## PostgreSQL race hardening checkpoint

Environment: PostgreSQL 17.11, isolated database me_r151_race_test.
Isolation: READ COMMITTED.
Production trad_bot database: NOT MODIFIED.

Code changes:
- Source INSERT uses ON CONFLICT DO NOTHING RETURNING.
- Conflict fallback re-reads source_setup_id with FOR UPDATE.
- Missing fallback row rejects conflicting candle identity.
- Registration advisory lock corrected to bigint.
- Four additional SQL-contract regression tests.

Evidence:
- 185 V3 regression tests passed.
- Concurrent COMMIT: PASS; one persistent source.
- Second UNIQUE constraint: PASS; conflicting source rejected by SQL fallback conditions.
- Concurrent ROLLBACK: PASS; surviving transaction committed exactly one source.
- bigint advisory lock: PASS.

Limitations:
- SQL concurrency tests used a simplified race_source table.
- Full Python repository plus seven variants integration: NOT VERIFIED.
- Natural runtime source provenance: NOT VERIFIED.
- Operator UTC boundaries and runtime fingerprint: PENDING.

Verdict: SQL_RACE_PASS / FULL_REPOSITORY_PENDING / ACTIVATION_NO_GO.

## Full PostgreSQL repository integration checkpoint

Database: me_r151_race_test (isolated PostgreSQL 17.11)
Driver: pg8000
Migration: 068 applied successfully
Transaction isolation: READ COMMITTED

Validated:
- register_protocol(): PASS
- Protocol fingerprint match: PASS
- Registered variants: exactly 7
- Source insert and seven outcomes: PASS
- Identical source replay: same source_event_id
- Second UNIQUE candle collision: correctly rejected
- Database CHECK violation: rollback without partial rows
- Two concurrent Python repository calls: PASS
- Both workers returned source_event_id = 4
- Database verified one source and exactly seven variants
- Previous SQL concurrent COMMIT and ROLLBACK tests: PASS

Limitations:
- Python worker synchronization occurred before repository calls,
  not precisely at the competing SQL INSERT.
- Natural scanner-to-capture provenance is not yet verified.
- Operator freeze and activation boundaries are not approved.

Verdict:
REPOSITORY_INTEGRATION_PASS
CONCURRENT_REPOSITORY_SMOKE_PASS
PRODUCTION_ACTIVATION_NO_GO

## Candidate freeze boundaries — independently verified

Verification date: 2026-10-15
Verification source: VPS Python protocol_payload() and SHA-256

FROZEN_AT: 2026-10-16T12:00:00Z
ACTIVATION_AT: 2026-10-19T12:00:00Z

Interval: 72 hours
Ordering: PASS

Candidate runtime protocol SHA-256:
ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a

Independent runtime calculation: MATCH
Canonical protocol version: 3.0.0

Status:
- FINGERPRINT_VERIFIED
- BOUNDARIES_PROPOSED
- OPERATOR_APPROVAL_PENDING
- PRODUCTION_ACTIVATION_NO_GO

These are candidate boundaries only.
No production environment variables have been configured.
Historical observations must not be backfilled or counted.

## Final consolidated preflight status — 2026-10-15

This section supersedes earlier historical checkpoint statuses.
Historical checkpoints are retained for auditability.

### Verification

- Immutable protocol 3.0.0 SHA-256: PASS
- Runtime fingerprint calculation: PASS
- Runtime fingerprint independent VPS comparison: MATCH
- UTC freeze/activation ordering: PASS
- Strict prospective guards: PASS in regression tests
- Regression suite: 185 PASS
- PostgreSQL migration 068: PASS in isolated database
- register_protocol(): PASS
- Seven variant definitions: PASS
- Source + seven variant outcomes: PASS
- Identical replay: PASS
- Candle identity collision: PASS
- Transaction rollback: PASS
- Concurrent PostgreSQL COMMIT/ROLLBACK: PASS in SQL tests
- Concurrent Python repository smoke: PASS

### Remaining limitations

- Python concurrency test did not force collision at the INSERT statement.
- Integration tests executed manually; permanent in-repo integration
  regression coverage is not yet committed.
- Natural scanner-to-capture timestamp provenance is not verified.
- Production clock synchronization has not been verified in this preflight.
- Operator approval of proposed UTC boundaries is pending.
- No production deployment or activation is authorized.

### Candidate operator freeze record

Protocol version: 3.0.0

FROZEN_AT: 2026-10-16T12:00:00Z
ACTIVATION_AT: 2026-10-19T12:00:00Z

APPROVED_RUNTIME_FINGERPRINT_CANDIDATE:
ed1cd8e48d3617fec66b5eac220ec8f7dab3b963e53a4ea51874f12ceddf131a

Immutable specification SHA-256:
b626700a04b679d67fa4db029e4968dd9feba62123d5145f428e22a87c2e5ed3

Boundary and fingerprint verification: PASS
Operator approval: PENDING
Freeze effective: NOT YET EFFECTIVE
Production activation: NO_GO

This record does not authorize deployment, production environment
configuration, historical backfill, or V3 capture.
