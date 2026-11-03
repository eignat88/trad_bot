# Issue #253 Stage E — OOS / research-data-integrity safety of shared Bybit limiter denials

Branch: `fix/issue253-bybit-shared-limiter-alignment` (based on `origin/main` of `github.com/eignat88/trad_bot`).
Analysis type: **read-only**; no source files were modified (this document is the only file created).

## 1. Question and deny mechanics (baseline)

Unacceptable scenario to rule out:

> limiter wait/deny → consumer silently skipped the event → evaluator proceeded as if the
> source were complete → checkpoint advanced / OOS outcome finalized on an incomplete source.

Deny mechanics (evidence):

- `app/exchange/bybit_client.py:43` — `class BybitRateLimitError(BybitError):`; `bybit_client.py:24`
  — `class BybitError(RuntimeError):` → **every** `except Exception` in the codebase catches a limiter deny.
- `bybit_client.py:676-682` — deny raises before any HTTP attempt:

  ```python
  if not grant.granted:
      rec("BYBIT_API_SKIP", ret_msg=_limiter_skip_label(grant))
      raise BybitRateLimitError(
          f"{symbol}: {endpoint} deferred by shared rate limiter "
          ... + ") — not sent (fail-closed)")
  ```

  Also `bybit_client.py:669-675` (limiter state unavailable) and `bybit_client.py:685-689`
  (grant revoked) — all `rec("BYBIT_API_SKIP", ...)` + `BybitRateLimitError`.
- `bybit_client.py:900-905` — `get_klines()` is **latest-N** (`rows.reverse()` after the Bybit
  newest-first page); `bybit_client.py:913` documents "trading callers retain their existing
  latest-N behavior". A deny therefore never yields `[]` from `get_klines` — it raises. `[]` can
  only come from a *successful* response that is empty or that the caller then filters down.
- Contract: `docs/issue231_r2_limiter_config_contract_v1.md:152` — "deny → `BybitRateLimitError (not sent)`
  with zero HTTP attempts".

