# Issue #308 — H1-C: Historical HTF Source + Event Adapter (research-only)

**Package:** `app/research/me_entry_quality_h1c_v1/`
**Base:** `main` @ `d7f8d2b` (includes PR #307 = the Issue #297 classifier).
**Parent:** #295 `ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1`. **Dependencies:**
#296, #297, #305.

H1-C connects the pure #297 4H/1D causal classifier to source events of the
independent prospective source #305 (`ME_UPSTREAM_SOURCE_V1`), so every event
can be classified at its ORIGINAL `signal_time` for the later OOS analysis of
hypothesis H1. **No production scanner, trading filter or existing OOS is
changed.** No deploy, no migrations, no prospective-ledger backfill, no
observer start.

## 1. READ-ONLY source inventory (audit 2026-11-24)

All probes were SELECT-only (VPS `trad-bot-01`, `trad_bot` DB, PostgreSQL
17.12). Evidence: `reports/issue308_vps_readonly_inventory*.txt`.

| Candidate source | Finding | Sufficient for `canonical_lookback=60`? |
|---|---|---|
| `market.candle` (analytics store, code supports `'240'`/`'D'`) | **`market` schema does NOT exist** on the VPS DB; no candle/kline/OHLC tables anywhere | **No — zero stored history** |
| 4H/1D aggregated from stored 1H/5m | no stored 1H/5m either (no candle tables at all) | No |
| Local snapshots (`audit_db/`, `research_snapshot/`) | 5m candles only, stale (Oct 2026), unrelated symbols | No |
| `dds.scanner_setup.market_regime` / `htf_timeframe` | scanner-derived flags, snapshot at scan time, not candles, not independent | Cross-check candidate only |
| `research_me_upstream_coverage.source_event` (#305) | **0 rows**; `activation` = `RUNNING`, `freeze_ts 2026-11-24 13:31:53Z`, protocol `1.0.0`, fingerprint `7cbf151f…`; `capture_health` 0 rows | Event source only (correct), empty so far |
| `research_me_entry_quality.*` (#296 mirror) | activation empty; `source_event` 0 rows | Secondary mirror, NOT the independent source |
| Bybit public klines API (`BybitClient.get_klines` / `get_klines_range`) | the scanner's current live HTF path; bounded historical source exists in code | **Yes — but requires ADDITIONAL API calls** |

### Exact time anchoring

* `source_event.signal_time` is `timestamptz` — an absolute instant,
  converted to UTC epoch-ms exactly (`parse_utc_ms`, integer arithmetic,
  naive timestamps rejected);
* classification ASOF = `signal_time` (NEVER `captured_at`), enforced by
  `classification_asof_ms` and by an `ValueError` guard in
  `build_h1_result`;
* candle usability: `open_ms + timeframe_ms <= evaluation_asof_ms`
  (inclusive at the close boundary); Bybit `'240'`/`'D'` candles open on the
  UTC grid (00/04/08/12/16/20 UTC / 00:00 UTC), matching the #297 grids
  exactly — no local-day shift anywhere.

### Recommended option / blocker status

**Recommended:** H1-C is implemented SOURCE-AGNOSTIC and fail-closed. For
real runs (H1-D), fetch 4H/1D history in bounded offline batches from the
Bybit public kline endpoint via the existing bounded historical source
(`BybitClient.get_klines_range`), inside a separate read-only research step
(not in production, not inside the scanner), and feed the rows into
`provide_htf_candles`. Longer term, extending the analytics candle sync to
store `'240'`/`'D'` would remove the extra API calls — but that is a
production change and needs its own approval.

**Blocker recorded (no workarounds with unverified data):** the production
database currently stores NO 4H/1D history and the #305 `source_event` table
is still empty, so real-data source coverage CANNOT be confirmed in this
iteration. This development therefore embeds NO real data, fetches NOTHING,
and its verdict carries `SOURCE_COVERAGE_HOLD`. Prospective accumulation
stays blocked pending the separate #296/#305 coverage checks.

## 2. H1-C.2 read-only HTF provider (`htf_provider.py`)

`provide_htf_candles(rows, *, timeframe, evaluation_asof_ms, source_identity,
canonical_lookback, min_closed_candles, require_latest_closed)`:

* `HtfSourceIdentity` — immutable provenance: `source_kind` (closed set:
  `BYBIT_KLINE_V5` / `MARKET_CANDLE_TABLE` / `OFFLINE_SNAPSHOT` — unknown
  kinds fail closed), `source_label`, `fetched_at_ms`, `row_count`,
  `payload_hash` (mandatory canonical lowercase-hex SHA-256 of the
  canonicalized row multiset — order-insensitive, tamper-detecting; an
  empty or malformed digest fails closed with `INVALID_SOURCE_IDENTITY`);
* strict fail-closed chain: identity → `SOURCE_PAYLOAD_MISMATCH` on
  tampered/replaced rows (`row_count` AND the SHA-256 are verified
  unconditionally, never only "when the hash is set") → `ROW_PARSE_ERROR` →
  the UNCHANGED #297 validator (`CANDLE_GAP`, `DUPLICATE_CANDLE`,
  `MISSING_LATEST_CLOSED_CANDLE`, `INSUFFICIENT_HISTORY`, `INVALID_OHLC`, …
  propagated verbatim);
* **ASOF isolation**: the trailing future/unclosed suffix is excluded
  BEFORE any OHLCV/grid validation — a corrupt or off-grid future payload
  can never influence a historical classification (only rows whose open
  time is itself unparseable fail closed, because such a row cannot be
  placed in time). Only actually-closed candles are returned; the suffix
  lands in `dropped_unclosed` and is never used;
* `CoverageReport` — canonical-window coverage (`COMPLETE` / `PARTIAL` /
  `NONE`) with the exact `missing_open_ms` buckets, computed from the opens
  ACTUALLY PRESENT in the payload for every outcome — an invalid series
  gets an accurate report and a partial source is NEVER relabeled `NONE`.
  Leading shortfall = `PARTIAL` (classification runs over the available
  window); interior gaps and stale tails are INVALID (`CANDLE_GAP` /
  `MISSING_LATEST_CLOSED_CANDLE`), never silently accepted.

## 3. H1-C.3 event adapter (`event_adapter.py`)

* `adapt_source_events(rows)` normalizes #305 `source_event` rows into
  frozen `SourceEventRecord`s — `source_event_key`, `source_identity`,
  `signal_time` (`_ms` + exact `_us`), `activation_freeze_ts`, `lineage_hash`
  preserved VERBATIM (`lineage_payload()` returns exactly these fields);
* **classification ASOF = `signal_time`** (`classification_asof_ms` — never
  `captured_at`);
* duplicate `source_event_key` deliveries → explicit
  `DUPLICATE_SOURCE_EVENT` rejection (first delivery wins, deterministic
  input order); invalid rows are rejected with specific reason codes
  (`MISSING_LINEAGE_HASH`, `INVALID_SIGNAL_TIME`, …) — never coerced, never
  dropped silently;
* **timestamp precision policy** (`TIMESTAMP_PRECISION_POLICY =
  US_PRESERVED__MS_FLOOR_TOWARD_PAST`): the exact `signal_time` instant is
  preserved losslessly at microsecond resolution (`signal_time_us`); the
  epoch-ms views are derived by explicitly FLOORING the sub-millisecond
  remainder toward the past (`us // 1000`) — never float rounding, never
  implicit truncation. Flooring is causal-safe (the ASOF never extends
  beyond the true instant). `parse_utc_us` / `parse_utc_ms` accept aware
  `datetime` (any offset) at full microsecond resolution, ISO-8601 with
  explicit offset (`Z` or `±HH:MM`), integral epoch-ms integers; naive
  timestamps and non-integral floats are rejected (no UTC anchor, no
  guessing). The record rejects inconsistent `_ms`/`_us` views.

## 4. H1-C.4 result contract (`h1_result.py`, `pipeline.py`)

Per event: 4H regime/BOS/coverage + source identity, 1D ditto, one of the
nine `<4H>/<1D>` combinations preserved verbatim (or a verbatim
`INVALID_SOURCE` combination), explicit `eligible` + `eligibility_reason`,
protocol version `H1C_1.1.0`, and the full verbatim source lineage. An
`INVALID_SOURCE` on one timeframe keeps the other timeframe's own state but
forces `combined_eligible = False`. A failed source NEVER produces a market
mode.

**Eligibility requires COMPLETE canonical coverage** (`canonical_lookback =
60` frozen candidate): `eligible = True` only when BOTH timeframes are
valid AND both report `COMPLETE` window coverage (60/60 candles). A
classification over a partial window (e.g. 17 or 59 of 60) is kept as a
DIAGNOSTIC result — the regime over the available window is computed and
`combined_eligible` stays True — but it never gets `eligible = True` and
never `outcome.ok = True`. `eligibility_reason` names the gap explicitly
(`HTF_4H_INCOMPLETE_COVERAGE` / `HTF_1D_INCOMPLETE_COVERAGE` /
`HTF_BOTH_INCOMPLETE_COVERAGE`); `outcome.reason = INCOMPLETE_COVERAGE`
distinguishes it from `INVALID_SOURCE`.

`run_h1_event(record, rows_4h, rows_1d, source_4h=…, source_1d=…)` runs the
three stages purely and returns `H1EventOutcome` (`ok = result.eligible`).

## 5. Tests (all deterministic, offline)

`tests/test_me_entry_quality_h1c_provider_v1.py`,
`tests/test_me_entry_quality_h1c_event_adapter_v1.py`,
`tests/test_me_entry_quality_h1c_result_v1.py`,
`tests/test_me_entry_quality_h1c_purity_v1.py` — **124 passed / 0 failed /
0 skipped** after the PR #309 review remediation, covering: ASOF-at-close
boundary, ASOF isolation of corrupt future payloads, no-lookahead /
future-append invariance, stale candles, duplicate source events, invalid
source (gaps, tamper, bad OHLCV, unknown kinds, empty/malformed
`payload_hash`), immutable lineage, microsecond precision policy, accurate
coverage diagnostics on invalid series, the 59/60 vs 60/60 eligibility
boundary (plus mixed 60/59), and all nine HTF combinations over complete
60-candle windows. Plus the full regression set (H1-A/B: 215 passed
together; HTF/ASOF/coverage/governance suites re-run). No test touches
PostgreSQL or Bybit.

Governance note: `tests/test_me_entry_quality_h1_classifier_v1.py`'s
production-wiring test received a minimal intent-preserving fix — its needle
was the loose prefix `me_entry_quality_h1`, which false-matched any sibling
research package name AND any research-to-research import of the #297
package (which H1-C is REQUIRED to reuse). The needle is now the exact
package name `me_entry_quality_h1_v1` and `app/research/**` is out of scope
for "production wiring" (each research package keeps its own isolation +
purity tests). The phase-2 confinement gates remain irrelevant for this PR
(CI class `NO_PHASE2`).

## 6. Safety

`NO_EXISTING_SERVICE_RESTART`, `ISOLATED_DEPLOYMENT`, `READ_ONLY_SOURCE`,
`NO_PRODUCTION_SIDE_EFFECTS` — the package is pure (AST + token enforced:
no I/O/network/DB/clock imports), is not referenced by any production module
(test-enforced), no SQL migration, no systemd unit, no deploy, no writes to
`dds`/`research`/`research_me_upstream_coverage`, no prospective-ledger
backfill, no observer start.

## 7. H1-D recommendations (next stage)

1. **Bounded offline fetch:** one read-only research step that pulls 4H/1D
   klines per event window (60×4H + 60×1D before `signal_time`) via the
   existing `BybitClient.get_klines_range`, stores rows as research
   artifacts with `HtfSourceIdentity` provenance — no production calls, no
   in-scanner fetching;
2. **Source coverage confirmation** for #296/#305 remains a separate gate;
   do not start prospective accumulation before it passes;
3. Optionally propose extending analytics candle sync for `'240'`/`'D'`
   storage as a production change (own approval) to remove future API
   needs;
4. Sensitivity runs (`swing_lookback` 2/3/5) on real data belong to H1-D,
   with NO parameter selection before the analysis plan is frozen.
