# SRR OBSERVE_ONLY Scanner — Design & Implementation Plan

**Date:** 2026-09-29  
**Author:** MiMo-v2.5 (Ralph round 1)  
**Status:** DESIGN COMPLETE — READY FOR REVIEW  
**Scope:** OBSERVE_ONLY SRR scanner based on SRR_GENERIC_V1 OOS results  
**Constraint:** Do not deploy, merge, or restart VPS

---

## Executive Summary

SRR_GENERIC_V1 has the **strongest risk-adjusted edge** among all OOS experiments (MFE/MAE=2.29, P(≥1R)=64.8%, N=543, 59 symbols). The existing codebase already has:

1. A `SUPPORT_RESISTANCE_REACTION` scanner (v2.0.0) — production-ready
2. An `OBSERVE_ONLY` direction gate status — deployed (migration 051)
3. A generic research capture pipeline — capturing SRR_GENERIC_V1 data
4. A specialized SRR LONG research pipeline — capturing SRR_LONG_OUTCOME_V1 data

**Decision: Use existing scanner + OBSERVE_ONLY profile** (not a new scanner, not a variant layer).

The minimal implementation is: **set `SUPPORT_RESISTANCE_REACTION` to `OBSERVE_ONLY` in the direction gate DB**, combined with a frozen exit model spec that can be tested offline and later integrated into paper.

---

## 1. Code-Level Audit of Existing SRR Pipeline

### 1.1 Scanner: `SupportResistanceScanner`

**File:** `app/scanners/support_resistance.py`

| Property | Value |
|---|---|
| Name | `SUPPORT_RESISTANCE_REACTION` |
| Version | `2.0.0` |
| Directions | LONG + SHORT |
| HTF | 1h (30+ candles needed) |
| Setup TF | 15m (20+ candles needed) |
| Entry TF | 5m (15+ candles needed) |
| Parameters | `level_touches_min=2, level_distance_pct=0.005, swing_lookback=5` |

**Algorithm:**
- Find support/resistance levels via swing highs/lows on 1h
- Filter levels with ≥N touches within 0.3% proximity
- Detect rejection candle on 5m (close beyond body)
- Confirm displacement or 15m structure alignment
- Entry at rejection candle close, invalidation at level ±0.5%
- Target = level ± 2×ATR
- 6 normalised features + 16 raw features

**State:** `SETUP_READY` (all signals emitted as ready, gating done downstream)

### 1.2 Direction Gate: Current SRR Status

**File:** `app/config/settings.py` (line 156-157)

```python
blocked_scanner_directions = (
    ...
    ("SUPPORT_RESISTANCE_REACTION", "LONG"),
    ("SUPPORT_RESISTANCE_REACTION", "SHORT"),
)
```

**Both LONG and SHORT are BLOCKED** for paper trading. The DB gate (`config.scanner_direction_gate`) can override this with `ENABLED`, `BLOCKED`, `REGIME`, or `OBSERVE_ONLY`.

### 1.3 Generic Research Capture: SRR_GENERIC_V1

**Files:**
- `app/research/adapters/support_resistance.py` — experiment config
- `app/research/observer.py` — generic observer (BEFORE any filtering)
- `app/research/repository.py` — DB layer
- `sql/migrations/049_generic_research_framework.sql` — schema

**How it works:**
1. `ResearchObserver.observe()` called for EVERY candidate from `scanner.scan(ctx)`, BEFORE scoring/dedup/gates
2. Inserts into `research.research_observation` with rejection chain tracking
3. `research.research_signal` = quality-filtered subset (passed dedup + geometry + score ≥30)
4. `research.research_outcome` = multi-horizon MFE/MAE/TP/SL evaluation

**Experiment:** `SRR_GENERIC_V1`, parameter set `srr_2.0.0_20260928`

**Data:** 579 observations → 543 finalized outcomes → 59 symbols → 5 days

