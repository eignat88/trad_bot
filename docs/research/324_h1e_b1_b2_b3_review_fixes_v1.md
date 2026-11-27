# #324 H1-E — Review Fixes B1–B3 (Phase A, pre-re-review)

**Issue:** #324 · **PR:** #325 · **Branch:** `research/324-h1e-prospective-observer-v1`
**Status:** `PHASE_A_READY_FOR_RE_REVIEW` (no merge / no deploy / no
activation — see §7)
**Scope:** ONLY defects B1–B3 + their regression tests.  No feature
expansion, no Phase B/C work.

---

## 1. B1 — DEFERRED events were lost for re-processing (HIGH)

**Defect.** After a `DEFERRED` classification the capture cursor advanced
past the event; `reader.read_after(cursor)` would never return it again.
Re-processing only happened if the source redelivered the row — not the
shipped operating mode.  The old test drove retry via manual redelivery
with a stale cursor, masking the loss.

**Cause.** Deferral state was derived from the ledger journal only; no
state was tied to the cursor-independent lifecycle of "awaiting retry".

**Fix** (`capture.py`, `ledger.py`, DDL `h1e_pending_retry`):

1. durable pending-retry queue — per event: identity, `attempt_count`,
   `deferred_reason`, `next_attempt_us`, `preserved_hash`;
2. the queue is INDEPENDENT of the source cursor and survives restart
   (`InMemoryH1EStore.to_dict()/from_dict()` model durable persistence);
3. due entries are re-processed in deterministic order on every cycle;
4. attempts are bounded twice: the classifier's own deferral budget AND
   the worker-level `CaptureConfig.max_attempts` — exhaustion produces a
   TERMINAL non-eligible record (`H1E_RETRY_BUDGET_EXHAUSTED`) **without
   another classifier call** (no endless attempts, even with a stubborn
   classifier);
5. retry state, the append-only classification journal and the checkpoint
   update in ONE transaction;
6. terminal / conflicted identities are never re-executed (stale queue
   entries are dropped, not run); a tampered queue identity fails closed
   (`H1E_PENDING_IDENTITY_MISMATCH`); a lost queue entry self-heals on
   redelivery — the event is never lost;
7. retries run at the ORIGINAL `signal_time` ASOF — pinned by a test with
   extra future candles available at retry time (they are dropped, and the
   transport is queried at the original ASOF only).

**Test evidence** — `tests/test_me_entry_quality_h1e_durable_retry_v1.py`
(9 tests), incl. the acceptance scenario `DEFERRED → checkpoint advance →
restart → re-processed from the queue` (CLASSIFIED, `attempt_count == 2`,
append-only evidence, not counted as a new delivery), repeated HTF denial
(budget + preserved reason), budget exhaustion (terminal, `calls == 3`,
quiet afterwards), redelivery after finalization (idempotent), fixed-ASOF
retry, terminal/conflict protection, atomic retry state on crash, self-heal.

## 2. B2 — conflict persisted outside the batch transaction (HIGH)

**Defect.** `capture.py` called `store.record_conflict()` BEFORE
`commit_batch()`; the in-memory store mutated `_conflicts` immediately.
On a failed transaction the conflict stayed while the rest rolled back —
partial durable state.

**Cause.** The conflict journal had a durable mutation path outside the
single batch transaction.

**Fix** (`capture.py`, `ledger.py`, DDL):

1. `record_conflict()` is REMOVED — there is exactly ONE durable mutation
   path: `H1EStore.commit_batch(items, checkpoint, *, conflicts=, pending=)`;
2. detected conflicts are passed INTO `commit_batch` and applied with the
   writes, the pending state and the checkpoint atomically;
3. on failure NOTHING is applied — no partial records (verified by
   snapshot-equality of the whole durable state);
4. one conflict is recorded EXACTLY ONCE across redeliveries (conflict key
   `(source_event_key, existing_preserved_hash, delivered_preserved_hash)`;
   DDL: `uq_h1e_conflict_event_identity`);
