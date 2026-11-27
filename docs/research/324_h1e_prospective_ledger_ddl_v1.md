# #324 H1-E — Prospective Ledger DDL Proposal V1 (rationale)

**Artifact:** `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`
**Status:** `PROPOSAL ONLY — DO NOT APPLY / NO_DDL_APPLY / RESEARCH_ONLY`
**Applies to:** a FUTURE isolated research database only (Phase C, separate
owner GO).  Nothing in Phase A executes this SQL.

## 1. Why a separate schema

`research_me_entry_quality_h1e` is deliberately disjoint from:

* `research_me_upstream_coverage.*` (#305 source — READ-ONLY input);
* `dds.*` / `market.*` / paper / live production tables;
* every existing OOS namespace.

The H1-E ledger identity is `(source_event_key, h1e_protocol_fingerprint)`:
one event per protocol fingerprint, isolated from the #305 identity
namespace (`ME_UPSTREAM_SOURCE_V1`) and from all V3/OOS-2 keys.

## 2. Tables and immutability

| Table | Mutability | Purpose |
| --- | --- | --- |
| `h1e_activation` | freeze/protocol/APPROVAL immutable (trigger `H1E_IMMUTABLE_FREEZE_TS` / `H1E_IMMUTABLE_PROTOCOL_FINGERPRINT` / `H1E_IMMUTABLE_APPROVAL`) | the SEPARATE, owner-approved H1-E freeze — never the #305 freeze |
| `h1e_event` | **append-only** (trigger `H1E_LEDGER_IMMUTABLE`) | immutable first-capture record: preserved #305 identity + lineage, µs signal time, preserved_hash, explicit `h1e_activation_id` link + `h1e_freeze_ts_us` |
| `h1e_classification` | **append-only** | one row per classification attempt (DEFERRED retries append new rows; prior evidence never rewritten) |
| `h1e_status_event` | **append-only** | status transitions journal; current status is DERIVED |
| `h1e_reject_event` | **append-only** | durable rejection ledger (deterministic reason codes) |
| `h1e_conflict_event` | **append-only** + `uq_h1e_conflict_event_identity` | conflicting-payload isolation (`H1E_EVENT_KEY_LINEAGE_CONFLICT`) — ONE record per conflict key (exactly once) |
| `h1e_provenance_snapshot` | **append-only** | `H1D_HTF_SNAPSHOT_2` integrity anchors (canonical snapshot SHA-256, per-payload hashes, honest fixture label) |
| `h1e_pending_retry` | MUTABLE retry state (updated ONLY inside the batch transaction) | durable deferred-event queue (identity, attempt_count, deferred_reason, next_attempt_us) — independent of the delivery cursor, survives restart |
| `h1e_checkpoint` | single row, monotonic (trigger `H1E_CHECKPOINT_REGRESSION`) | delivery cursor — updated in the SAME transaction as the batch |
| `h1e_coverage_audit` | append-only by convention | persisted five-dimension reconciliation runs |

## 2a. Two independent freezes (review fix B3)

* the **#305 source freeze** stays an independent boundary:
  `CHECK (signal_time_us > source_freeze_ts_us)` on `h1e_event`;
* the **H1-E freeze** is a separate activation row; every event is linked
  to it (`h1e_activation_id` FK) and the DB-level trigger
  `h1e_event_prospective_guard` (BEFORE INSERT) fail-closes on:
  `H1E_NO_APPROVED_ACTIVATION` (no row / not `APPROVED`-`RUNNING` /
  freeze not fixed), `H1E_FINGERPRINT_MISMATCH` (protocol fingerprint
  differs from the activation), `H1E_FREEZE_TS_MISMATCH` (denormalized
  freeze instant disagrees with the activation) and `H1E_PRE_FREEZE_EVENT`
  (`signal_time_us <= h1e_freeze_ts_us` — equality is refused).  A
  PostgreSQL `CHECK` cannot reference another table, hence the trigger —
  a direct INSERT cannot bypass it; concurrent inserts pin the activation
  row with `FOR SHARE` and the activation itself is immutable once frozen
  (freeze/protocol fingerprint/approval metadata — a frozen activation can
  never be de-approved).

Status never overwrites evidence: transitions are journal rows
(`ACCEPTED → CLASSIFIED | DEFERRED | REJECTED | CONFLICT`, `DEFERRED →
CLASSIFIED/…`), and the current status is a pure function of the journal.

## 3. Atomicity & recovery (deterministic)

* Ledger inserts and the checkpoint advance in ONE transaction — the
  in-code contract is `InMemoryH1EStore.commit_batch()` (all-or-nothing:
  ledger writes, reject/conflict journals, pending-retry state and the
  checkpoint roll back TOGETHER — review fix B2); the SQL equivalent is a
  single transaction over the batch + `UPDATE h1e_checkpoint`.
* An interrupted transaction applies NOTHING (entries, rejects, conflicts,
  pending-retry state and checkpoint roll back together).
* Recovery after crash/restart: resume from the committed checkpoint and
  re-deliver (at-least-once) PLUS the durable `h1e_pending_retry` queue
  for deferred events (independent of the cursor).  Exactly-once effect
  comes from identity idempotency — an identical redelivery is a no-op; a
  conflicting redelivery is isolated in `h1e_conflict_event` exactly once
  (unique conflict key), never an overwrite.
* Backfill is impossible by construction: `CHECK (signal_time_us >
  source_freeze_ts_us)` on `h1e_event` mirrors the admission gate, and the
  H1-E freeze itself is immutable after fixation.

## 4. Retention

* `h1e_event` / `h1e_classification` / `h1e_provenance_snapshot` — kept for
  the experiment lifetime: they ARE the evidence for any future OOS
  comparison and its reproducibility (offline byte-identical replay).
* `h1e_reject_event` / `h1e_conflict_event` / `h1e_status_event` — kept
  forever (append-only audit; exclusions and conflicts must stay
  accountable).
* `h1e_coverage_audit` — kept forever (verdict history).

## 5. Dialect

PostgreSQL, mirroring `sql/migrations/094_me_upstream_source_capture_v1.sql`
(`TIMESTAMPTZ`, `BIGINT GENERATED ALWAYS AS IDENTITY`, `JSONB`, plpgsql
immutability triggers).  Additive-only statements: `CREATE SCHEMA/TABLE/
INDEX/FUNCTION/TRIGGER` — no `ALTER`/`DROP` of anything existing (verified
by `tests/test_me_entry_quality_h1e_ledger_v1.py`).

## 6. Explicit non-claims

* This proposal does NOT confirm any live schema (VPS-only details are
  `UNKNOWN` pending Phase B read-only confirmation queries).
* Applying this DDL is NOT authorized by the implementation PR; it is a
  Phase C decision under `NO_DDL_APPLY` today.