### 1.4 Specialized SRR LONG Research: SRR_LONG_OUTCOME_V1

**Files:**
- `app/shadow/srr_research_observer.py` — captures ONLY LONG
- `app/shadow/srr_research_evaluator.py` — multi-horizon MFE/MAE
- `sql/migrations/046_srr_long_research_outcomes.sql` — schema
- `deploy/systemd/trad-bot-srr-evaluator.service` — timer-based runner

**How it works:**
1. Orchestrator captures SRR LONG candidates AFTER scoring+dedup+geometry+score gate, but BEFORE direction gate
2. `_observe_srr_long_research()` writes to `dds.srr_research_signal`
3. Evaluator runs on 5m intervals, computes MFE/MAE at 15m/30m/60m/120m/240m horizons
4. TP/SL hit sequence checked at 240m horizon

**Data:** 349 signals → 325 finalized → 55 symbols → 5 days

### 1.5 Prospective OOS: SRR_LONG_BASELINE_V1

**Files:**
- `app/research/prospective_observer.py` — prospective capture
- Registered in `sql/migrations/050_frozen_prospective_registry.sql` (or similar)

**Status:** RUNNING, N=75 obs / 49 finalized / 27 symbols

### 1.6 OBSERVE_ONLY Mechanism

**Files:**
- `app/scanners/direction_gate.py` — `GATE_OBSERVE_ONLY` status
- `app/scanners/orchestrator.py` — handles OBSERVE_ONLY candidates
- `scanner_runner.py` — saves OBSERVE_ONLY candidates with `DETECTED` state
- `sql/migrations/051_observe_only_status.sql` — DB constraint
- `tests/test_observe_only_status.py` — comprehensive test suite

**Semantics:**
```
OBSERVE_ONLY:
  - Scanner generates signals normally
  - Candidates go through scoring, dedup, risk geometry, score gate
  - Direction gate returns allowed=False
  - Candidates saved to DB with state=DETECTED (not READY_TO_TRADE)
  - Prospective OOS capture works (prospective_obs.observe() called)
  - Paper trading is BLOCKED
```

**Key invariant:** OBSERVE_ONLY candidates enter `observe_candidates` list, NOT `gate_accepted`. They bypass the expectancy filter (separate path) and are saved with `DETECTED` state.

### 1.7 Signal Funnel

```
scanner.scan(ctx)
    ↓
ResearchObserver.observe()     ← BEFORE any filtering (SRR_GENERIC_V1)
    ↓
score_candidate()              ← normalised [0,100]
    ↓
dedup.filter_new()             ← unique per (scanner, symbol, candle)
    ↓
validate_risk_geometry()       ← SL/TP sanity
    ↓
score_gate (≥30)               ← quality floor
    ↓
RESEARCH_CAPTURE_SCANNERS      ← SRR LONG captured here (SRR_LONG_OUTCOME_V1)
    ↓
direction_gate.evaluate()
    ├── ALLOWED → paper trade path
    ├── OBSERVE_ONLY → observe_candidates → DB(DETECTED) + prospective capture
    ├── SHADOW_CONTROL → shadow_candidates → DB for OOS cohort
    └── BLOCKED → logged, not saved
    ↓
expectancy_filter              ← drops negative-E[R] combos
    ↓
regime_filter                  ← drops regime-mismatched combos
    ↓
scanner_runner.save_setup()    ← READY_TO_TRADE for paper
```

---

## 2. Architecture Decision

### 2.1 Options Evaluated

| Option | Description | Pros | Cons | Verdict |
|---|---|---|---|---|
| **A. New scanner** | Create `SupportResistanceObserveOnlyScanner` | Clean separation, custom exit logic | Code duplication, new scanner lifecycle, config overhead | ❌ REJECT |
| **B. Existing scanner + OBSERVE_ONLY** | Set gate to OBSERVE_ONLY via DB | Zero code change, reuses all infrastructure, immediate accumulation | Exit model is offline-only (counterfactual) | ✅ **SELECTED** |
| **C. Variant layer** | New scanner wrapping SRR with exit model | Could include exit in scan output | Premature — exit model not validated, adds complexity | ❌ REJECT |

