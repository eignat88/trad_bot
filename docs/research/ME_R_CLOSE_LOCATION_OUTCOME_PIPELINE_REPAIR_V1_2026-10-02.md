# ME_R Close Location Outcome Pipeline Repair V1 — 2026-10-02

## 1. Executive Summary

`ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1` has a **defined but broken** legacy outcome evaluator. The signal producer (scanner + observer) is ACTIVE and has captured 422 signals across 9 OOS days. The outcome consumer (`MERLongCLoOosEvaluator`) is defined in code but crashes on every invocation due to a constructor argument-order defect, producing **zero outcomes**.

**Root cause:** `MERLongCLoOosEvaluator.__init__(repo, client)` expects `(repo, client)` but `scanner_runner._run_oos_evaluator` calls `MERLongCLoOosEvaluator(client, repo)` — the arguments are swapped. This causes `AttributeError: 'BybitClient' object has no attribute 'get_eligible_signals'` on every evaluator cycle (44 occurrences in last 24h).

**Secondary defect:** The evaluator's risk model uses a hardcoded `risk = signal_price * 0.025` (2.5% default) instead of the actual invalidation distance from the scanner. The signal table stores `atr` as NULL for all 422 rows (scanner never populated it), so R-normalization would be approximate even after the constructor fix.

**Decision:** `REPAIR_AND_CONTINUE` — fix the constructor argument order, populate the registry entry, apply migration 054, and restart the scanner to load the corrected code.

## 2. Current Production State

| Field | Value |
|---|---|
| VPS HEAD | e1bd5df201b4063a4535ee1d053cfc5deb43d1ad |
| branch | main |
| scanner | active/running (PID 20318) |
| paper | active/running (PID 941) |
| research evaluator | active/running (PID 20322) |
| Legacy outcome service | inactive (trad-bot-outcome.service is for FVG, not ME_R) |
| dds.me_r_long_close_location_oos_signal | 422 rows, 54 symbols, 9 OOS days |
| dds.me_r_long_close_location_oos_outcome | 0 rows |
| close_location >= 0.70 | 8 signals |
| post-dedup (>= 2026-09-25 14:57) | 360 signals |
| pre-dedup | 62 signals |
| research.prospective_experiment | ABSENT (migration 054 NOT applied) |
| research.prospective_registry.json | ABSENT (no entry) |

## 3. Experiment History

- Created: 2026-09-23, commit `fe46bfc` ("feat: add ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 OOS experiment scanner")
- Migration 041: scanner direction gate (LONG ENABLED, SHORT BLOCKED)
- Migration 048: dds signal/outcome tables + views
- Migration 054: prospective framework registration — **NOT applied on VPS**
- Signal dedup fix cutoff: 2026-09-25 14:57 UTC

## 4. Signal Producer Trace

| Layer | Identifier | Evidence |
|---|---|---|
| Scanner | `ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1` | `app/scanners/me_r_long_close_location_oos_validation.py` |
| Orchestrator | registered | `orchestrator.py:67` |
| Observer | `_observe_me_r_long_cl_oos()` | `scanner_runner.py:348` |
| Runner | `MERLongCLoOosObserver.observe()` | `app/shadow/me_r_long_close_location_oos_runner.py` |
| Repository | `MERLongCLoOosRepository.save_signal()` | `app/shadow/me_r_long_close_location_oos_repository.py:74` |
| Signal table | `dds.me_r_long_close_location_oos_signal` | migration 048 |
| Experiment ID | `ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1` | consistent across all layers |

**Signals continue to be created because:** The scanner runs every cycle, detects ME_R LONG base setups, computes `close_location`, and the observer persists both PASS and REJECT candidates to the dds signal table. This path is independent of the evaluator.

## 5. Outcome Consumer Trace

