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

181 passed, 0 failed.
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