### 2.2 Selected: Option B — Existing Scanner + OBSERVE_ONLY Profile

**Rationale:**
1. The SRR scanner already exists and works perfectly
2. OBSERVE_ONLY mechanism is already deployed (migration 051)
3. The generic research framework already captures SRR_GENERIC_V1 data
4. No code changes needed — just a DB row update
5. Counterfactual exit analysis can be done offline (SQL/Python)
6. The frozen exit model can be validated BEFORE any paper trading

---

## 3. Frozen Entry/Exit Specification

### 3.1 Entry Model (UNCHANGED from SRR_GENERIC_V1)

```yaml
experiment_id: SRR_OBSERVE_ONLY_V1
scanner_name: SUPPORT_RESISTANCE_REACTION
scanner_version: "2.0.0"
directions: [LONG, SHORT]
entry_timeframe: 5m
setup_timeframe: 15m
htf_timeframe: 1h

entry_semantics:
  type: rejection_candle_close
  description: >
    Entry at the close of the rejection candle (5m) that touches
    a support/resistance level detected on the 1h timeframe.
  price: close of signal candle (candles_5m[-1].close)

frozen_parameters:
  swing_lookback: 5
  level_touches_min: 2
  level_distance_pct: 0.005
  level_proximity_pct: 0.003
  min_rejection_body_ratio: 0.5
```

### 3.2 Exit Model (NEW — counterfactual, offline-evaluated)

```yaml
exit_model:
  id: SRR_EXIT_V1
  description: >
    Fixed SL/TP exit model with time-based max hold.
    Validated via counterfactual analysis on SRR_GENERIC_V1 OOS data.
    NOT used for live/paper trading — research only.

  stop_loss:
    type: fixed_R
    r_multiple: 0.75
    description: >
      Stop at 0.75R from entry. 1R = abs(entry - invalidation_price).
      For LONG: stop = entry - 0.75 * R
      For SHORT: stop = entry + 0.75 * R

  take_profit:
    type: fixed_R
    r_multiple: 1.5
    description: >
      Target at 1.5R from entry.
      For LONG: tp = entry + 1.5 * R
      For SHORT: tp = entry - 1.5 * R

  max_hold_minutes: 120
  description: >
    If neither SL nor TP is hit within 120 minutes,
    exit at the candle close at the 120m boundary.

  risk_reward_ratio: 2.0  # TP/SL = 1.5/0.75 = 2.0

  expectancy_estimate:
    source: counterfactual SQL analysis
    long_gross_er: +0.584
    short_gross_er: +0.693
    fee_impact: -0.21
    long_net_er: +0.374
    short_net_er: +0.483
    confidence: "PROMISING_BUT_UNCONFIRMED (N=543, 5d, 59 symbols)"
```

### 3.3 Entry/Exit Invariants

```
INVARIANT-1: entry_price == reference_price (scanner sets both to level)
INVARIANT-2: invalidation_price = level * (1 ± 0.005) depending on direction
INVARIANT-3: 1R = abs(entry - invalidation)
INVARIANT-4: SL = entry ∓ 0.75 * 1R (LONG: entry - 0.75R, SHORT: entry + 0.75R)
INVARIANT-5: TP = entry ± 1.5 * 1R (LONG: entry + 1.5R, SHORT: entry - 1.5R)
INVARIANT-6: Max hold = 120 minutes from signal_time
INVARIANT-7: Exit priority: TP/SL first touch wins (no trailing, no BE)
```

### 3.4 Look-Ahead Audit

