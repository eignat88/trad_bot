# HTF_ISSUE176_PHASE2_REPAIR_AND_RUNTIME_IMPLEMENTATION_V1 — Final Report

Status: **`LOCAL_IMPLEMENTATION_READY_FOR_REVIEW`**
Target reached: **B1–B7 TECHNICALLY RESOLVED / LOCAL TESTS PASS / REPEAT AUDIT COMPLETE / READY_FOR_OPERATOR_REVIEW**

> `PHASE_2_GO` is NOT issued and must not be inferred from local test success.
> Protocol status remains `PROTOCOL_NOT_FROZEN`. Nothing was pushed, merged,
> deployed, migrated on production, or activated.

---

## 1. §17 Report block

```text
TASK: HTF_ISSUE176_PHASE2_REPAIR_AND_RUNTIME_IMPLEMENTATION_V1
BRANCH: feature/htf-issue176-phase2-runtime-repair-v1
BASE_SHA: 4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba
CANDIDATE_SHA: f4b588e (commits ae8feae, f4b588e; local only, NOT pushed)
WORKTREE: D:\py_pro\trad_bot\.cache\worktrees\issue176-phase2-repair
B1_PROTOCOL: RESOLVED (PROTOCOL_NOT_FROZEN; fingerprint 04f7c314bafb613f1a11ebb3d6868101be046468637d56284365d74477f5cf23; 6 OPERATOR_APPROVAL_REQUIRED fields)
B2_FREEZE: RESOLVED (independent boundary; strict signal_time> AND first_touch>; warmup/resume exclusions; 3-source agreement; fail-closed)
B3_CAUSAL: RESOLVED (strict 5m contract D1; explicit 1H coverage D2; window-independent level identity D3/D4; unified decision-ATR R3; frozen Point-B untouched)
B4_IDENTITY: RESOLVED (episode_id = sha256(experiment_id|symbol|direction|level_id|first_touch_open_ms); parity-tested vs audited machine)
B5_CHECKPOINT: RESOLVED (atomic checksummed envelope; legacy rejection; writer lease + stale recovery; durable cursor; AT-LEAST-ONCE + IDEMPOTENT, exactly-once NOT claimed)
B6_PERSISTENCE: RESOLVED (migration 074: PENDING / freeze NULL / observer FALSE; immutable rows; unique dedup; DB-level freeze + observer gate; 13 ITs on real PG)
B7_RUNTIME: RESOLVED (registration-gated runner; disabled-by-default unit; heartbeat; SIGTERM; kill-recovery via stale-lease takeover; fail-closed startup)
HTF_TESTS: 225/225 PASS (baseline reproduced exactly, 29 files)
NEW_TESTS: 114/114 PASS (8 suites: protocol, freeze, causal, identity+lifecycle, state-store, runtime, persistence, isolation)
COMBINED: 339/339 PASS
FOCUSED_P2: 24/24 PASS (p2_09_10_11_repair + p2_consolidated_regression)
HISTORICAL_DRY_RUN: HISTORICAL_DIAGNOSTICS_ONLY_NOT_OOS (restart parity OK; warmup contamination absent; identity stable; 0 production writes)
PRODUCTION_DB_WRITES: 0
PAPER_LIVE_CHANGES: 0
SERVICE_RESTARTS: 0
PRODUCTION_FREEZE_TS_CREATED: NO
PHASE_2_ACTIVATED: NO
FINAL_READINESS: READY_FOR_OPERATOR_REVIEW
NEXT_APPROVAL_REQUIRED: operator approval of protocol fields + repeat-audit verdict, then (separately) migration 074 apply + activation sequence in the runbook
```

## 2. Blocker closure matrix B1–B7