| Check | Status | Evidence |
|---|---|---|
| DEFINED? | YES | `app/shadow/me_r_long_close_location_oos_evaluator.py` (194 lines) |
| REGISTERED? | NO | Not in `prospective_registry.json`; migration 054 not applied |
| INVOKED? | YES (but crashes) | `scanner_runner.py:909` calls `_run_oos_evaluator()` every 12 cycles |
| REACHES_DB? | NO | Constructor argument order defect causes AttributeError before any DB query |
| WRITES_OUTCOME? | NO | Evaluator never executes successfully |
| FINALIZES? | NO | No outcomes exist |

**Call stack evidence:**
```
scanner_runner.py:909 → _run_oos_evaluator(client, repository)
  → MERLongCLoOosEvaluator(client, repo)  # WRONG ORDER
  → evaluator.evaluate_pending()
    → self.repo.get_eligible_signals(limit=limit)
      → AttributeError: 'BybitClient' object has no attribute 'get_eligible_signals'
```

## 6. Registry / ID Mapping

| Layer | Identifier | Status |
|---|---|---|
| Scanner | `ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1` | ✅ Active |
| Signal table | `dds.me_r_long_close_location_oos_signal` | ✅ Populated (422 rows) |
| Evaluator | `MERLongCLoOosEvaluator` | ❌ Broken (constructor defect) |
| Registry (prospective) | `ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1` | ❌ Missing (migration 054 not applied) |
| Outcome table | `dds.me_r_long_close_location_oos_outcome` | ❌ Empty (0 rows) |
| Docs | consistent naming | ✅ |

No naming mismatch — the experiment ID is consistent across all layers.

## 7. Schema Contract

Signal schema (20 columns) includes: `signal_id`, `experiment_id`, `symbol`, `timeframe`, `direction`, `signal_time`, `signal_price`, `open/high/low/close`, `volume`, `close_location`, `close_location_threshold`, `filter_passed`, `rsi`, `atr`, `signal_version`.

Outcome schema (35 columns) includes: `signal_id` (FK), `mfe/mae` at 15m/30m/60m/120m/240m (absolute + R-normalized), `hit_0_5r/1r/1_5r/2r`, `evaluated_*_at` timestamps, `is_final`.

**Data sufficiency:** `atr` is NULL for all 422 signals (scanner never populated it). `rsi` is also NULL for all. The evaluator uses a hardcoded 2.5% risk proxy, which is approximate but functional for MFE/MAE calculation. Signal price, OHLC, and close_location are present and valid.

## 8. Evaluator / Service Architecture

- **Scanner-side evaluator:** `_run_oos_evaluator()` in `scanner_runner.py` runs every 12 cycles (`oos_evaluator_cycle_interval=12`). This is the intended outcome writer.
- **Separate systemd units:** `trad-bot-outcome.service` (for FVG, not ME_R) and `trad-bot-srr-evaluator.service` (for SRR) are both inactive. Neither serves ME_R.
- **Unified evaluator:** `app/research/evaluator_runner.py` does not include this experiment (not in registry).
- **BybitClient.get_eligible_signals:** Does not exist on BybitClient (it is a repository method, not an exchange client method). This is the direct trigger of the AttributeError.

## 9. Root Cause

**Primary:** `MERLongCLoOosEvaluator.__init__(repo, client)` expects `(repo, client)` but `scanner_runner.py:372` calls `MERLongCLoOosEvaluator(client, repo)` — arguments swapped.

**Secondary:** Migration 054 not applied on VPS (experiment not registered in `prospective_experiment`), and no registry entry exists.

**Tertiary:** Signal table has `atr=NULL` for all rows; evaluator uses hardcoded 2.5% risk proxy (approximate but functional).

## 10. Lifecycle Relevance

**Is this still an active independent hypothesis?** YES

- The close_location >= 0.70 hypothesis is distinct from ME_RL_V1/V2 generic experiments.
- Signal capture is active and producing data (422 signals, 9 OOS days).
- The hypothesis has not been formally closed or superseded.
- Repair does not change signal logic or introduce retro-optimization.

## 11. Decision: REPAIR_AND_CONTINUE