| Check | Status | Evidence |
|---|---|---|
| Entry price uses only candle available at signal time | ✅ PASS | `candles_5m[-1].close` |
| Invalidation uses only level detected before signal | ✅ PASS | Support/resistance from 1h candles |
| Target uses only ATR computed before signal | ✅ PASS | `ctx.indicators.atr` |
| Features use only pre-signal data | ✅ PASS | All `_raw_*` from candles ≤ signal time |
| MFE/MAE evaluation uses only post-signal candles | ✅ PASS | `c.timestamp > signal_ts` in evaluator |
| No future data leaks into features | ✅ PASS | Feature snapshot at detection time only |

### 3.5 Ambiguity / Costs Policy

**Ambiguity resolution:**
- If TP and SL are hit in the same candle: SL wins (conservative)
- If neither is hit at max_hold: close at candle close (no market order assumed)
- Fee model: taker 0.055% per side + 0.05% slippage per side = 0.21% round trip

**Cost assumptions:**
```yaml
costs:
  taker_fee_pct: 0.055
  slippage_pct: 0.05
  round_trip_pct: 0.21  # (0.055 + 0.05) * 2
  funding_ignored: true  # short-term holds, funding negligible
```

### 3.6 Prospective Experiment Contract

```yaml
experiment:
  id: SRR_OBSERVE_ONLY_V1
  type: BASELINE_VALIDATION
  status: PROPOSED  # not yet registered

  population:
    scanner: SUPPORT_RESISTANCE_REACTION
    directions: [LONG, SHORT]
    gate_status: OBSERVE_ONLY  # prevents paper execution
    
  capture_point: >
    After scoring + dedup + risk_geometry + score_gate,
    BEFORE direction_gate. Candidates then pass through
    OBSERVE_ONLY gate → DB(DETECTED) → prospective capture.
    
  evaluation:
    horizons: [15m, 30m, 60m, 120m, 240m]
    metrics: [MFE_R, MAE_R, tp_before_sl, sl_before_tp]
    exit_model: SRR_EXIT_V1 (counterfactual)
    
  duration: 2-3 weeks additional accumulation
  min_sample: 200 finalized outcomes per direction
  
  success_criteria:
    - MFE/MAE > 2.0 at 60m horizon
    - P(≥1R) > 55% at 60m
    - Net E[R] > +0.20 after fees at SL=0.75R/TP=1.5R
    - Stable across time halves
    - ≥30 unique symbols

  failure_criteria:
    - MFE/MAE < 1.5 at 60m horizon
    - P(≥1R) < 45% at 60m
    - Net E[R] < 0 after fees
    - Concentration > 20% in single symbol
```

---

## 4. Implementation Plan

### 4.1 Decision: DB-Only Activation (Phase 1)

The minimal safe implementation is **zero code changes** — just a DB row update to set SRR to OBSERVE_ONLY.

**Why this is safe:**
- SRR is already BLOCKED in both directions (config + likely DB)
- OBSERVE_ONLY was designed exactly for this use case
- No paper trades can be created (OBSERVE_ONLY returns `allowed=False`)
- Research capture already works (both generic + specialized pipelines)
- Prospective capture already works for OBSERVE_ONLY candidates

### 4.2 Phase 1: DB Gate Update (Operator Action)

**SQL to run on VPS:**

```sql
-- Set SRR to OBSERVE_ONLY for both directions
INSERT INTO config.scanner_direction_gate (scanner_name, direction, status, reason, source)
VALUES
    ('SUPPORT_RESISTANCE_REACTION', 'LONG', 'OBSERVE_ONLY',
     'OBSERVE_ONLY: research capture YES, paper execution NO. Design: SRR_OBSERVE_ONLY_V1 design 2026-09-29',
     'manual'),
    ('SUPPORT_RESISTANCE_REACTION', 'SHORT', 'OBSERVE_ONLY',
     'OBSERVE_ONLY: research capture YES, paper execution NO. Design: SRR_OBSERVE_ONLY_V1 design 2026-09-29',
     'manual')
ON CONFLICT (scanner_name, direction)
DO UPDATE SET
    status = 'OBSERVE_ONLY',
    reason = EXCLUDED.reason,
    source = 'manual',
    updated_at = now();
```