| # | Audit blocker | Resolution | Evidence |
|---|---|---|---|
| B1 | Protocol incomplete/unapproved | Deterministic machine-readable protocol + canonical SHA-256 fingerprint + human doc; every undecided value explicitly `OPERATOR_APPROVAL_REQUIRED` with proposed defaults; approval itself remains operator work by design | `app/research/htf_baseline_short_phase2_protocol_v1.py`, `docs/research/HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1.md`, `test_..._protocol_v1.py` (10) |
| B2 | No Phase-2 freeze mechanism | `Phase2FreezeBoundary`: strict `signal_time > freeze_ts` AND `first_touch_time > freeze_ts`; warmup-origin and pre-freeze resumes excluded; three sources (protocol/registry/DB) must agree exactly; missing/mismatch → reject everything | `..._freeze_v1.py`, `test_..._freeze_v1.py` (12), DB gate in migration 074 |
| B3 | Causal contract defects D1–D5+R3 | D1: strict 5m alignment/contiguity/dup/conflict/order/stale validation. D2: 1H partial/missing hours excluded AND reported. D3/D4: level identity anchored to formation/availability/price — no window-start anchoring. R3: ATR evaluated at the decision bar (single ATR definition, imported read-only). Frozen Point-B keeps its behavior (required); Phase-2 path does not use the defective helpers | `..._contracts_v1.py`, `..._level_universe_v1.py`, `test_..._causal_contracts_v1.py` (19) |
| B4 | Warmup-sensitive identity; module divergence | One counter-free identity rule for all modes; identity equals across restart/warmup-length/window-shift; single unified episode machine preserves P2-06..P2-11 exactly (differential parity test vs audited machine) | `..._episode_v1.py`, `test_..._identity_lifecycle_v1.py` (26, incl. parity) |
| B5 | Checkpoint/restart research-only | Atomic tempfile+fsync+`os.replace` with SHA-256 envelope; corrupt/truncated/legacy checkpoints rejected, never reinterpreted; single-writer lease with heartbeat and explicit stale takeover; durable cursor; replay idempotent. Guarantee stated honestly: **at-least-once + idempotent writes** (exactly-once effect via unique key + emitted set) | `..._state_store_v1.py`, `test_..._state_store_v1.py` (12), `test_..._runtime_v1.py` (11) |
| B6 | No persistence/dedup/registration | Migration 074: dedicated immutable observation table, `UNIQUE(experiment_id, source_event_id)` + `ON CONFLICT DO NOTHING`, DB-enforced strict freeze, `PENDING/freeze NULL/observer FALSE` registration, set-once freeze+fingerprint, terminal statuses, emergency stop. Never touches Point-B tables | `sql/migrations/074_...sql`, `..._persistence_v1.py`, `test_..._persistence_v1.py` (13, real PG) |
| B7 | No runtime wiring (12/14 capabilities NOT_IMPLEMENTED) | Dedicated runner + `trad-bot-htf-baseline-short-observer.service` disabled by default (`ConditionPathExists` flag gate); registration-gated startup fail-closed; heartbeat; SIGTERM clean stop; stale-lock crash recovery; dry-run mode; runbook | `..._observer_runner_v1.py`, `deploy/systemd/...service`, `test_..._runtime_v1.py`, runbook |

## 3. Repeat readiness audit (Gate A–K at `f4b588e`)

| Gate | Verdict | Evidence |
|---|---|---|
| A — Git/env | **PASS** | Worktree isolated at `D:\py_pro\trad_bot\.cache\worktrees\issue176-phase2-repair`, branch `feature/htf-issue176-phase2-runtime-repair-v1`, base `4ef60d1e`, 2 local commits, 0 uncommitted, **nothing pushed** |
| B — Protocol | **PASS (technical)** | Complete deterministic protocol + fingerprint + docs; approval of 6 fields intentionally pending operator; `PROTOCOL_NOT_FROZEN` |
| C — Freeze | **PASS** | Independent mechanism, strict `>`, pre-freeze-touch rejection tested; Point-B freeze never referenced in code as data |
| D — Causal data | **PASS** | D1/D2/D3/D4/R3 fixed on the Phase-2 path with tests; frozen Point-B byte-identical (isolation test) |
| E — Identity | **PASS** | Counter-free, window-independent, restart-stable; differential lifecycle parity with audited machine |
| F — Checkpoint/restart | **PASS** | Atomic + corrupt-reject + stale-lock recovery + downtime replay tested; guarantee documented as at-least-once + idempotent |
| G — Persistence | **PASS** | Migration 074 applied in disposable DB in 13 integration tests; dedup/concurrency/immutability/strict-freeze verified |
| H — Isolation | **PASS** | `git diff` vs base: 0 tracked modifications; only Phase-2-surface files added; Point-B stack byte-identical; registry untouched |
| I — Runtime | **PASS** | Disabled-by-default unit; registration gate fail-closed; heartbeat; SIGTERM; kill-recovery; second-writer refusal |
| J — Tests | **PASS** | 339/339 local; operational gaps from the prior audit all addressed (see §4 limitations) |
| K — Plan | **READY** | `docs/research/HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1.md` — operator-gated activation/rollback |