**Headline result:** the literal chain `deny → [] → treated as complete → finalize` was **not found
in any consumer** — every generic evaluator guards the empty list. However, a **closely related
silent-finalization path DOES exist** in the five generic evaluators: a *non-empty but
non-covering* latest-N window (the exact "R1" class) is silently accepted, matured horizons are
stamped as evaluated with `NULL` economics, and `is_final=True` is written with all-false TP/SL /
target flags. Repeated limiter denies are one of the few realistic ways to *reach* that window
backlog (see Risk #1). Details in §4.

## 2. Per-consumer table

Legend for "checkpoint-on-failure": what is persisted when the Bybit fetch is refused.

| # | Consumer | Evidence (file:line) | Exception types caught | Empty list treated as complete? | Checkpoint / outcome on missing data | Source-unavailable state? | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | `scanner_runner._run_oos_evaluator` → `MERLongCLoOosEvaluator` | `scanner_runner.py:851-870`, call at `scanner_runner.py:1674-1680`; `app/shadow/me_r_long_close_location_oos_evaluator.py:43-62` | generic `Exception` (`evaluator.py:54`); no Bybit-specific catch | **No** — `fetch_historical_5m` raises instead of returning short: `app/shadow/me_r_close_location_historical_source.py:120-121` `raise HistoricalSourceError("INCOMPLETE_CANDLE_COVERAGE")` | no save on failure: `me_r_long_close_location_oos_evaluator.py:145` `save_outcome_partial` is only reached after a successful window; error counted at `:54-60` | error counter + `logger.exception` only | **SAFE** (limiter deny) |
| 2 | `app/research/evaluator.py` (`ResearchEvaluator`, generic OOS) | fetch `:260-272`; cycle guard `:417-423`; stamping `:309-339`; finalize `:342-360` | generic `Exception` (`:270`, `:427`) — catches `BybitRateLimitError` | deny → `[]` → **skipped** (`:421-423` `if not candles: ... continue`), but a **short non-empty window IS accepted silently** (`:267` caps `needed` at 1000 bars ≈ 3.47 days) | fetch failure → no upsert (safe). Short window → `evaluated_*_at=now` with `mfe/mae=None` (`:334-339`) and `is_final=True` (`:347-360`) | none | **RISK** (Risk #1) |
| 3 | `app/research/prospective_evaluator.py` (`ProspectiveOOSEvaluator`) | fetch `:202-211`; cycle guard `:483-486`; stamping `:259-291`; finalize `:294-305` | generic `Exception` (`:209`, `:503`) | deny → `[]` → skipped (`:484-486`); short non-empty window accepted (`:206` 1000-bar cap) | same as #2: `is_final=True` at `:298-305`; `_check_tp_sl` defaults to no-hit when no candle falls in-window (`:67-72`, loop `:83-85` breaks immediately) | none | **RISK** (Risk #1) |
| 4 | `app/shadow/evaluator.py` (`ShadowSignalEvaluator`) | fetch `:178-190`; cycle guard `:313-316`; stamping `:229-244`; finalize `:247-261` | generic `Exception` (`:188`, `:320`) | deny → `[]` → skipped (`:314-316`); short window accepted (`:185` cap) | `mfe/mae=None` when no in-window candle (`:87-88`, `:114-115`) still stamped `:234`; `is_final=True` `:255-261`; `_check_target_achievement` returns all-`False` on empty in-window set (`:130-136`) | none | **RISK** (Risk #1) |
| 5 | `app/shadow/srr_research_evaluator.py` | fetch `:130-142`; cycle guard `:369-372`; stamping `:240-244`; finalize `:246-259` | generic `Exception` (`:140`, `:376`) | deny → `[]` → skipped (`:370-372`); short window accepted (`:137` cap) | same pattern as #2 (`:244` stamp, `:259` `is_final=True`) | none | **RISK** (Risk #1) |
| 6 | `app/shadow/v2d_evaluator.py` | fetch `:120-132`; cycle guard `:226-229`; stamping `:161-166`; finalize `:168-177` | generic `Exception` (`:130`, `:233`) | deny → `[]` → skipped (`:227-229`); short window accepted (`:127` cap) | same pattern (`:166` stamp, `:174-177` `is_final=True`) | none | **RISK** (Risk #1) |
| 7 | SRR specialized prospective evaluator (wired in `evaluator_runner`) | `app/research/srr_short_execution_r_expansion_prospective_evaluator.py:446-452` (source), `:643-662` (route), `:950-959` (stats) | generic `Exception` in source → **typed status** `SOURCE_FETCH_ERROR` (`:446-452`) | No — failure becomes `status="SOURCE_UNVERIFIABLE"`, `finalization_eligible=False` (`:643-662`) | no economics persisted; counted in `stats["source_errors"]` (`:950-959`) | explicit `source_status=SOURCE_FETCH_ERROR` | **SAFE** |
| 8 | ME V3 evaluator (`GuardedV3Evaluator` path) | `app/research/me_reverse_long_v3/historical_source.py:41-49`; `app/research/me_reverse_long_v3/evaluator.py:141-161`, `:204-209` | generic (`evaluator.py:204`) | No — `result.status != SOURCE_VALIDATED` → `[], reason` (`historical_source.py:48-49`) → record written with `status="COVERAGE_INVALID"`, **not** `FINALIZED` (`evaluator.py:151-161`) | `COVERAGE_INVALID` row only; exception → `errors` counter, no upsert (`:204-209`) | explicit `coverage_reason` | **SAFE** |
| 9 | `app/research/htf_baseline_short_phase2_observer_runner_v1.py` (+ `..._bybit_candle_source_v1.py`) | runner `:719-747`; source `:417-433`, `:356-357`, `:373-377`, `:380-384` | typed `CandleSourceError`/`BybitCandleSourceError` (`runner:723`), source catches generic `Exception` and re-raises typed (`:417-418`, `:468-470`) | No — source **raises** on deny (`:417-418` → `_transport_failure` `:425-433`), on empty (`:356-357`), on missing bars (`:373-377`), on short history (`:380-384`) | symbol **blocked**, nothing advanced: `runner:723-736` `... "action": "SOURCE_BLOCKED"` + `continue`; comment `:724-726` "BLOCK the symbol, advance nothing, emit nothing"; `save_checkpoint` at `:747` only persists cursors that were never moved for the blocked symbol | explicit `SOURCE_BLOCKED` coverage report + `metrics.source_errors` | **SAFE** |
| 10 | `app/shadow/runner.py` (observer) | `:47-52`, `:208-231`, `:264-279` | **typed** `isinstance(exc, BybitRateLimitError)` (`:49`), else generic re-raise → scan error | n/a (observer); deny → counted drop `rate_limit_drops += 1` (`:268-271`) | **no observation row created for that symbol this cycle**; only in-memory `summary["rate_limit_drops"]` (`:307`) + `logger.warning` (`:226-229`) | counted counter only (no durable per-symbol record) | **SAFE** (no finalization) with note Risk #4 |
| 11 | `app/shadow/v2d_runner.py` (observer) | `:48-53`, `:164-182`, `:264-267` | typed `BybitRateLimitError` (`:50`) else generic | n/a | same counted drop (`:265-267`, `summary["rate_limit_drops"]` `:307`) | counted counter only | **SAFE** with note Risk #4 |
| 12 | `paper_runner.py` → `MELongEarlyExitShadowObserver` (DB-candle consumer) | `paper_runner.py:141-157`; `app/shadow/me_long_early_exit_shadow.py:326-339`, `:374-385`, `:446-453`, `:596-604` | generic `Exception` (`paper_runner.py:151`; shadow `:296`) | empty → `{"status": "NO_CANDLES"}` and **no write** (`:332-333`); **partial** `market.candle` window is accepted with no coverage check (`:335-339`) | evaluation row written from whatever candles exist (`:374-385`); trade close finalizes `status='CLOSED'` + `delta_r` from the stored `rule_triggered` (`:446-453`, `:596-604`) — **no re-check of coverage at close** | none in this module (analytics pipeline has gap reconciliation, see §3) | **RISK** (Risk #3, indirect) |
| 13 | `me_short_a_forward_outcome_runtime_runner_v1.py` + runtime + lineage | runner `:157-170` (client joins limiter bucket); `app/research/me_short_a_forward_outcome_runtime_v1.py:1541-1546`, `:1588-1604`, `:1606`; sources `:326-339`; lineage `:548-568` | generic `Exception` (`runtime:1543`, sources `:328`) | deny → `closed_candles` **raises** → event not advanced (`:1543-1546` error counted, event stays open, retried next cycle). Partial window → `finalize_outcome` refuses economics: `lineage:548-568` `if not (coverage_complete and asof_ok): ... "is_final": False` | `is_final=False`, `OUTCOME_STATUS_SOURCE_INCOMPLETE`, `missing_fields`/`source_quality_flags` persisted (`lineage:548-568`, `runtime:1596-1604`) | explicit `SOURCE_INCOMPLETE` outcome status + `source_window_unavailable` flag (`sources:331-332`) | **SAFE** (exemplary) |
| 14 | `app/research/htf_point_b_observation_recovery.py` | whole file: no `BybitClient`/`get_klines`/`_public_get` references (grep over file) | n/a — DB-only recovery of already-completed events (`:254-307`, `:310-320`) | n/a | inserts observations from immutable DB rows; malformed rows → `"malformed"` verdict (`:219-221`) | n/a | **NOT APPLICABLE** (no limiter exposure) |
| 15 | `app/research/evaluator_runner.py` (scheduler) | `:300-305` (generic cycle catch), `:314-328` (prospective cycle catch) | generic `Exception` | n/a — a failed cycle is logged and the loop sleeps (`:336-338`) | no checkpoint semantics in this file; DB rows untouched on failure | none | **SAFE** (orchestration only) |
| 16 | `app/research/candle_coverage_guard_v1.py` (new, untracked) | whole file; **zero importers** (grep `import candle_coverage_guard` / `from app.research.candle_coverage_guard` → no matches) | classifies limiter deny: `:434-437` `if type(exc).__name__ == "BybitRateLimitError" ... return SOURCE_FETCH_ERROR, "BYBIT_RATE_LIMITER_DENY"` | explicitly refuses empty/short as valid: `:571-574` `EMPTY_KLINE_RESPONSE → SOURCE_INCOMPLETE_COVERAGE` | not a consumer; its docstring claims `finalization_eligible` is "enforced by the callers (see FIX-1 §5)" (`:33-37`) but **no caller exists** | defines `SOURCE_FETCH_ERROR` / `SOURCE_INCOMPLETE_COVERAGE` / `SOURCE_NOT_YET_AVAILABLE` (`:59-69`) | **BLOCKER** (protection not wired — Risk #2) |

## 3. Confirmed safe patterns (file:line)

1. **Fail-closed historical source (ME clean OOS)** — `app/shadow/me_r_close_location_historical_source.py:120-121`:
   ```python
   if any(timestamp not in by_open for timestamp in expected):
       raise HistoricalSourceError("INCOMPLETE_CANDLE_COVERAGE")
   ```
   Caller never saves on exception: `app/shadow/me_r_long_close_location_oos_evaluator.py:50-60`
   (`except Exception: stats["errors"] += 1 ... logger.exception(...)`) and `save_outcome_partial`
   (`:145-149`) is only reached after a fully covered window.

2. **Empty-window guard in every generic evaluator** (deny → `[]` → skip, never evaluate):
   - `app/research/evaluator.py:421-423` — `if not candles: stats["errors"] += len(signals); continue`
   - `app/research/prospective_evaluator.py:484-486` — same
   - `app/shadow/evaluator.py:314-316` — same
   - `app/shadow/srr_research_evaluator.py:370-372` — same
   - `app/shadow/v2d_evaluator.py:227-229` — same

3. **Typed source status → finalization blocked (SRR route)** —
   `app/research/srr_short_execution_r_expansion_prospective_evaluator.py:643-662`:
   ```python
   if source_result.status == SOURCE_FETCH_ERROR:
       return SrrRouteResult(... status="SOURCE_UNVERIFIABLE" ...,
                             finalization_eligible=False,
                             source_status=SOURCE_FETCH_ERROR, ...)
   ```
   plus `:950-959` counting `source_errors` for `SOURCE_FETCH_ERROR`/`SOURCE_INCOMPLETE_COVERAGE`.

4. **ME V3 coverage gate** — `app/research/me_reverse_long_v3/evaluator.py:151-161` writes
   `status="COVERAGE_INVALID"` instead of `FINALIZED` whenever `fetch_error` is set
   (`historical_source.py:48-49`).

5. **HTF Phase-2 observer fail-closed source contract** —
   `app/research/htf_baseline_short_phase2_observer_runner_v1.py:723-736`:
   ```python
   except CandleSourceError as exc:
       # fail-closed source contract (API error / rate limit /
       # incomplete pagination): BLOCK the symbol, advance nothing,
       # emit nothing; other symbols continue
       ...
       {"symbol": symbol, "action": "SOURCE_BLOCKED", "reason": exc.reason}
   ```
   Source side: `..._bybit_candle_source_v1.py:417-418` (`except Exception as exc: raise self._transport_failure(...)`),
   `:432` `REASON_RATE_LIMITED` when the message matches `_RATE_LIMIT_MARKERS` (`:120`),
   `:356-357` / `:373-377` / `:380-384` raise instead of returning short data.

6. **ME SHORT A "no economics without complete coverage"** —
   `app/research/me_short_a_forward_outcome_lineage_v1.py:548-568`:
   ```python
   # ── matured but source-incomplete: never produce economics ─────────
   if not (coverage_complete and asof_ok):
       base.update({ "outcome_status": OUTCOME_STATUS_SOURCE_INCOMPLETE, ...,
                     "is_final": False, "finalized_at": None, ... })
       return base
   ```
   with `:483-485` `coverage_complete = (candles_present == expected_count)` and explicit
   `missing_fields`/`source_quality_flags` (`runtime_v1.py:1596-1604`).

7. **Counted fail-closed drops in observers** — `app/shadow/runner.py:47-52` + `:224-231` +
   `:268-271` (`rate_limited=True ... rate_limit_drops += 1`, comment at `:269-270` "counted
   fail-closed drop ... never silent"); mirrored in `app/shadow/v2d_runner.py:48-53`, `:175-182`,
   `:265-267`.

8. **`get_klines_range` fail-closed contract** — `app/exchange/bybit_client.py:918-926`:
   "a transport, API or payload-shape error RAISES and no partial history is returned, so a
   transient failure can never finalize a historical outcome from half a window".

9. **Analytics gap reconciliation / backfill for `market.candle`** —
   `app/analytics/candle_sync.py:366-448` (`reconcile_candles`, `_coalesce_gaps`),
   `app/analytics/runner.py:774-909` ("FINAL must not succeed if BLOCKING post-exit coverage is
   missing", `:777-780`).

10. **Explicit source-unavailability vocabulary that already exists** —
    `SOURCE_FETCH_ERROR` / `SOURCE_INCOMPLETE_COVERAGE` / `SOURCE_NOT_YET_AVAILABLE`
    (`candle_coverage_guard_v1.py:59-69`, `srr_short_execution_r_expansion_prospective_evaluator.py:40-41`);
    `SOURCE_UNAVAILABLE` (`app/research/me_short_a_ref_exec_alignment_audit_v1.py:138`);
    telemetry label `BYBIT_API_SKIP` (`bybit_client.py:670/677/685`, tests
    `tests/test_global_bybit_rate_limiter_integration_v1.py:493-510`).

## 4. Concrete risks / blockers (ordered by severity)

### Risk #1 — RISK / de-facto BLOCKER: five generic evaluators finalize `is_final=True` on a non-empty but non-covering candle window

This is the *actual* silent-finalization path found. It is **not** `deny → [] → finalize` (the
`[]` case is always guarded — §3.2); it is `deny/outage backlog → short non-empty latest-N window
→ horizons stamped without data → finalized`.

Chain with evidence:

1. A repeated limiter deny (or any evaluator outage) leaves signals eligible but unevaluated:
   eligibility has **no age bound** — `app/research/repository.py:417-418`
   `ORDER BY s.signal_time ASC / LIMIT %s` (oldest first), condition `:406-415`; same structure in
   `app/shadow/repository.py:190`, `app/shadow/v2d_repository.py:197-198`,
   `app/shadow/srr_research_evaluator.py:176-177`.
2. When evaluation finally runs, the fetch window starts at the **oldest** eligible signal
   (`app/research/evaluator.py:419-420` `oldest = min(...)`) but the request size is capped:
   `evaluator.py:267` — `needed = min((end_ms - start_ms) // 300_000 + 10, 1000)`
   (1000 × 5m ≈ 3.47 days; identical cap at `prospective_evaluator.py:206`,
   `shadow/evaluator.py:185`, `srr_research_evaluator.py:137`, `v2d_evaluator.py:127`).
3. `get_klines` returns only the **newest** `needed` candles (`bybit_client.py:900-905`, latest-N
   documented at `:913`), so the filter at `evaluator.py:269`
   (`[c for c in candles if start_ms <= c.timestamp <= end_ms]`) yields candles that do **not**
   reach back to the old signal's horizon windows.
4. For such a signal, the MFE/MAE helpers find no candle inside `[signal_time, signal_time+h)`
   and return `(None, None)` (`evaluator.py:49-50` / `:70`; `prospective_evaluator.py:35-36` / `:58`;
   `shadow/evaluator.py:87-88` / `:114-115`) — **yet the horizon is still stamped**:
   ```python
   # app/research/evaluator.py:334-339
   updates[f"mfe_{label}"] = mfe_pct
   ...
   updates[eval_field] = now
   ```
5. All five horizons become "done" (`evaluator.py:342-345`), so finalization runs:
   `:347-360` `updates["is_final"] = True`, and the TP/SL check over an empty in-window set
   returns the all-`False` default (`:149-153`) — i.e. **"no TP, no SL" computed from zero
   in-window candles**. The row is upserted at `:366-371`.

Identical finalize-on-`None` logic: `prospective_evaluator.py:286-305` (defaults `:67-72`,
immediate `break` `:83-85`), `shadow/evaluator.py:232-261` (defaults `:130-136`),
`srr_research_evaluator.py:240-259`, `v2d_evaluator.py:164-177`.

Note the second reachable trigger of the same bug (no limiter needed): a symbol with less than
~3.47 days of listing history, or any evaluation backlog > 1000 bars.

**Why this matters for Stage E:** an OOS outcome row reaches `is_final=True` (the irreversible
mark) computed from a source window that does not cover the evaluation horizon at all — exactly
the unacceptable scenario, reached via backlog rather than via the deny itself.

### Risk #2 — BLOCKER (process): the FIX-1 coverage guard exists but is not wired anywhere

- `app/research/candle_coverage_guard_v1.py` (untracked, new) states (`:33-37`):
  "Finalization rule enforced by the callers (see FIX-1 §5):: finalization_eligible =
  source_validated AND required_path_covered AND horizon_matured".
- Grep for `from app.research.candle_coverage_guard` / `import candle_coverage_guard` returns
  **no matches** anywhere in the repo; there are no tests referencing it either
  (`tests/*coverage*` only matches unrelated HTF/ME files).
- Its own docstring (`:6-9`) describes Risk #1 verbatim: "a *non-empty but truncated* Bybit kline
  response used to be treated as a complete evaluation path, so a generic OOS outcome could
  receive ``is_final=True`` ... computed from a partial candle path".

So today the guard is dead code: any statement in the Stage-E package that "generic OOS
finalization is coverage-guarded" would be **false** until a caller exists. Operator decision
required before wiring (see §5).

### Risk #3 — RISK: `paper_runner` ME early-exit shadow finalizes `delta_r` from an unchecked partial `market.candle` window

- `paper_runner.py:141-157` calls `observer.evaluate_variants(...)` and swallows all exceptions
  (`except Exception: logger.debug(...)`), so a Bybit-driven failure upstream is invisible at INFO.
- `app/shadow/me_long_early_exit_shadow.py:332-333` handles only the **empty** case
  (`if not candles: return {"status": "NO_CANDLES"}`); a **partial** window is accepted with no
  grid/coverage check (`:335-339` `candle_lows = [c["low"] for c in candles]; min_low = min(...)`).
- The result is persisted immediately (`:374-385`), and on trade close the row is finalized from
  the stored `rule_triggered` with **no coverage re-check** (`:446-453`, `:596-604`
  `status = 'CLOSED'`).
- Exposure to the limiter is indirect: `market.candle` is filled by the analytics candle sync,
  which has gap reconciliation (`app/analytics/candle_sync.py:366-448`) and a post-exit backfill
  gate (`app/analytics/runner.py:774-909`). A transient deny is therefore eventually repaired in
  the DB, but a shadow evaluation performed *before* the backfill is written and then frozen at
  close. If the gap is never detected (e.g. candles genuinely absent for a delisted symbol),
  nothing re-validates.

### Risk #4 — RISK (data completeness, not finalization): observers silently omit prospective events during deny windows

- `app/shadow/runner.py:224-231` / `app/shadow/v2d_runner.py:175-182`: a limiter deny converts to
  `rate_limited=True` and the symbol contributes **no observation row** that cycle. The drop is
  counted in the in-memory summary (`runner.py:307`) and logged at WARNING (`:226-229`), but there
  is no durable per-symbol "source unavailable" record; the prospective cohort for that cycle is
  simply smaller.
- Same for scanner-side observation creation: `scanner_runner.py:971-979`
  (`except Exception: logger.exception("market data failed for %s", symbol); failed += 1;
  repository.save_error(...)`) — no observation, no evaluation, only an error row.
- This does **not** finalize anything (fail-closed), but it means "no signal observed" is
  indistinguishable from "no signal existed" downstream, except by counting
  `rate_limit_drops`/`errors` in run summaries. For a prospective OOS cohort this is a silent
  denominator change during limiter pressure.

### Risk #5 — UNKNOWN (checked, absent): no durable per-symbol source-unavailable state / alerting for generic evaluators

- Grep for `SOURCE_UNAVAILABLE` → only `app/research/me_short_a_ref_exec_alignment_audit_v1.py:138`.
- Grep for `source_age`/`staleness` → only unrelated hits (HTF writer-lease
  `htf_baseline_short_phase2_observer_runner_v1.py:128` `stale_lease_after_s`, book staleness in
  `tools/research/me_short_a_execution_alignment_counterfactual_v1.py:120`, an analytics
  status-view test `tests/test_me_short_a_forward_outcome_runtime_migration_079_v1.py:136`).
- `BYBIT_API_SKIP` exists only in `bybit_client.py` + tests + `docs/issue231_r2_limiter_config_contract_v1.md`;
  no consumer reads the telemetry to gate evaluation.
- Consequence: a symbol denied for a long stretch just accumulates `stats["errors"]`; nothing
  marks the *source* as unavailable in research tables, and nothing pages an operator. This is
  safe (nothing finalizes) but **invisible**: an operator looking only at outcome tables cannot
  distinguish "evaluated" from "never evaluated because the limiter denied".

### Non-risks explicitly ruled out

- `deny → [] → finalize`: **does not occur** — all five generic evaluators guard
  `if not candles: continue` (§3.2); `MERLongCLoOosEvaluator` raises instead of returning short
  (§3.1); SRR/V3/HTF/ME-SHORT-A use typed statuses (§3.3-§3.6).
- `htf_point_b_observation_recovery.py` never touches Bybit (DB-only) → no limiter exposure.
- `evaluator_runner.py` never advances anything on a failed cycle; it logs and sleeps (`:300-305`,
  `:314-328`, `:336-338`).

## 5. Recommended minimal remediation (no frozen-policy changes)

Principle applied: **do not touch freeze timestamps, eligibility rules, research geometry, or
checkpoint semantics.** Anything that would change which rows exist or when they become final is
escalated as a BLOCKER for operator decision rather than proposed as a code change.

1. **BLOCKER — operator decision on wiring `candle_coverage_guard_v1.py` (Risk #1/#2).**
   The module already exists and already classifies limiter denies
   (`candle_coverage_guard_v1.py:434-437`). The only *semantics-preserving* way to use it is as a
   **pre-finalization gate**: before writing `evaluated_*_at`/`is_final`, run
   `validate_candle_coverage(...)`; if the result is not `SOURCE_VALIDATED`, skip the signal this
   cycle — i.e. exactly the same action today's `if not candles: continue` performs, so no row,
   timestamp, eligibility rule or checkpoint changes; the signal simply stays eligible and is
   retried next cycle. **Do not implement without operator sign-off**, because it changes when
   outcomes become final (it delays finalization on windows that today finalize incorrectly).
   This is the single change that closes both Risk #1 and Risk #2.

2. **Zero-behavior-change diagnostics (safe to do now, recommended):** in the five generic
   evaluators, when a horizon is mature and the computed `mfe_pct is None` **because no candle
   fell inside the horizon window** (not because the list was empty — that path already counts
   errors), emit a WARNING with `signal_id/symbol/horizon` and bump a dedicated stats counter
   (e.g. `horizons_finalized_without_window`). This makes Risk #1 observable in logs/summaries
   without altering any persisted value, freeze timestamp or eligibility condition.

3. **Document, don't code, the observer omission (Risk #4):** record in the Stage-E operator
   notes that `rate_limit_drops` (shadow `runner.py:307`, v2d `runner.py:307`) and scanner
   `failed`/`save_error` counts are the *only* source-unavailability signal for prospective
   observation capture, and that a drop means "observation absent from the cohort", not
   "observation existed and was neutral". Any durable per-symbol marker in research tables would
   be a schema addition → operator decision.

4. **ME early-exit shadow (Risk #3):** the conservative option is documentation only — note that
   `me_r_long_early_exit_observation` rows can be evaluated against a `market.candle` window that
   was later backfilled, and that the close path (`me_long_early_exit_shadow.py:596-604`) performs
   no coverage re-check. A code fix (re-validate the 5-minute grid completeness before
   `_update_observation_evaluation`, or re-evaluate once after `on_trade_close` when gaps close)
   changes research output timing → BLOCKER for the owner of `ME_R_LONG_EARLY_MAE_EXIT_OOS_V1`.

5. **Explicitly out of scope / do NOT do:** do not change `global_limiter_max_wait_s`, do not add
   consumer-level retries around the limiter deny (the deny is fail-closed by contract,
   `docs/issue231_r2_limiter_config_contract_v1.md:152`), do not backfill skipped observations,
   and do not alter `is_final`/`evaluated_*_at` write rules for rows that already finalized.

## 6. What was checked / UNKNOWNs

- Files read in full or in relevant part: `scanner_runner.py`, `app/research/evaluator.py`,
  `app/research/evaluator_runner.py`, `app/research/prospective_evaluator.py`,
  `app/research/candle_coverage_guard_v1.py`,
  `app/research/htf_baseline_short_phase2_observer_runner_v1.py`,
  `app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py`,
  `app/research/srr_short_execution_r_expansion_prospective_evaluator.py`,
  `app/research/srr_short_timeout_persistence_gate.py`,
  `app/research/me_reverse_long_v3/{historical_source,evaluator}.py`,
  `app/research/me_short_a_forward_outcome_runtime_{runner_v1,runtime_v1,sources_v1,lineage_v1}.py`,
  `app/research/htf_point_b_observation_recovery.py`,
  `app/shadow/{evaluator,runner,v2d_runner,v2d_evaluator,srr_research_evaluator,
  me_r_long_close_location_oos_evaluator,me_r_close_location_historical_source,
  me_long_early_exit_shadow}.py`, `paper_runner.py`, `app/exchange/bybit_client.py`,
  `app/analytics/{candle_sync,runner}.py`, `app/scanners/context_builder.py`.
- Greps run repo-wide: `BybitRateLimitError`, `BYBIT_API_SKIP`, `SOURCE_UNAVAILABLE`,
  `SOURCE_INCOMPLETE_COVERAGE`, `SOURCE_FETCH_ERROR`, `source_age|staleness`,
  `candle_coverage_guard_v1` importers, `is_final` + empty-candle combinations,
  `market.candle` writers/readers.
- UNKNOWNs (stated, not guessed):
  - Whether production `research.research_signal` currently contains signals older than
    ~3.47 days awaiting evaluation (would confirm Risk #1 is *currently* reachable, not just
    theoretically). Requires DB access — not available in this read-only task.
  - Whether the analytics gap-reconciliation schedule runs frequently enough that the ME
    early-exit shadow (Risk #3) can observe a window *before* its gap is backfilled in practice.
  - Whether any operator runbook outside this repo consumes `rate_limit_drops` for alerting
    (nothing in-repo does).
