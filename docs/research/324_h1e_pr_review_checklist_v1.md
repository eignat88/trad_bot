# H1-E Phase A — PR Review Checklist (#324 / PR #325)

**Status:** `PHASE_A_OFFLINE_IMPLEMENTATION / NO_DEPLOY / NOT_ACTIVATED / H1_OOS_HOLD`
**Scope:** new research-only files + one justified test-guard amendment. No
merge, no deploy, no VPS work is authorized by this PR.

## 1. Changed files

**New — code (`app/research/me_entry_quality_h1e_v1/`):**
`__init__.py`, `protocol.py`, `contracts.py`, `source_readiness.py`,
`capture.py`, `classification.py`, `ledger.py`, `coverage_health.py`,
`oos_readiness.py`

**New — DDL proposal (DO NOT APPLY):**
`sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`

**New — docs:** `docs/research/324_h1e_prospective_observer_phase_a_implementation_v1.md`,
`324_h1e_prospective_ledger_ddl_v1.md`, `324_h1e_oos_readiness_protocol_v1.md`,
`324_h1e_phase_a_verification_report_v1.md`

**New — tests:** `tests/h1e_v1_fixtures.py` + 10 test modules
(`test_me_entry_quality_h1e_{protocol_freeze,capture,classification,htf_failures,ledger,provenance_tamper,coverage_health,readiness,oos_readiness,isolation_purity}_v1.py`)

**Modified — existing file (JUSTIFIED, review attention requested):**
`tests/test_me_entry_quality_h1d_purity_v1.py` — the H1-D
"nothing outside the package references H1-D" guard conflicts with the #324
spec requirement to integrate the FROZEN H1-D adapter +
`H1D_HTF_SNAPSHOT_2` into H1-E.  Amendment adds ONE sanctioned consumer —
exactly `app/research/me_entry_quality_h1e_v1` — verified research-only and
wiring-free by `test_me_entry_quality_h1e_isolation_purity_v1.py`.  No
production file changed.

## 2. Tests (offline, zero network, zero production DB)

* 98 new H1-E tests: redelivery/idempotency, crash between
  read/classify/commit + restart recovery, 60/60 vs 59/60 (exact missing
  buckets), close boundary ±1 µs, all nine 4H×1D regimes, invalid
  OHLCV/gaps/empty payloads, limiter/429/10006/breaker/timeout/API errors,
  bounded retry + deferral budget, atomic checkpoint, checkpoint
  regression, conflict isolation, DDL-proposal contract, tamper detection
  (row/lineage/asof/row-count/schema), byte-identical offline replay,
  readiness UNKNOWN→HOLD, coverage-health five dimensions, OOS template,
  PYTHONHASHSEED determinism, mirrored-hash parity with frozen #305.
* H1-A/B/C/D + #305 regression suites: PASS (see the verification report
  for exact counts).

## 3. Identity (preserved verbatim)

`source_event_key`, `source_identity`, `signal_time` (µs), `lineage_hash`,
`source_fingerprint`, `activation_freeze_ts` (#305) carried VERBATIM; the
only recomputations are integrity re-derivations that must MATCH; ledger
identity = `(source_event_key, h1e protocol fingerprint)` in the separate
namespace `ME_ENTRY_QUALITY_H1E_V1`.

## 4. Scenario matrix (offline)

| Scenario | Result |
| --- | --- |
| redelivery / restart / out-of-order / mid-batch crash | exactly-once effect, no silent skips |
| 60/60 4H+1D | eligible; nine regimes retained verbatim |
| 59/60 / gap / invalid OHLCV / empty | non-eligible, exact evidence, never a market mode |
| close boundary ±1 µs | inclusive at close_us; 59/60 one µs earlier |
| future append | earlier ASOF classification unchanged |
| limiter/429/10006/breaker/timeout/API | bounded deferral → terminal fail-closed codes |
| identity conflict | isolated (`H1E_EVENT_KEY_LINEAGE_CONFLICT`), original preserved |
| snapshot tamper | `ProvenanceTamperError` fail-closed; replay byte-identical |

## 5. Unverified-gap accounting

* #305 UNVERIFIED window `2026-11-24T13:31:53.365952+00:00 →
  2026-11-25T07:14:41.230516+00:00` preserved; events inside → explicit
  `H1E_REJECT_UNVERIFIED_WINDOW`;
* 2026-11-25 SOXSUSDT = historical QA example only →
  `H1E_REJECT_SOXSUSDT_QA_EXAMPLE`, excluded from all prospective
  denominators;
* `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` (#296) stays honest: readiness check
  `SRC305_COVERAGE_PROOF` is `HOLD`/`UNKNOWN`, never PASS.

## 6. Immutable freeze rule

* #305 freeze untouched (`2026-11-24T13:31:53.365952+00:00`);
* H1-E freeze: `H1E_FREEZE_NOT_MINTED` in Phase A — minted only in Phase C
  with owner approval; synthetic fixture freezes are labelled and refused
  outside offline tests; `signal_time <= freeze_ts` always rejected.

## 7. Additive-only schema

DDL is a PROPOSAL (`sql/proposals/…`, `DO NOT APPLY`): CREATE-only, own
schema `research_me_entry_quality_h1e`, append-only triggers, monotonic
checkpoint trigger; touches no `dds.*` / `market.*` /
`research_me_upstream_coverage.*` object.

## 8. Outstanding owner GO decisions (blockers)

1. Phase B VPS read-only preflight — separate GO (not prepared here).
2. Phase C isolated deploy + H1-E freeze minting — separate GO (not
   prepared here).
3. OOS policy freeze (thresholds / execution model / unchanged control) —
   separate review + GO before any accumulation/evaluation.
4. Applying the DDL proposal — Phase C decision (`NO_DDL_APPLY` today).
5. #305 independent upstream coverage proof — remains BLOCKER-listed
   (#296).

**Not done, by design:** merge, deploy, VPS changes, live Bybit fetches,
backfill, OOS activation, service restarts, production DB writes.