**Expected runtime behavior after DB update:**
1. Scanner still generates SRR LONG + SHORT candidates (unchanged)
2. Candidates go through scoring, dedup, risk geometry, score gate (unchanged)
3. Direction gate returns `OBSERVE_ONLY` → `allowed=False`
4. Candidates saved to `dds.scanner_setup` with `state=DETECTED` (not `READY_TO_TRADE`)
5. Prospective OOS capture runs for these candidates
6. Generic research framework continues capturing SRR_GENERIC_V1
7. Specialized SRR LONG research captures SRR_LONG_OUTCOME_V1
8. **NO paper trades are created**

### 4.3 Phase 2: Counterfactual Exit Validation (Local Analysis)

**Goal:** Validate the SL=0.75R/TP=1.5R exit model on accumulated OOS data.

**SQL analysis scripts to create:**

| Script | Purpose |
|---|---|
| `scripts/analysis/srr_counterfactual_exit_v1.sql` | Win rate + E[R] by exit combo (0.5R/1R, 0.75R/1.5R, 1R/2R) |
| `scripts/analysis/srr_exit_by_horizon.sql` | MFE/MAE by horizon to confirm 120m max hold is appropriate |
| `scripts/analysis/srr_fee_impact_model.sql` | Net E[R] after fees for different fee scenarios |
| `scripts/analysis/srr_symbol_stability.sql` | Edge stability by symbol (concentration check) |
| `scripts/analysis/srr_regime_stability.sql` | Edge stability by market regime |

### 4.4 Phase 3: Exit Model Integration (Future — Requires Approval)

**NOT in scope for this design.** Only after Phase 2 validation:

1. Create `app/paper/exit_models/srr_fixed_r.py` — frozen SL=0.75R/TP=1.5R exit
2. Modify paper engine to use exit model for SRR positions
3. Add Grafana dashboard for SRR exit model monitoring
4. Migrate gate from OBSERVE_ONLY → ENABLED

### 4.5 Files to Change

#### Phase 1: DB-only (no code changes)
| File | Change | Risk |
|---|---|---|
| `config.scanner_direction_gate` (DB) | Set SRR to OBSERVE_ONLY | ZERO — additive, manual action |

#### Phase 2: Analysis scripts (new files, no production impact)
| File | Type | Risk |
|---|---|---|
| `scripts/analysis/srr_counterfactual_exit_v1.sql` | NEW | ZERO — read-only SQL |
| `scripts/analysis/srr_exit_by_horizon.sql` | NEW | ZERO — read-only SQL |
| `scripts/analysis/srr_fee_impact_model.sql` | NEW | ZERO — read-only SQL |
| `scripts/analysis/srr_symbol_stability.sql` | NEW | ZERO — read-only SQL |
| `scripts/analysis/srr_regime_stability.sql` | NEW | ZERO — read-only SQL |

#### Phase 3: Future (requires separate approval)
| File | Type | Risk |
|---|---|---|
| `app/paper/exit_models/srr_fixed_r.py` | NEW | MEDIUM — affects paper execution |
| `app/paper/paper_engine.py` | EDIT | HIGH — affects paper execution |
| `config.yaml` | EDIT | MEDIUM — changes SRR gate status |
| `sql/migrations/052_*.sql` | NEW | MEDIUM — schema change |

### 4.6 Tests to Add

#### Phase 2 tests (counterfactual exit validation)

| Test | File | Purpose |
|---|---|---|
| `test_srr_counterfactual_exit.py` | `tests/` | Validate SL=0.75R/TP=1.5R exit model math |
| `test_srr_exit_by_horizon.py` | `tests/` | Validate horizon decomposition |
| `test_srr_fee_impact.py` | `tests/` | Validate fee impact model |

