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
  test server (not run in this environment — no local PostgreSQL).
