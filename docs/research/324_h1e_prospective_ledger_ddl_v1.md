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
| `h1e_activation` | freeze immutable (trigger `H1E_IMMUTABLE_FREEZE_TS`) | the SEPARATE, owner-approved H1-E freeze — never the #305 freeze |
| `h1e_event` | **append-only** (trigger `H1E_LEDGER_IMMUTABLE`) | immutable first-capture record: preserved #305 identity + lineage, µs signal time, preserved_hash |
| `h1e_classification` | **append-only** | one row per classification attempt (DEFERRED retries append new rows; prior evidence never rewritten) |
| `h1e_status_event` | **append-only** | status transitions journal; current status is DERIVED |
| `h1e_reject_event` | **append-only** | durable rejection ledger (deterministic reason codes) |
| `h1e_conflict_event` | **append-only** | conflicting-payload isolation (`H1E_EVENT_KEY_LINEAGE_CONFLICT`) |
| `h1e_provenance_snapshot` | **append-only** | `H1D_HTF_SNAPSHOT_2` integrity anchors (canonical snapshot SHA-256, per-payload hashes, honest fixture label) |
| `h1e_checkpoint` | single row, monotonic (trigger `H1E_CHECKPOINT_REGRESSION`) | delivery cursor — updated in the SAME transaction as the batch |
| `h1e_coverage_audit` | append-only by convention | persisted five-dimension reconciliation runs |

Status never overwrites evidence: transitions are journal rows
(`ACCEPTED → CLASSIFIED | DEFERRED | REJECTED | CONFLICT`, `DEFERRED →
CLASSIFIED/…`), and the current status is a pure function of the journal.

## 3. Atomicity & recovery (deterministic)

* Ledger inserts and the checkpoint advance in ONE transaction — the
  in-code contract is `InMemoryH1EStore.commit_batch()` (all-or-nothing);
  the SQL equivalent is a single transaction over the batch + `UPDATE
  h1e_checkpoint`.
* An interrupted transaction applies NOTHING (entries, rejects, conflicts
  and checkpoint roll back together).
* Recovery after crash/restart: resume from the committed checkpoint and
  re-deliver (at-least-once).  Exactly-once effect comes from identity
  idempotency — an identical redelivery is a no-op; a conflicting
  redelivery is isolated in `h1e_conflict_event`, never an overwrite.
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