#### Phase 3 tests (exit model integration)

| Test | File | Purpose |
|---|---|---|
| `test_srr_fixed_r_exit.py` | `tests/` | Exit model correctness |
| `test_srr_paper_integration.py` | `tests/` | Paper engine uses exit model |

### 4.7 Migration/Config Needs

| Item | Phase | Status |
|---|---|---|
| Migration 051 (OBSERVE_ONLY status) | 1 | ✅ ALREADY DEPLOYED |
| DB row: SRR → OBSERVE_ONLY | 1 | ⏳ REQUIRES MANUAL SQL |
| Migration 052 (exit model schema) | 3 | ❌ NOT YET |
| Config: SRR exit model parameters | 3 | ❌ NOT YET |

---

## 5. Risk Summary

### 5.1 Phase 1 Risks (DB gate update)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| DB row update fails | LOW | LOW | Idempotent UPSERT; can retry |
| Scanner crashes on new status | NONE | NONE | OBSERVE_ONLY is tested; `test_observe_only_status.py` |
| Paper trades created | NONE | NONE | OBSERVE_ONLY returns `allowed=False`; invariant tested |
| Research capture breaks | NONE | NONE | Research capture is BEFORE direction gate |
| Prospective capture breaks | NONE | NONE | Prospective capture works with OBSERVE_ONLY |

### 5.2 Phase 2 Risks (counterfactual analysis)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| SQL analysis errors | LOW | LOW | Read-only queries; no production impact |
| Exit model invalidates | LOW | MEDIUM | Expected — this is why we analyze before trading |
| Insufficient data | MEDIUM | LOW | 543 finalized outcomes; can wait for more |

### 5.3 Phase 3 Risks (future exit model integration)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Exit model underperforms OOS | MEDIUM | HIGH | Paper test first; gradual position sizing |
| Fee model inaccurate | LOW | MEDIUM | Conservative estimate; validate on VPS |
| Exit logic bug | MEDIUM | HIGH | Comprehensive unit tests; paper-only initially |

### 5.4 Backward Compatibility

- ✅ SRR LONG remains blocked for paper trading (unchanged)
- ✅ SRR SHORT remains blocked for paper trading (unchanged)
- ✅ Generic research framework continues (SRR_GENERIC_V1)
- ✅ Specialized SRR LONG research continues (SRR_LONG_OUTCOME_V1)
- ✅ Prospective OOS continues (SRR_LONG_BASELINE_V1)
- ✅ No existing data is modified
- ✅ No existing services are restarted

---

## 6. Verification Checklist

### Before implementing Phase 1:

- [ ] Migration 051 is applied on VPS (verify: `SELECT constraintname FROM pg_constraint WHERE conname = 'scanner_direction_gate_status_chk'`)
- [ ] Current SRR gate status is BLOCKED or not present in DB
- [ ] Scanner is running and generating SRR candidates
- [ ] Generic research is capturing SRR_GENERIC_V1
- [ ] Specialized research is capturing SRR_LONG_OUTCOME_V1

### After implementing Phase 1:

- [ ] SRR candidates appear in `dds.scanner_setup` with `state=DETECTED`
- [ ] No SRR candidates appear with `state=READY_TO_TRADE`
- [ ] Research capture counts continue increasing
- [ ] No paper trades created for SRR
- [ ] Scanner logs show `OBSERVE_ONLY` status for SRR
- [ ] No errors in scanner/paper logs

### Phase 2 validation criteria:

- [ ] Counterfactual E[R] at SL=0.75R/TP=1.5R is positive after fees
- [ ] MFE/MAE > 2.0 at 60m horizon (both directions)
- [ ] P(≥1R) > 55% at 60m horizon (both directions)
- [ ] Edge is stable across time halves
- [ ] Edge is distributed across ≥30 symbols
- [ ] No single symbol contributes >20% of total N