## 4. Known limitations (disclosed, not blockers)

1. **Exactly-once is NOT claimed.** Delivery is at-least-once with idempotent
   writes (DB unique key + emitted set in checkpoint). Exactly-once *effect* is
   demonstrated by the replay/concurrency tests.
2. **Process-kill recovery is simulated** (dead-PID lease metadata + crash
   between emission and checkpoint persistence), not an OS-level SIGKILL test.
3. **Live exchange source not integrated.** The runner consumes a pluggable
   candle source; only `FileCandleSource` (CSV) is implemented and tested. A
   Bybit-backed source must be added and tested before activation.
4. **pg8000 path not integration-tested** (psycopg2 is); the repository is
   DB-API-generic but only psycopg2 was exercised against real PostgreSQL.
5. **6 protocol fields await operator decisions** (geometry proximity/price
   band, maturity gates, terminal level-retirement policy).
6. R1 deviation is deliberate and tested: a same-step re-contact episode is
   never evicted as stale (driver-equivalent semantics).

## 5. Draft PR description (NOT created — push/PR requires separate approval)

> **Title:** `feat: HTF baseline SHORT Phase-2 protocol, freeze, persistence and observe-only runtime (#176)`
>
> **What changed:** adds the Phase-2 protocol candidate (B1), causal market-data
> contracts and unified level universe (B3), counter-free episode identity and
> lifecycle parity (B4), independent freeze boundary (B2), durable
> checkpoint/lease store (B5), dedicated persistence migration 074 (B6) and the
> disabled-by-default observe-only runtime + runbook (B7). New test suites (114
> tests) cover the mandatory matrix; the 225-test HTF baseline and 24 focused P2
> regressions are reproduced exactly.
>
> **Why:** closes blockers B1–B7 of the Phase-2 readiness audit (Issue #176).
>
> **Components:** Scanner NO / Paper NO / Live NO / PostgreSQL YES (new
> migration, unapplied) / Grafana NO / VPS deployment NO.
>
> **Tests:** `python -m pytest` over `test_htf_*` = **339 passed** (225 baseline
> + 114 new), focused P2 = 24 passed.
>
> **Database changes:** `sql/migrations/074_htf_baseline_short_phase2_registration.sql`
> — creates 2 new tables + triggers + functions; registers the experiment as
> `PENDING` with `freeze_ts = NULL` and `observer_enabled = FALSE`; does NOT
> modify any existing object; NOT applied to production.
>
> **Deployment requirements:** none. Merging must not activate Phase 2.
>
> **Risks:** see §4 limitations (at-least-once delivery; simulated kill tests;
> no live candle source yet; 6 protocol fields pending operator).
>
> **Activation:** only via `docs/research/HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1.md`,
> by explicit operator action.

## 6. Questions for Codex Review

1. Is the at-least-once + idempotent-writes guarantee acceptable for the Phase-2
   observation store, or is a transactional outbox required before freeze?
2. Is the strict interpretation of `first_touch_time > freeze_ts` (episode-level
   exclusion) the intended reading of the audit requirement?
3. Should the same-step re-contact/unavailable edge (R1 deviation) stay
   driver-equivalent, or be re-specified in the protocol as an explicit rule?
4. Is `UNIQUE (experiment_id, source_event_id)` with `episode_id:FIRST_CONFIRM`
   the right dedup key, or should `episode_id` itself carry a unique constraint?
5. Should migration 074's registration row be seeded with a placeholder
   fingerprint instead of NULL, so registry/DB consistency can be checked
   pre-activation?
6. Is a real SIGKILL chaos test required before freeze, or is the simulated
   crash matrix sufficient evidence?
7. Do the six `OPERATOR_APPROVAL_REQUIRED` defaults (proximity 300000 ms,
   20 bps band, 30/5/14 maturity gates, non-terminal eligibility loss) look
   reasonable as proposed?
