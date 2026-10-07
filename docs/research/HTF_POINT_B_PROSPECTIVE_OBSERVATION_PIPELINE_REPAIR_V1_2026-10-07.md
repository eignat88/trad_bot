# HTF Point-B Prospective Observation Pipeline Repair V1 — Local Recovery

Date: 2026-10-07
Scope: LOCAL DEVELOPMENT ONLY

PRODUCTION DEPLOYMENT = NOT YET PERFORMED

## Incident

Runtime capture produced Point-B completed events while the authoritative immutable completion snapshot transport omitted `cohort`. The runtime Point-B cohort guard therefore rejected legacy completed events, producing zero `research.prospective_observation` rows for some valid Point-B completions.

## Root cause

The detector Point-B candidate contains `features["cohort"] == "POINT_B"` (`app/research/htf_keylevel_point_b.py`). The completion builder in `app/research/prospective_observer.py` historically copied detector geometry into the immutable snapshot without explicitly preserving `cohort`. `_observe_htf_keylevel()` then replaced detector features with the authoritative snapshot, and the Point-B guard `features.get("cohort") != "POINT_B"` returned `None` for legacy rows missing that field.

## Local runtime repair

The local repair preserves `cohort` in new Point-B completion snapshots. Existing immutable rows are never updated. For legacy authoritative Point-B snapshots only, the observer performs narrow in-memory normalization when all authoritative context is consistent:

```text
completed event experiment_id == Point-B experiment
snapshot experiment_id matches
snapshot setup_event_id matches row
symbol is present
```

This normalization is not a global default and is not written back to `research.prospective_completed_event`.

## Recovery source

The recovery utility source of truth is exclusively:

```text
research.prospective_completed_event
```

It reads rows for:

```text
HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE
```

with frozen protocol boundary:

```text
2026-10-06T14:00:00Z
```

## Why this is not historical backfill

Each source row was already created by the prospective HTF detector and frozen through the immutable first-writer-wins lifecycle. Recovery reconstructs the missing prospective observation mapping from that frozen state. It does not rerun the detector over historical candles, does not call `detect_point_b_setup()` or `detect_point_b_setups()`, and does not use Bybit/API data, 5m/1h CSVs, or scanner market context.

## Eligibility

A row is eligible only when all applicable conditions hold:

```text
row.experiment_id == HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE
row.status == COMPLETED_IMMUTABLE
row.frozen_at > 2026-10-06T14:00:00Z
snapshot.experiment_id == HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE
snapshot.setup_event_id == row.setup_event_id
setup_event_id is present
symbol is present and matches row.symbol
direction in {LONG, SHORT} and matches row.direction
snapshot.signal_time > 2026-10-06T14:00:00Z
snapshot.signal_time is parseable and UTC-normalized
entry_reference_price is finite and positive
structural_stop_price is finite and positive
risk_abs > 0
snapshot.cohort is absent (legacy Point-B context) or == POINT_B
```

Every scanned row receives exactly one final verdict. Dry-run reconciliation is:

```text
scanned =
  eligible
  + prefreeze
  + experiment_mismatch
  + snapshot_experiment_mismatch
  + setup_id_mismatch
  + symbol_mismatch
  + direction_mismatch
  + malformed
  + wrong_cohort
  + invalid_signal_time
  + invalid_entry
  + invalid_stop
  + invalid_risk
  + identity_collision
```

The `eligible` count includes both `WOULD_INSERT` and `already_observed`; therefore `already_observed` must not be added separately in the reconciliation sum. The rejection categories exclude already-observed rows so they are not double-counted.

## Legacy snapshot handling

A missing `cohort` is accepted only for an authoritative Point-B completed event that passes the full experiment, setup identity, symbol, and directional-risk checks. An explicit wrong cohort fails closed as `wrong_cohort`. A BASELINE experiment or snapshot is never recovered as Point-B.

## Identity

Recovery uses the exact runtime identity function:

```python
ProspectiveOOSObserver._make_htf_source_key(experiment_id, setup_event_id)
```

The utility exposes the same identity through `recovery_source_signal_id()`. The digest is SHA-256-based and restart-stable; it does not use process-randomized Python `hash()`. Therefore the same immutable completed event maps to the same `source_signal_id` across runtime, recovery, and restarts.

## Observation mapping

The shared builder `ProspectiveOOSObserver._build_htf_point_b_observation_payload()` is used by both runtime capture and recovery.

```text
completed_event.experiment_id
  → prospective_observation.experiment_id

SHA-256 source key from (experiment_id, setup_event_id)
  → prospective_observation.source_signal_id

snapshot.symbol
  → prospective_observation.symbol

snapshot.direction
  → prospective_observation.direction

snapshot.signal_time
  → prospective_observation.signal_time

snapshot.entry_reference_price
  → prospective_observation.reference_price

snapshot.structural_stop_price
  → prospective_observation.invalidation_price

snapshot.target_1
  → prospective_observation.target_1

snapshot.target_2
  → prospective_observation.target_2

snapshot geometry
  → features JSON

features._frozen_max_hold = 240
features._frozen_intrabar_policy = STOP_FIRST
features._structural_r = risk_abs
features._authoritative_completion = true
  → evaluator protocol fields
```

The unique database constraint is:

```text
UNIQUE (experiment_id, source_signal_id)
```

so duplicate recovery attempts are naturally idempotent.

## Dry-run semantics

Default CLI mode is `--dry-run`.

The utility performs classification and existing-observation checks only. It executes zero inserts, rolls back the read transaction, and reports `would_insert`, `already_observed`, and all rejection categories.

## Apply semantics

`--apply` inserts only eligible rows whose identity is not already present. Each insert uses:

```text
ON CONFLICT (experiment_id, source_signal_id) DO NOTHING
RETURNING observation_id
```

The apply transaction commits after all eligible rows are processed. No completed-event row is updated or deleted.

## Idempotence

Expected local behavior:

```text
first dry-run:   would_insert = N
first apply:     inserted = N
second dry-run:  would_insert = 0
                 already_observed = N
second apply:    inserted = 0
```

## Immutable source protection

Recovery contains no UPDATE or DELETE statement against `research.prospective_completed_event`. The repository remains INSERT-only and first-writer-wins.

## Local fixture rehearsal

Recovery unit and rehearsal coverage lives in:

```text
tests/test_htf_point_b_observation_recovery_v1.py
```

The synthetic rehearsal uses isolated in-memory completed-event rows and verifies:

```text
first dry-run:   would_insert = 2
first apply:     inserted = 2
second dry-run:  would_insert = 0, already_observed = 2
second apply:    inserted = 0
completed_event rows unchanged
```

No local PostgreSQL write was needed because the rehearsal exercises the exact repository/observer SQL semantics through isolated fixtures. No production or VPS database was accessed.

## Local validation

Narrow HTF runtime, lifecycle, detector, recovery, evaluator-contract, identity, idempotence, freeze-boundary, and baseline regression tests pass locally. Production deployment, VPS access, production recovery, and service restart remain intentionally unperformed.