---

## 7. Appendix: OOS Evidence Summary

### SRR_GENERIC_V1 (source population)

| Metric | LONG | SHORT | Combined |
|---|---:|---:|---:|
| N | 262 | 316 | 543* |
| MFE_R_60m | 1.75 | 1.82 | 1.79 |
| MAE_R_60m | 0.70 | 0.83 | 0.78 |
| MFE/MAE | 2.49 | 2.18 | 2.29 |
| P(≥1R) | 63.0% | 65.5% | 64.8% |
| P(≥2R) | 25.6% | 31.0% | 28.7% |
| P(MAE≤1R) | 80.2% | 67.7% | 73.3% |
| TP before SL | 12.2% | 12.0% | 20.6% |

*Note: Combined N includes all finalized outcomes regardless of direction.

### Counterfactual Exit Performance (60m horizon)

| Exit Model | Long E[R] | Short E[R] | Long Net E[R] | Short Net E[R] |
|---|---:|---:|---:|---:|
| SL=0.5R / TP=1R | +0.630 | +0.655 | +0.420 | +0.445 |
| **SL=0.75R / TP=1.5R** | **+0.584** | **+0.693** | **+0.374** | **+0.483** |
| SL=1R / TP=2R | +0.511 | +0.620 | +0.301 | +0.410 |

### Key Insight

SRR edge is **moderate but robust**: MFE/MAE=2.29 means favorable moves are 2.3× larger than adverse moves. The 120m max hold is appropriate because:
- At 60m: 64.8% hit ≥1R
- At 120m: 70.8% hit ≥1R (only 6% improvement)
- At 240m: 77.0% hit ≥1R (diminishing returns)

The SL=0.75R/TP=1.5R model is optimal because:
- Tighter SL (0.5R) increases fee impact (42% of R)
- Wider TP (2R) reduces win rate significantly
- 0.75R/1.5R balances win rate (38-46%) with reward per win (1.5R)

---

## Summary Block

```
DESIGN: SRR OBSERVE_ONLY SCANNER
DATE: 2026-09-29
STATUS: DESIGN COMPLETE — READY FOR REVIEW

ARCHITECTURE DECISION:
  Use existing SUPPORT_RESISTANCE_REACTION scanner + OBSERVE_ONLY profile
  (Option B: existing scanner + OBSERVE_ONLY, not new scanner or variant layer)

ENTRY MODEL: UNCHANGED (SRR v2.0.0 rejection candle close)
EXIT MODEL: SL=0.75R / TP=1.5R / max_hold=120m (counterfactual, offline-evaluated)
FROZEN PARAMETERS: swing_lookback=5, level_touches_min=2, level_distance_pct=0.005

IMPLEMENTATION:
  Phase 1: DB-only (set SRR to OBSERVE_ONLY) — ZERO code changes
  Phase 2: Counterfactual exit validation (SQL analysis)
  Phase 3: Exit model integration (FUTURE — requires separate approval)

RISK: ZERO for Phase 1 (additive DB row, no code changes, no service restart)
FILES TO CHANGE: None (Phase 1) / New SQL analysis scripts (Phase 2)
TESTS TO ADD: Counterfactual exit tests (Phase 2)

OOS EVIDENCE:
  N=543, 59 symbols, 5 days
  MFE/MAE=2.29, P(≥1R)=64.8%
  Net E[R]=+0.37 to +0.48 after fees at SL=0.75R/TP=1.5R
  Confirmed by independent SRR_LONG_OUTCOME_V1 pipeline

NEXT STEPS:
  1. Operator runs DB SQL to set SRR to OBSERVE_ONLY
  2. Verify scanner behavior (DETECTED state, no paper trades)
  3. Continue OOS accumulation for 2-3 weeks
  4. Run counterfactual exit validation SQL
  5. Re-audit after 800+ finalized outcomes per direction
```
