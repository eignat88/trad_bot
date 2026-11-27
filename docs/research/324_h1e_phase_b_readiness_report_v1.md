# H1-E — Phase B: Readiness Report (Issue #324)

- **Status:** `PHASE_B_READINESS_REPORT` (research only; no deployment, no DDL apply, no activation)
- **Scope:** read-only preflight for the isolated prospective H1-E observer on `trad-bot-01`
- **Method:** reproducible, READ-ONLY checks only; every verdict carries its evidence class
- **Date basis:** VPS state snapshot of 27.11.2026 (as supplied in the task; NOT re-verifiable from this host)

## Verdict key

| Verdict | Meaning |
|---|---|
| `PASS` | verified by evidence available to this review |
| `HOLD` | conditionally satisfied; requires an owner GO or a runtime check on the VPS |
| `BLOCK` | cannot be satisfied as specified |
| `UNKNOWN` | not verifiable from this host / without live requests |

## B.1 Upstream source coverage (#305) — `HOLD`

| Check | Verdict | Evidence |
|---|---|---|
| #305 source `RUNNING`, freeze `2026-11-24 13:31:53.365952+00:00` | PASS | VPS snapshot 27.11.2026 |
| 120 source events, 0 identity/metadata mismatches, 0 pre-freeze events | PASS | VPS snapshot 27.11.2026 |
| capture health 120 CAPTURED / 1 DUPLICATE / 0 errors; last coverage audit 25.11.2026 matched 1/1 | PASS | VPS snapshot 27.11.2026 |
| completeness of the unverified window **24–25 Nov 2026** | **`UPSTREAM_SOURCE_COVERAGE_UNPROVEN`** | see below |

**24–25 Nov window.** The observer is prospective-only: it accumulates nothing from history and
its eligibility rests on the source stream being complete for the period it serves. From this
host the window cannot be proven complete (the upstream capture is the only witness and its own
audit chain covers 25.11 only). Per the phase-B rule, when proven completeness is impossible the
verdict is **`UPSTREAM_SOURCE_COVERAGE_UNPROVEN`** and the window must NOT be used to justify any
eligibility claim. Consequences are fail-closed by construction: rows are admitted strictly after
the H1-E freeze, and the 24–25 Nov window is never backfilled (`NO_BACKFILL`).

Reproducible read-only audit SQL (run by an owner on the VPS; NO writes):

```sql
-- source inventory + freeze boundary
SELECT COUNT(*), MIN(captured_at), MAX(captured_at),
       COUNT(*) FILTER (WHERE signal_time <= activation_freeze_ts) AS pre_freeze,
       COUNT(DISTINCT source_identity) AS identities
  FROM research_me_upstream_coverage.source_event;

-- identity / metadata mismatch audit (read-only)
SELECT COUNT(*) AS hash_mismatches
  FROM research_me_upstream_coverage.source_event
 WHERE lineage_hash <> encode(digest(lineage::text, 'sha256'), 'hex');

-- 24-25 Nov window inventory (the unproven window)
SELECT date_trunc('day', captured_at) AS day, COUNT(*)
  FROM research_me_upstream_coverage.source_event
 WHERE captured_at >= TIMESTAMPTZ '2026-11-24 00:00:00+00'
   AND captured_at <  TIMESTAMPTZ '2026-11-26 00:00:00+00'
 GROUP BY 1 ORDER BY 1;
```

## B.2 HTF / Bybit readiness — `UNKNOWN` (fail-closed)

| Check | Verdict | Evidence |
|---|---|---|
| frozen ASOF/60-60 semantics implemented (no later candles) | PASS | offline tests `test_me_entry_quality_h1e_htf_adapter_v1.py` (10/10) |
| bounded retry + request budget, fail-closed errors | PASS | same tests (limiter/timeout/rate-limit/budget/cap matrix) |
| shared cross-process limiter / retry budget / circuit breaker wiring | `HOLD` | code path verified offline; production wiring requires the VPS client config |
| **live Bybit availability of 4H/1D kline windows** | **`UNKNOWN`** | no live fetch is permitted in this phase (`NO_LIVE_BYBIT_FETCH`); live availability must be probed by an owner at activation time |

A live probe checklist (owner, on the VPS, at activation time, read-only public GETs only) is
prescribed in the deploy runbook; until it is executed, HTF readiness stays `UNKNOWN` and the
worker stays disabled.

## B.3 VPS / PostgreSQL readiness — `HOLD`

| Check | Verdict | Evidence |
|---|---|---|
| PostgreSQL primary reachable; #305 schema present | PASS | VPS snapshot 27.11.2026 |
| H1-E PG schema ABSENT (as required before any apply) | PASS | VPS snapshot 27.11.2026 |
| reviewed DDL + rollback/recovery package exists (NOT applied) | PASS | `sql/proposals/324_h1e_prospective_ledger_v1_proposal.sql`, `..._rollback.sql` |
| least-privilege observer role script exists (NOT applied) | PASS | `deploy/sql/324_h1e_observer_role.sql`, `324_h1e_observer_recovery.sql` |
| isolated test-PostgreSQL validation of the DDL + store | PASS | `tests/test_me_entry_quality_h1e_pg_integration_v1.py` (9/9, scratch DB, applied there ONLY) |
| DDL apply on the VPS | `HOLD` | requires explicit owner GO (`NO_DDL_APPLY` otherwise) |
| H1-E freeze / activation minting | `HOLD` | requires explicit owner GO; done at activation, never pre-declared |

## Cross-cutting constraints honoured

- `RESEARCH_ONLY / NO_DEPLOY / NO_DDL_APPLY / NO_PRODUCTION_DB_WRITES / NO_LIVE_BYBIT_FETCH /
  NO_EXISTING_SERVICE_RESTART / NO_TRADING_LOGIC_CHANGE / NO_BACKFILL / NO_OOS_ACTIVATION /
  H1_OOS_HOLD` — all in force; nothing in Phase B/C writes to any working database or starts any
  existing process.
- Closed issues **#296 / #305** do NOT imply runtime readiness: their deployment gates are
  re-checked independently (see the compatibility matrix).
- Untracked VPS files (incl. `.env.pre_*`) are never deleted or overwritten.

## Summary

Phase B yields no `BLOCK`. The two `UNKNOWN`/unproven items (`UPSTREAM_SOURCE_COVERAGE_UNPROVEN`
for the 24–25 Nov window; live HTF availability `UNKNOWN`) are fail-closed: neither enables the
observer, and neither is consumed as evidence of eligibility. The observer remains
`DISABLED / NOT_ACTIVATED` until the owner GOs in the deploy runbook are issued one by one.