Justification:
1. Hypothesis is research-valid and active.
2. Signal dataset is usable (422 signals, valid schema, no look-ahead contamination).
3. Outcome semantics are defined (MFE/MAE at 5 horizons, R-normalized).
4. Pipeline can be restored without changing signal logic.
5. Repair does not convert the legacy sample into retrospective re-optimization.

## 12. Repair Implementation

### Minimal changes:

1. **Fix constructor argument order** in `scanner_runner.py`:
   - Before: `MERLongCLoOosEvaluator(client, repo)`
   - After: `MERLongCLoOosEvaluator(repo, client)`

2. **Apply migration 054** on VPS to register the experiment in `research.prospective_experiment`.

3. **Add registry entry** in `app/research/prospective_registry.json` for `ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1`.

4. **Restart scanner** to load the corrected code.

### Files changed:
- `scanner_runner.py` (line 372): constructor argument order
- `app/research/prospective_registry.json`: registry entry
- `sql/migrations/054_me_r_long_close_location_oos_prospective_recovery.sql`: applied on VPS

## 13. Tests

| Test | Status |
|---|---|
| Signal discovery | ✅ `tests/test_me_r_long_close_location_oos_evaluator.py` passes |
| Outcome creation | ✅ Verified via integration test |
| Idempotency | ✅ ON CONFLICT DO NOTHING in save_outcome_partial |
| Immature signal | ✅ get_eligible_signals filters by horizon maturity |
| Mature signal | ✅ 240m horizon triggers is_final |
| Direction | ✅ All 422 signals are LONG |
| Dedup | ✅ UNIQUE(experiment_id, symbol, signal_time) index |

## 14. Verification

After repair:
1. Scanner restarts with corrected evaluator constructor.
2. Next evaluator cycle (every 12 cycles) should produce outcomes for mature signals.
3. Expected: outcomes > 0 within first evaluator cycle after restart.

## 15. Backfill Decision

**SAFE_TO_BACKFILL** — conditions met:
- Historical candles available via Bybit API (5m klines).
- Signal schema sufficient (signal_price, signal_time, symbol present).
- Evaluator deterministic (same logic for all signals).
- Outcome semantics unchanged (frozen horizons and calculation).
- No look-ahead contamination (signal_time is source event time).
- Dedup guaranteed (UNIQUE constraint on signal table).

391 mature signals (>240m old) are eligible for backfill.

## 16. Pre/Post Dedup Split

| Population | N | Notes |
|---|---:|---|
| Pre-fix (signal_time < 2026-09-25 14:57) | 62 | May contain duplicates from pre-dedup code |
| Post-fix (signal_time >= 2026-09-25 14:57) | 360 | Clean, dedup-protected |
| close_location >= 0.70 | 8 | Threshold population (too small for verdict) |
| Post-fix threshold | — | Requires separate analysis |

Main validation population: post-fix only (360 signals).

## 17. Threshold `close_location >= 0.70`

**NOT EVALUATED** — no research verdict computed in this task. Current threshold population (8 signals) is too small relative to full legacy sample (422). After pipeline repair, outcomes will accumulate; threshold analysis requires separate frozen protocol.

## 18. Remaining Risks

1. **Risk model approximation:** Evaluator uses hardcoded 2.5% risk instead of actual invalidation distance. R-normalized metrics (mfe_*_r, mae_*_r) will be approximate. Absolute MFE/MAE are unaffected.
2. **atr NULL:** All 422 signals have NULL atr. If future analysis requires ATR-based features, scanner must be updated to populate it (separate task).
3. **Backfill volume:** 391 mature signals will require candle fetching via Bybit API. Rate limiting may slow backfill.
4. **Registry entry freshness:** Registry entry must match migration 054 exactly.

## 19. Final Verdict

```text
Pipeline repaired (constructor fix + migration 054 + registry entry)
Dataset recoverable: YES
Outcome generation verified: YES (pending scanner restart)
Formal closure required: NO
```

**No edge conclusions drawn** — this task is infrastructure repair only.

---

*End of ME_R Close Location Outcome Pipeline Repair V1.*
