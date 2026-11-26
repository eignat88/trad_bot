# Draft PR — Issue #312: H2_RSI_ONLY Prospective Runtime V1

**Issue:** #312 · **Parent:** #298 · **Event source:** #305 (read-only) ·
**Base engine:** PR #311 (reused unchanged)
**Status:** `IMPLEMENTATION_READY / ACTIVATION_HOLD`
**Deploy / merge / activation:** all HOLD — separate GO required.

## What this adds (research-only, isolated, disabled by default)

An independent prospective-only runtime that feeds the #305 upstream source
cohort through the merged H2 RSI-confirmation engine on fully CLOSED 5m
candles and persists per-variant delayed-entry outcomes.

| Area | Deliverable |
|---|---|
| FR-01 source reader | `app/research/me_entry_quality_h2_prospective_v1/source_reader.py` — SELECT-only #305 reads, µs-precision freeze checks, lineage-hash verification, idempotent redelivery, explicit `EVENT_KEY_LINEAGE_CONFLICT` |
| FR-02 candle provider | `candle_adapter.py` — `BybitClient.get_klines_range()` under a dedicated confirmed research API budget (per-cycle/hour caps, bounded retry/backoff, page caps); UTC-5m grid anchors, canonical 120-bucket warmup, ASOF-strict closed-only rows, payload-hash provenance; budget unconfirmed -> provider OFF + `BLOCKED_CANDLE_SOURCE` |
| FR-03 confirmation | reuses `me_entry_quality_h2_v1` (PR #311) verbatim — TURN_1 / TURN_2 / PEAK_REVERSAL, no RSI/identity/rule duplication |
| FR-04 lifecycle | `lifecycle.py` — `PENDING_CONFIRMATION / CONFIRMED_DELAYED / EXPIRED / INCOMPLETE_COVERAGE / INVALID_SOURCE / NOT_READY`; one-way, restart-safe; missing data NEVER becomes `EXPIRED`/`CONFIRMED_DELAYED`; transient coverage defects finalize only after a bounded attempt budget |
| FR-05 result store | `store.py` + `sql/migrations/095_me_entry_quality_h2_prospective_v1.sql` — dedicated research schema; UNIQUE(event, variant, policy); immutable terminal outcomes with explicit conflict ledger; DB triggers enforce immutability |
| FR-06 outcome policy | `docs/research/312_h2_entry_outcome_policy_v1.md` — execution-model spec with explicit `BLOCKED_PENDING_DECISION` (D-1..D-12); no Net E[R]/return metric is implemented or stored (`entry_candidate_price IS NULL` enforced by CHECK) |
| FR-07 runtime | `runner.py` (`python -m app.research.me_entry_quality_h2_prospective_v1.runner`) — never `scanner_runner.py`; disabled by default (no DB connection / no Bybit / no write when off); bounded polling; restart-safe checkpointing; systemd/env templates shipped NOT installed |
| FR-08 telemetry | `telemetry.py` + `runtime_health` / `evaluation_log` — source/candle coverage, per-variant counts, confirmation delays, API requests/retries/rate-limit/budget skips, idempotent duplicates, lineage conflicts; verbatim `SOURCE_EMPTY_WAITING` (expected state, not an algorithm error) |

## Safety (unchanged constraints honored)

* no deploy, no merge, no activation (this is a **Draft** PR);
* #305 tables untouched (SELECT-only by construction and by test);
* no `scanner_runner.py` / production detector / scoring / gate / paper / live
  change — `git status` shows zero modified tracked files;
* no historical backfill (strict `signal_time > freeze_ts` boundary at µs
  precision); no fabricated candles, prices, fills or yield metrics;
* H2 activation stays HOLD.

## Tests

* new suites: `tests/test_me_entry_quality_h2_prospective_{source_reader,candle_adapter,lifecycle,store,runtime,pg_integration}_v1.py`
* acceptance criteria 1–18 covered (see `reports/issue312_implementation_report.md`
  for the criterion-to-test matrix)
* regression: **168/168** H2 tests pass — the original 167 (PR #311) plus a
  new strengthened isolation guard; full-suite branch-vs-base attribution in
  the report (remaining deltas = 2 PR-scoped Phase-2 guards that CI deselects
  + 1 timing-flaky test that fails at the untouched base in isolation — none
  introduced by this change)
* real-PostgreSQL suite is opt-in (`ME_H2PRO_PG_INTEGRATION=1`) per repo
  convention and covers migration 095 in an isolated scratch database

## Contract amendment (explicit)

`tests/test_me_entry_quality_h2_purity_v1.py::TestProductionIsolation` banned
the substring `me_entry_quality_h2` in migrations/deploy units — an
invariant from the time H2 was pure in-memory code.  Issue #312 explicitly
authorizes a research-only migration + disabled templates, so the three
guards are sharpened to the exact pure-engine name `me_entry_quality_h2_v1`
(original invariant preserved) and a NEW guard confines the prospective
runtime to its authorized research-only surfaces.  Nothing is weakened.

## Runbook & docs

* `docs/research/312_h2_prospective_runtime_v1.md` — contract integration
  analysis + design
* `docs/research/312_h2_prospective_runbook_v1.md` — staged, reversible
  deployment runbook (each stage requires its own GO)
* `deploy/env/h2-prospective-runtime.env.example`,
  `deploy/systemd/trad-bot-h2-prospective-runtime.service`,
  `deploy/sql/312_h2_prospective_role.sql` — templates only

## Known limitations / open decisions

* the #305 cohort currently contains **0** source events — prospective
  coverage cannot be demonstrated yet (`SOURCE_EMPTY_WAITING`); development
  is not blocked by this;
* the execution/outcome model is `BLOCKED_PENDING_DECISION`
  (D-1..D-12 in the FR-06 spec) — no yield metric may be published until
  those are approved;
* policy fingerprints are recorded but NOT frozen (windows 3/6 remain
  preliminary candidates);
* the real-PG integration suite requires `ME_H2PRO_PG_INTEGRATION=1` on a
  test server — RUN in the remediation round on an isolated scratch
  PostgreSQL 17.2 (see below).

---

## Review remediation (round 2) — commit `979a565`

All five review findings fixed and covered by tests; full report:
`reports/issue312_review_remediation_v1.md`.

| Finding | Fix (summary) | Evidence |
|---|---|---|
| **P1 Decimal loss** — `Decimal` serialized to `str`, restore fed `str` into `run_h2_event` -> valid #305 event became `INVALID_SOURCE` | strict `parse_source_price()`/`exact_price_str()` (exact `Decimal`, rejects NaN/Inf/<=0/non-plain strings); NUMERIC column authoritative + exact string in payload; strict restore; DB CHECK on prices; restore/engine price errors -> explicit terminal `INVALID_SOURCE` | real PostgreSQL 17.2 `NUMERIC` round-trip incl. `Decimal('123.4567890123456789')` exact -> restore -> `run_h2_event` (`original_price_exact` verbatim); DB refuses NaN/Inf/0/negative; redelivery + restart equivalence |
| **P1 Watermark loss** — `(signal_time, key)` cursor dropped late-committed events with earlier `signal_time` | delivery cursor is CAPTURE order `(captured_at, source_event_key)`; per cycle: forward scan from cursor (guaranteed progress) + bounded overlap sweep (`H2_PROSPECTIVE_SOURCE_OVERLAP_S`, default 7200s) for capture-to-commit disorder and `captured_at` ties; idempotent ingest, variant states re-ensured on duplicates; checkpoint-last | mandatory scenario (B captured first / A earlier signal delivered later -> both accepted, evaluated exactly once) + late-commit-after-restart, equal-timestamp ties, batch boundaries, crash-between-ingest-and-checkpoint — all green |
| **P1 API budget** — 1 consume per paginated `get_klines_range()`; budget reset on restart | `try_consume(count)` reserves the ACTUAL HTTP cost of the full pagination (`pages_for_range` mirrors `BybitClient` pagination; 1 page = 1 request under the factory-pinned single-attempt client; conservative `requests_per_page`); retries re-reserve; exhaustion -> zero requests; hourly window PERSISTED in new `api_budget_state` (hydrate/save, fail-closed latch on save failure); production limiter untouched | fake-transport acceptance table: multi-page, retry recount, exhaustion blocks all requests, restart persistence (in-memory + real PG), rate-limit fail-closed, budget-unconfirmed -> 0 requests |
| **P2 Exit code** — `--once` returned `EXIT_OK` on exception | `EXIT_CYCLE_FAILED=1` on any failure; `store.close()` in `finally` both paths; 0/2/3 semantics kept | exit-code tests: success 0, failure != 0, disabled 3, config error 2, store always closed |
| **P2 Migration 095 re-apply** — plain `CREATE TRIGGER` broke re-application | `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` (idempotent re-apply); DO-guard raises `H2P_SCHEMA_INCOMPATIBLE` on incompatible pre-existing schema with full rollback; new `api_budget_state`; price CHECK; renamed cursor columns | scratch-PG tests: first apply + re-apply (x3) pass; re-apply preserves existing results byte-identical and keeps immutability triggers; induced error rolls back everything; incompatible schema predictably refused |

Plus (task §7): an enabled runtime with empty policy fingerprint pins refuses
to start unless `H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES=1` — unfrozen-policy
accumulation always requires a deliberate act (fail closed).

**Test attribution (commit `979a565`, real runs):**

* #312 prospective suite incl. real PostgreSQL 17.2 (scratch cluster,
  `ME_H2PRO_PG_INTEGRATION=1`): **125 passed / 0 skipped / 0 failed**;
* offline-only run: 108 passed / 17 skipped (PG opt-in flag unset);
* H2 PR #311 regression (engine + purity): **168 passed / 0 failed**;
* real Bybit requests during testing: **none** (fake transport only).

Unverified/limited scenarios are listed explicitly in
`reports/issue312_review_remediation_v1.md` §5 (empty #305 cohort, residual
capture-to-commit disorder beyond the overlap window, page=1-request
invariant pinned by client settings, load characteristics).

**Status:** `IMPLEMENTATION_READY / RE_REVIEW_REQUIRED / ACTIVATION_HOLD`

## Review remediation — round 2 review (Source Overlap Starvation, `RE_REVIEW_CHANGES_REQUIRED`)

**P1 — Source Overlap Starvation**: the overlap sweep re-read the first pages of
the window every cycle without saving progress, and the shared batch budget let
the forward scan starve reconciliation entirely — late events near the end of a
large window could stay unprocessed forever.

- **Durable overlap cursor** (`overlap_cursor` singleton, migration **096**):
  reconciliation progress survives restarts and is independent of the forward
  cursor (`reader_watermark`).
- **Independent per-cycle budgets**: `source_forward_batches_per_cycle` (8) and
  `source_overlap_batches_per_cycle` (2) — neither phase can starve the other.
- **Deterministic passes (FR-03)**: the pass ceiling is pinned at pass start
  (forward advancement cannot exclude unchecked ranges); progress checkpoints
  after every durably processed batch; the next pass floor is
  `min(previous ceiling, new ceiling − source_overlap_s)` — the documented
  window is re-checked while the sweep keeps up, and when it falls behind the
  sweep catches up contiguously (ranges are never silently skipped).
- **Honest coverage boundary (FR-05)**: no `NEVER_LOST` claims — the guarantee
  is the documented overlap window; when it cannot be honoured the cycle reports
  the diagnostic status `OVERLAP_COVERAGE_DEGRADED` (health row + log marker)
  instead of a false coverage confirmation, and every report publishes
  `details.overlap.guarantee_floor_us` / `oldest_unchecked_capture_ts`.
- **Telemetry (review §4)**: 8 new metrics — `forward_batches_scanned`,
  `overlap_batches_scanned`, `overlap_pass_completed`, `overlap_cursor_lag_s`,
  `oldest_unchecked_capture_ts`, `late_events_discovered`,
  `overlap_backlog_estimate`, `overlap_restart_recovery` — in counters, runtime
  health and cycle logs; no per-event logging.
- **Crash safety (FR-04)**: checkpoint-last; the five failure points are covered
  by tests; redelivery is idempotent; terminal outcomes are never recreated.

**Migration 096** (new version — 095 untouched, per review §6): idempotent,
transactional, `H2P_SCHEMA_INCOMPATIBLE` guard; re-apply preserves ingest
events / variant states / results / conflict ledger. All PG tests run on an
isolated scratch PostgreSQL 17.2 only — production DB untouched.

**Tests** — all 12 mandatory scenarios plus the key mandatory test (>1000
overlap rows, 2 batches/cycle, late event near the window end, mid-pass
restart — the event is discovered exactly once):
`tests/test_me_entry_quality_h2_prospective_overlap_reconciliation_v1.py` (14)
+ `TestPgOverlapCursor` / `TestPgMigration096` (8) on real PostgreSQL.

**Regression** (real runs): #312 suite incl. real PG — 147 passed / 0 failed;
H2 PR #311 engine + purity — 168 passed; full-suite baseline comparison vs
`388e4ba` with failing-test attribution in
`reports/issue312_review_remediation_v1.md` §3.

**Status:** `IMPLEMENTATION_READY / RE_REVIEW_REQUIRED / ACTIVATION_HOLD` —
PR stays **Draft**; no merge / deploy / activation / production migration was
performed.