5. the original immutable event record is untouched by a conflict (entry
   equality asserted byte-for-byte).

**Test evidence** — `tests/test_me_entry_quality_h1e_commit_atomicity_v1.py`
(5 tests): conflict + injected crash → zero partial records, checkpoint
unchanged → restart/redelivery → recorded exactly once; repeated
redeliveries → still one record; original preserved; full-batch rollback
(writes + rejects + conflicts + pending + checkpoint together); repeated
identical commits are idempotent (exactly-once effect).

## 3. B3 — DDL checked the #305 freeze instead of the H1-E freeze (MED/HIGH)

**Defect.** `h1e_event` enforced `signal_time_us > source_freeze_ts_us`
(the #305 freeze) while the comment claimed an H1-E boundary; nothing at
the database level required the event to be after the SEPARATE,
owner-approved H1-E freeze.

**Fix** (`sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`,
`ledger.py` reference gate):

1. explicit linkage: `h1e_activation_id` column with a FK to
   `h1e_activation(experiment_id)` + denormalized `h1e_freeze_ts_us`;
2. immutable freeze/protocol/**approval** metadata on `h1e_activation`
   (`H1E_IMMUTABLE_FREEZE_TS`, `H1E_IMMUTABLE_PROTOCOL_FINGERPRINT`,
   `H1E_IMMUTABLE_APPROVAL`; a frozen activation can never be de-approved);
3. DB-level check `signal_time_us > h1e_freeze_ts_us` — implemented as the
   `h1e_event_prospective_guard` BEFORE-INSERT trigger (a PostgreSQL
   `CHECK` cannot reference another table, so a trigger guards against
   direct-INSERT bypass): `H1E_NO_APPROVED_ACTIVATION` /
   `H1E_FINGERPRINT_MISMATCH` / `H1E_FREEZE_TS_MISMATCH` /
   `H1E_PRE_FREEZE_EVENT`;
4. the #305 freeze stays an INDEPENDENT `CHECK (signal_time_us >
   source_freeze_ts_us)` — both boundaries coexist;
5. fail-closed edges: no approved activation row / status not
   `APPROVED`-`RUNNING` / fingerprint mismatch / tampered freeze instant;
6. concurrency: the insert pins the activation row `FOR SHARE`; the
   activation is immutable once frozen;
7. reference semantics mirror the trigger (`InMemoryH1EStore` activation
   gate: `NoApprovedActivation` / `ActivationFingerprintMismatch` /
   `FreezeTsMismatch` / `PreFreezeWrite`) — tested with TWO different
   freezes.

**Test evidence** — `tests/test_me_entry_quality_h1e_activation_freeze_v1.py`
(10 tests): event after #305 but at/before H1-E freeze → rejected; store
bound to the later freeze refuses an earlier-freeze event (checked against
the H1-E freeze, not the source freeze); `signal_time == freeze_ts` →
rejected; strict-after → admitted; missing approval / whitespace approval
→ fail-closed; fingerprint mismatch → fail-closed; unbound store →
fail-closed; frozen freeze unchangeable (`FrozenInstanceError` + DDL
immutability of freeze/protocol/approval); concurrent/repeated commits
idempotent and integral.  DDL contract tests (5 new in
`test_me_entry_quality_h1e_ledger_v1.py`) pin the trigger, the FK linkage,
the two-freeze independence, approval immutability, the unique conflict
key and the `h1e_pending_retry` table.

## 4. Changed files

| File | Change |
| --- | --- |
| `app/research/me_entry_quality_h1e_v1/capture.py` | `PendingRetry`/`PendingRetryUpdate` contract; queue-driven retry loop; `max_attempts`/`retry_backoff_us`/`now_us`; conflicts via `commit_batch`; `LedgerWrite.activation_id/h1e_freeze_ts_us`; report `retried_keys`/`retry_terminal_keys`/`retry_skipped_keys` |
| `app/research/me_entry_quality_h1e_v1/ledger.py` | atomic `commit_batch(..., conflicts=, pending=)`; idempotent conflict journal; durable queue + `to_dict()/from_dict()`; activation gate (`NoApprovedActivation`/`ActivationFingerprintMismatch`/`FreezeTsMismatch`/`PreFreezeWrite`); `record_conflict()` REMOVED |
| `app/research/me_entry_quality_h1e_v1/contracts.py` | lossless `H1ESourceEvent.to_dict()` (+lineage) and fail-closed `from_dict()` (identity + preserved-hash re-derivation) |
| `app/research/me_entry_quality_h1e_v1/classification.py` | `terminal_noneligible_record()` + `NONELIGIBLE_RETRY_BUDGET_EXHAUSTED` |
| `app/research/me_entry_quality_h1e_v1/__init__.py` | exports |
| `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql` | activation FK + `h1e_freeze_ts_us`, `h1e_event_prospective_guard` trigger, approval immutability, `uq_h1e_conflict_event_identity`, `h1e_pending_retry` (still PROPOSAL ONLY — DO NOT APPLY) |
| `tests/test_me_entry_quality_h1e_durable_retry_v1.py` | NEW (9 tests, B1) |
| `tests/test_me_entry_quality_h1e_commit_atomicity_v1.py` | NEW (5 tests, B2) |
| `tests/test_me_entry_quality_h1e_activation_freeze_v1.py` | NEW (10 tests, B3) |
| `tests/test_me_entry_quality_h1e_ledger_v1.py` | activation-bound fixtures + 5 DDL contract tests |
| `tests/test_me_entry_quality_h1e_capture_v1.py` | DEFERRED test rewritten to the queue-driven flow (redelivery idempotent) |
| `tests/test_me_entry_quality_h1e_isolation_purity_v1.py` | determinism script binds the activation |
| docs | this report + implementation/DDL docs updated |

`tests/test_me_entry_quality_h1d_purity_v1.py` is UNCHANGED from the
previous commit (the sanctioned H1-E research consumer exception stays
exactly as reviewed; nothing added to it).

## 5. Test results

See `324_h1e_phase_a_verification_report_v1.md` §2 for the exact commands,
counts and HEAD commit.  Summary: 127 H1-E tests (98 prior + 29 new),
H1-A/B/C/D + #305 regression green, isolation/purity green, and the full
`tests/` run reproduces ONLY the known base failures + the two
Phase-2-only PR-diff confinement tests (CI-routed `NO_PHASE2` for this PR).

## 6. Acceptance criteria

| Criterion | Status |
| --- | --- |
| DEFERRED → checkpoint advance → restart → re-processed from durable queue | PASS |
| DEFERRED → repeated HTF denial → bounded, reason preserved | PASS |
| DEFERRED → budget exhaustion → terminal non-eligible, no endless attempts | PASS |
| redelivery after finalization → no re-classification, no duplicates | PASS |
| conflict + crash before commit → no partial records, checkpoint unchanged | PASS |
| conflict + restart/redelivery → recorded exactly once | PASS |
| two different freezes (#305 / H1-E) → checked against H1-E | PASS |
| `signal_time == H1-E freeze_ts` → rejected | PASS |
| `signal_time > H1-E freeze_ts` → admitted with valid activation | PASS |
| missing approval / fingerprint mismatch → fail-closed | PASS |
| frozen H1-E freeze cannot be changed | PASS |
| concurrent records / repeated commit → idempotent, integral | PASS |
| 60/60 & 59/60, µs ASOF, lineage, provenance, immutable identity, nine HTF combinations | PASS (unchanged, re-run) |

## 7. Phase B/C blockers (unchanged)

Phase B VPS read-only preflight, Phase C isolated deploy + H1-E freeze
minting, OOS policy freeze and DDL application all remain SEPARATE owner
GO decisions.  `NO_DEPLOY / NO_DDL_APPLY / NO_PRODUCTION_DB_WRITES /
NO_LIVE_BYBIT_FETCH / NO_BACKFILL / NO_OOS_ACTIVATION / H1_OOS_HOLD`
remain in force; merge of PR #325 authorizes nothing beyond review.
