# Stage 2 Phase 0 — Design Report

**Branch**: `feat/analytics-canonical-data` (from `feat/analytics-foundation`)
**Base**: `b4b4c9f` — Stage 1 validated HEAD
**Date**: 2026-09-12
**Author**: MiMo-v2.5 (automated Phase 0 audit)

---

## 1. Production Source Inventory

### 1.1. Database Schemas

| Schema | Purpose | Owner |
|--------|---------|-------|
| `dds` | Data Distribution Store — core operational entities | postgres |
| `config` | Scanner visibility, direction gates | postgres |
| `market` | OHLCV candle storage | postgres |
| `analytics` | Analysis pipeline runs, stages, quality checks (Stage 1) | postgres |
| `mart` | Grafana materialized views | postgres |

### 1.2. Core Tables — Detailed Inventory

#### `dds.scanner_run`
| Attribute | Value |
|-----------|-------|
| Primary Key | `run_id BIGSERIAL` |
| Business Key | (none — one row per physical scan pass) |
| Timestamp semantics | `started_at` = scan start; `finished_at` = scan end; `created_at` = row insert |
| Mutability | **Append-only** in practice; status transitions RUNNING→COMPLETED/FAILED |
| Available history | Full since production start |
| Nullable fields | `finished_at`, `duration_sec` (NULL while running) |
| Foreign Keys | None |
| Source of truth | Scanner engine (`app/scanners/orchestrator.py`) |

#### `dds.scanner_setup`
| Attribute | Value |
|-----------|-------|
| Primary Key | `setup_id TEXT` (e.g. `{scanner}_{symbol}_{candle_open_time}`) |
| Business Key | `(instrument_id, scanner_name, direction, entry_timeframe, signal_candle_open_time)` WHERE signal_candle_open_time > 0 |
| Timestamp semantics | `setup_started_at` = setup inception; `detected_at` = first detection; `confirmed_at` = confirmation; `executed_at` = paper entry; `invalidated_at`/`expired_at` = terminal |
| Mutability | **Mutable** — status transitions: DETECTED→CONFIRMED→READY_TO_TRADE→EXECUTED/INVALIDATED/EXPIRED |
| Available history | Full; deleted legacy rows with `signal_candle_open_time = 0` |
| Nullable fields | `run_id`, `status_reason`, `confirmed_at`, `executed_at`, `invalidated_at`, `expired_at`, `entry_zone_low/high`, `invalidation_price`, `target_1/2` |
| Foreign Keys | `instrument_id → dds.instrument`, `run_id → dds.scanner_run` |
| Source of truth | Scanner engine per-scanner |

#### `dds.market_signal`
| Attribute | Value |
|-----------|-------|
| Primary Key | `signal_id BIGSERIAL` |
| Business Key | `(instrument_id, direction, timeframe)` WHERE status = 'ACTIVE' |
| Timestamp semantics | `first_detected_at`, `last_detected_at`, `created_at`, `updated_at` |
| Mutability | **Mutable** — status transitions: ACTIVE→EXECUTED/INVALIDATED/EXPIRED/SUPPRESSED |
| Available history | Full |
| Nullable fields | `status_reason` |
| Foreign Keys | `instrument_id → dds.instrument` |
| Source of truth | Signal aggregator (`app/scanners/signal_aggregator.py`) |

#### `dds.paper_trade`
| Attribute | Value |
|-----------|-------|
| Primary Key | `trade_id BIGSERIAL` |
| Business Key | `setup_id TEXT` UNIQUE (one trade per setup) |
| Timestamp semantics | `entered_at` = fill time; `closed_at` = exit time; `created_at`/`updated_at` = row timestamps |
| Mutability | **Mutable** — status: PENDING→OPEN→CLOSED/EXPIRED/CANCELLED |
| Available history | Full |
| Nullable fields | `exit_price`, `exit_reason`, `entered_at`, `closed_at`, `duration_sec`, `market_regime`, `entry_market_price`, `price_at_expiry`, `distance_to_tp/sl`, `dca_state`, `dca_data` (JSONB), `atr_at_entry`, `dca_level_atr`, `dca_price`, `initial_fill_price/qty`, `dca_fill_price/qty`, `dca_filled_at`, `avg_entry_price`, `original_tp`, `active_tp`, `target_1/2`, `entry_zone_low/high` (not in schema but `entry_zone_*` may exist in features) |
| Foreign Keys | `setup_id → dds.scanner_setup` |
| Source of truth | Paper trading engine (`app/paper/engine.py`) |
| DCA fields | `dca_enabled`, `dca_state` (TEXT enum), `dca_data` (JSONB with full DCAPositionState), `initial_fill_price/qty`, `dca_fill_price/qty`, `dca_filled_at`, `avg_entry_price`, `original_tp`, `active_tp`, `tp_mode`, `initial_entry_pct`, `dca_target_pct`, `dca_fee`, `dca_slippage`, `atr_at_entry`, `dca_level_atr`, `dca_price` |

#### `dds.signal_outcome`
| Attribute | Value |
|-----------|-------|
| Primary Key | `setup_id TEXT` (FK → scanner_setup) |
| Business Key | Same as PK |
| Timestamp semantics | `evaluated_at`, `updated_at` |
| Mutability | **Mutable** — outcome is updated as candles arrive |
| Available history | Full |
| Nullable fields | `bars_to_entry`, `bars_to_exit`, `entry_price`, `exit_price` |
| Foreign Keys | `setup_id → dds.scanner_setup` ON DELETE CASCADE |
| Source of truth | Outcome evaluator (`app/scanners/outcome.py`) |

#### `dds.instrument`
| Attribute | Value |
|-----------|-------|
| Primary Key | `instrument_id BIGSERIAL` |
| Business Key | `symbol TEXT UNIQUE` |
| Mutability | **Mutable** — price/volume updates |
| Source of truth | Exchange universe fetch |

#### `dds.scanner_event`
| Attribute | Value |
|-----------|-------|
| Primary Key | `event_id BIGSERIAL` |
| Timestamp semantics | `detected_at`, `created_at` |
| Mutability | **Append-only** |
| Nullable fields | `run_id`, `timeframe`, `direction`, `score`, `detected_at` |
| Foreign Keys | `run_id → dds.scanner_run` |

#### `dds.paper_account`
| Attribute | Value |
|-----------|-------|
| Primary Key | `snapshot_id BIGSERIAL` |
| Timestamp semantics | `created_at` |
| Mutability | **Append-only** (periodic snapshots) |
| Nullable fields | `cooldown_until` |

#### `market.candle`
| Attribute | Value |
|-----------|-------|
| Primary Key | `(exchange, market_type, instrument_id, timeframe, open_time)` — natural key |
| Timestamp semantics | `open_time` (inclusive), `close_time` (exclusive), `ingested_at`, `source_received_at` |
| Mutability | **Append-only** (UPSERT for corrections) |
| Constraints | OHLC validation, `is_closed = TRUE` enforced |
| Quality | `quality_status`: validated/suspect/invalid |

#### `analytics.analysis_run` (Stage 1)
| Attribute | Value |
|-----------|-------|
| Primary Key | `run_id UUID` |
| Business Key | `(business_date, pipeline_version)` WHERE maturity = 'FINAL' |
| Timestamp semantics | `analysis_from`, `analysis_to`, `observation_cutoff`, `started_at`, `finished_at` |
| Mutability | **Mutable** — status/maturity transitions |

#### `analytics.analysis_stage_run` (Stage 1)
| Attribute | Value |
|-----------|-------|
| Primary Key | `stage_run_id BIGSERIAL` |
| Business Key | `(run_id, stage_name, attempt)` UNIQUE |
| Foreign Keys | `run_id → analytics.analysis_run` |

#### `analytics.data_quality_result` (Stage 1)
| Attribute | Value |
|-----------|-------|
| Primary Key | `quality_result_id BIGSERIAL` |
| Foreign Keys | `run_id → analytics.analysis_run` |

---

## 2. Lifecycle Mapping

### Current Production Lifecycle (DDS → Paper)

```
scanner_run (run_id)
    ↓
scanner_setup (setup_id, instrument_id, scanner_name, direction)
    ↓ status transitions: DETECTED → CONFIRMED → READY_TO_TRADE → EXECUTED
    ↓
market_signal (instrument_id, direction, timeframe)
    ↓ status: ACTIVE → EXECUTED
    ↓
paper_trade (trade_id, setup_id) [1:1 with setup via UNIQUE on setup_id]
    ↓ status: PENDING → OPEN → CLOSED/EXPIRED/CANCELLED
    ↓ DCA: dca_data JSONB stores full DCAPositionState
    ↓
signal_outcome (setup_id) [1:1 with setup]
    ↓ evaluates result_r, mfe_r, mae_r
```

### Key Observations

1. **No separate `entry_attempt` table exists.** The paper engine transitions `setup.status` directly from `READY_TO_TRADE` to `EXECUTED` upon fill. Entry attempt = the paper engine's internal check in `check_entries()`.

2. **No explicit trade event journal.** All lifecycle events are implicit in status transitions and JSONB payloads (`dca_data`).

3. **DCA state is fully stored in `paper_trade.dca_data` JSONB** — not in normalized columns (except for the denormalized summary columns added by migration 007).

4. **`signal_outcome`** evaluates post-hoc from candles, not from real-time events.

---

## 3. Entry Attempt Source Determination

### Current State

There is **NO dedicated `entry_attempt` table** in production. The entry attempt lifecycle is:

1. **Scanner marks setup as `READY_TO_TRADE`** — this is the signal that entry conditions are met.
2. **Paper engine `check_entries()`** evaluates the setup:
   - Checks risk gates (daily loss, consecutive losses, cooldown, max positions, exposure)
   - Checks price in entry zone
   - If all pass → creates `paper_trade` with status `PENDING` → immediately `OPEN` on fill
   - If rejected → setup stays `READY_TO_TRADE` until expiry
3. **No record of rejected entry attempts** — the paper engine logs them but does not persist.

### Decision: CREATE `analytics.entry_attempt_fact`

Since Stage 2 requires separating "setup existed" from "entry was attempted" from "fill occurred", we must **reconstruct** entry attempts from:

- `dds.paper_trade` (successful fills)
- `dds.scanner_setup` status transitions (READY_TO_TRADE but no paper_trade = rejected/expired)
- Paper engine logs (for rejection reasons — not available in DB)

**Reconstruction strategy:**
- If `paper_trade` exists for a `setup_id` → ENTRY_FILLED
- If `scanner_setup.status = 'EXECUTED'` but `paper_trade` does not exist → data inconsistency (should not happen with current code)
- If `scanner_setup.status IN ('READY_TO_TRADE', 'EXPIRED', 'INVALIDATED')` and no `paper_trade` → ENTRY_ATTEMPTED with result = EXPIRED/INVALIDATED/BLOCKED
- For rejected entries (risk gate blocks), we have **no DB record** → these will be classified as `DERIVABLE` (from setup status + absence of trade) but without rejection reason.

---

## 4. Event Availability Matrix

### Required Events vs. Production Availability

| Event | Production Source | Availability | Derivation Method |
|-------|-------------------|-------------|-------------------|
| **SETUP_READY** | `dds.scanner_setup.status = 'READY_TO_TRADE'` | ✅ AVAILABLE | `scanner_setup.confirmed_at` or status change timestamp |
| **ENTRY_ATTEMPTED** | Paper engine `check_entries()` (in-memory only) | ⚠️ DERIVABLE | From `paper_trade.created_at` (fill) or setup expiry without trade |
| **ENTRY_FILLED** | `dds.paper_trade.entered_at` + status `OPEN/CLOSED` | ✅ AVAILABLE | `paper_trade.entered_at` |
| **DCA_PLACED** | Paper engine creates virtual DCA order (in-memory) | ⚠️ DERIVABLE | From `dca_data.dca_order_active = TRUE` at any point |
| **DCA_FILLED** | `dds.paper_trade.dca_data` JSONB `dca_fill_count > 0` | ✅ AVAILABLE | `dca_data.dca_filled_at` |
| **STOP_MOVED** | Not tracked (SL never moves for DCA per invariant) | ❌ MISSING | Not applicable — SL is fixed at initial entry |
| **PARTIAL_EXIT** | Not implemented in current paper engine | ❌ MISSING | Future feature |
| **TRADE_CLOSED** | `dds.paper_trade.closed_at` + `exit_reason` | ✅ AVAILABLE | `paper_trade.closed_at`, `exit_reason` |

### Missing Events — Classification

| Event | Classification | Notes |
|-------|---------------|-------|
| STOP_MOVED | N/A | By design: `stop_price` is fixed at `initial_entry ± stop_loss_atr * ATR` and never changes after DCA |
| PARTIAL_EXIT | MISSING | Not implemented; paper engine exits full position only |
| DCA_PLACED | DERIVABLE | Virtual order; derivable from `dca_data.dca_order_active` |

---

## 5. Proposed SQL Schema

### 5.1. Schema: `analytics` (extending Stage 1)

All new tables go into the `analytics` schema, which already exists from migration 008.

### 5.2. Tables

#### `analytics.config_snapshot`
```sql
CREATE TABLE analytics.config_snapshot (
    config_hash       TEXT PRIMARY KEY,
    effective_config_json JSONB NOT NULL,
    source_map_json   JSONB NOT NULL,
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from        TIMESTAMPTZ NOT NULL,
    valid_to          TIMESTAMPTZ
);

COMMENT ON TABLE analytics.config_snapshot IS 'Canonical effective configuration snapshot, versioned by SHA-256 hash';
```

#### `analytics.strategy_snapshot`
```sql
CREATE TABLE analytics.strategy_snapshot (
    strategy_hash     TEXT PRIMARY KEY,
    git_commit_sha    TEXT,
    scanner_name      TEXT NOT NULL,
    scanner_version   TEXT NOT NULL,
    strategy_params_json JSONB NOT NULL DEFAULT '{}',
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE analytics.strategy_snapshot IS 'Immutable scanner strategy version snapshot';
```

#### `analytics.setup_fact`
```sql
CREATE TABLE analytics.setup_fact (
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    setup_id          TEXT NOT NULL,
    scanner_name      TEXT NOT NULL,
    scanner_version   TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    setup_at          TIMESTAMPTZ NOT NULL,
    signal_at         TIMESTAMPTZ,
    setup_status      TEXT NOT NULL,
    entry_zone_low    NUMERIC,
    entry_zone_high   NUMERIC,
    reference_price   NUMERIC NOT NULL,
    config_hash       TEXT,
    strategy_hash     TEXT,
    market_regime     TEXT,
    coverage_status   TEXT NOT NULL DEFAULT 'UNKNOWN',
    ambiguity_status  TEXT NOT NULL DEFAULT 'CLEAR',
    excluded_reasons  JSONB NOT NULL DEFAULT '[]',
    dataset_version   TEXT NOT NULL,
    published         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (run_id, setup_id)
);

CREATE INDEX idx_setup_fact_symbol ON analytics.setup_fact (symbol);
CREATE INDEX idx_setup_fact_scanner ON analytics.setup_fact (scanner_name);
CREATE INDEX idx_setup_fact_direction ON analytics.setup_fact (direction);
CREATE INDEX idx_setup_fact_published ON analytics.setup_fact (published, dataset_version);
```

#### `analytics.entry_attempt_fact`
```sql
CREATE TABLE analytics.entry_attempt_fact (
    attempt_id        TEXT PRIMARY KEY,
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    setup_id          TEXT NOT NULL,
    trade_id          BIGINT,  -- nullable: NULL if no fill
    attempt_at        TIMESTAMPTZ NOT NULL,
    attempt_price     NUMERIC,
    entry_model       TEXT NOT NULL DEFAULT 'PAPER',
    result            TEXT NOT NULL CHECK (result IN ('FILLED', 'REJECTED', 'EXPIRED', 'CANCELLED', 'UNKNOWN')),
    reason_code       TEXT,
    rejection_reason  TEXT,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_key  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (run_id, setup_id)
);

CREATE INDEX idx_entry_attempt_trade ON analytics.entry_attempt_fact (trade_id);
CREATE INDEX idx_entry_attempt_result ON analytics.entry_attempt_fact (result);
```

#### `analytics.trade_fact`
```sql
CREATE TABLE analytics.trade_fact (
    run_id                UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    trade_id              BIGINT NOT NULL,
    setup_id              TEXT NOT NULL,
    scanner_name          TEXT NOT NULL,
    scanner_version       TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    direction             TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    
    -- Timeline
    setup_at              TIMESTAMPTZ,
    signal_at             TIMESTAMPTZ,
    first_attempt_at      TIMESTAMPTZ,
    entered_at            TIMESTAMPTZ,
    dca_filled_at         TIMESTAMPTZ,
    closed_at             TIMESTAMPTZ,
    
    -- Prices
    reference_price       NUMERIC NOT NULL,
    entry_zone_low        NUMERIC,
    entry_zone_high       NUMERIC,
    initial_fill_price    NUMERIC,
    dca_fill_price        NUMERIC,
    avg_entry_price       NUMERIC,
    initial_stop          NUMERIC NOT NULL,
    final_stop            NUMERIC,
    target_1              NUMERIC,
    target_2              NUMERIC,
    exit_price            NUMERIC,
    
    -- Risk
    risk_usdt             NUMERIC NOT NULL,
    initial_risk_distance NUMERIC NOT NULL,
    initial_quantity      NUMERIC,
    dca_quantity          NUMERIC,
    final_quantity        NUMERIC,
    fees                  NUMERIC NOT NULL DEFAULT 0,
    slippage              NUMERIC NOT NULL DEFAULT 0,
    funding               NUMERIC NOT NULL DEFAULT 0,
    
    -- Outcome
    status                TEXT NOT NULL,
    exit_reason           TEXT,
    gross_pnl             NUMERIC,
    net_pnl               NUMERIC,
    pnl_r                 NUMERIC,
    mfe_r                 NUMERIC,
    mae_r                 NUMERIC,
    
    -- Context
    market_regime         TEXT,
    config_hash           TEXT,
    strategy_hash         TEXT,
    metric_version        TEXT NOT NULL DEFAULT '1.0.0',
    
    -- Quality
    coverage_status       TEXT NOT NULL DEFAULT 'UNKNOWN',
    ambiguity_status      TEXT NOT NULL DEFAULT 'CLEAR',
    excluded_reasons      JSONB NOT NULL DEFAULT '[]',
    
    -- Versioning
    dataset_version       TEXT NOT NULL,
    published             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (run_id, trade_id)
);

CREATE INDEX idx_trade_fact_symbol ON analytics.trade_fact (symbol);
CREATE INDEX idx_trade_fact_scanner ON analytics.trade_fact (scanner_name);
CREATE INDEX idx_trade_fact_direction ON analytics.trade_fact (direction);
CREATE INDEX idx_trade_fact_status ON analytics.trade_fact (status);
CREATE INDEX idx_trade_fact_entered ON analytics.trade_fact (entered_at);
CREATE INDEX idx_trade_fact_closed ON analytics.trade_fact (closed_at);
CREATE INDEX idx_trade_fact_published ON analytics.trade_fact (published, dataset_version);
CREATE INDEX idx_trade_fact_setup ON analytics.trade_fact (setup_id);
```

#### `analytics.trade_event`
```sql
CREATE TABLE analytics.trade_event (
    event_id          BIGSERIAL PRIMARY KEY,
    trade_id          BIGINT NOT NULL,
    setup_id          TEXT NOT NULL,
    event_type        TEXT NOT NULL CHECK (event_type IN (
        'SETUP_READY', 'ENTRY_ATTEMPTED', 'ENTRY_FILLED',
        'DCA_PLACED', 'DCA_FILLED', 'STOP_MOVED',
        'PARTIAL_EXIT', 'TRADE_CLOSED'
    )),
    event_at          TIMESTAMPTZ NOT NULL,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price             NUMERIC,
    quantity          NUMERIC,
    reason_code       TEXT,
    payload_json      JSONB NOT NULL DEFAULT '{}',
    source_event_key  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Idempotency: unique constraint on source_event_key
    UNIQUE (source_event_key)
);

CREATE INDEX idx_trade_event_trade ON analytics.trade_event (trade_id);
CREATE INDEX idx_trade_event_setup ON analytics.trade_event (setup_id);
CREATE INDEX idx_trade_event_type ON analytics.trade_event (event_type);
CREATE INDEX idx_trade_event_at ON analytics.trade_event (event_at);
CREATE INDEX idx_trade_event_source_key ON analytics.trade_event (source_event_key);
```

#### `analytics.trade_horizon_metric`
```sql
CREATE TABLE analytics.trade_horizon_metric (
    trade_id          BIGINT NOT NULL,
    anchor            TEXT NOT NULL CHECK (anchor IN ('signal', 'entry', 'dca_fill', 'exit')),
    horizon           TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',
    
    favorable_move_r   NUMERIC,
    adverse_move_r     NUMERIC,
    late_entry_loss_r  NUMERIC,
    post_exit_opportunity_r NUMERIC,
    continuation_r     NUMERIC,
    recovery_r         NUMERIC,
    
    coverage_status    TEXT NOT NULL DEFAULT 'COMPLETE',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (trade_id, anchor, horizon, metric_version)
);

CREATE INDEX idx_horizon_metric_trade ON analytics.trade_horizon_metric (trade_id);
```

#### `analytics.trade_replay_metric`
```sql
CREATE TABLE analytics.trade_replay_metric (
    trade_id          BIGINT NOT NULL,
    scenario          TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',
    
    simulated_pnl_r   NUMERIC,
    simulated_exit_price NUMERIC,
    simulated_exit_at TIMESTAMPTZ,
    simulated_exit_reason TEXT,
    
    -- Scenario metadata
    scenario_family   TEXT NOT NULL,  -- entry, dca, exit, stop
    scenario_params   JSONB NOT NULL DEFAULT '{}',
    
    coverage_status   TEXT NOT NULL DEFAULT 'COMPLETE',
    ambiguity_status  TEXT NOT NULL DEFAULT 'CLEAR',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (trade_id, scenario, metric_version)
);

CREATE INDEX idx_replay_metric_trade ON analytics.trade_replay_metric (trade_id);
CREATE INDEX idx_replay_metric_family ON analytics.trade_replay_metric (scenario_family);
```

#### `analytics.setup_counterfactual`
```sql
CREATE TABLE analytics.setup_counterfactual (
    setup_id          TEXT NOT NULL,
    scenario          TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',
    
    would_have_saved_loss BOOLEAN,
    would_have_blocked_win BOOLEAN,
    classification    TEXT NOT NULL CHECK (classification IN ('FILTER_SAVED_LOSS', 'FILTER_BLOCKED_WINNER', 'NEUTRAL')),
    
    simulated_pnl_r   NUMERIC,
    simulated_exit_price NUMERIC,
    
    coverage_status   TEXT NOT NULL DEFAULT 'COMPLETE',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (setup_id, scenario, metric_version)
);

CREATE INDEX idx_counterfactual_setup ON analytics.setup_counterfactual (setup_id);
CREATE INDEX idx_counterfactual_class ON analytics.setup_counterfactual (classification);
```

#### `analytics.metric_snapshot`
```sql
CREATE TABLE analytics.metric_snapshot (
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    period            TEXT NOT NULL CHECK (period IN ('24h', '7d', '30d')),
    segment           TEXT NOT NULL,  -- e.g. 'scanner:TREND_PULLBACK_V2:LONG'
    metric_name       TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',
    
    metric_value      NUMERIC,
    sample_count      BIGINT,
    
    window_from       TIMESTAMPTZ NOT NULL,
    window_to         TIMESTAMPTZ NOT NULL,
    
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (run_id, period, segment, metric_name, metric_version)
);

CREATE INDEX idx_metric_snapshot_run ON analytics.metric_snapshot (run_id);
CREATE INDEX idx_metric_snapshot_period ON analytics.metric_snapshot (period);
CREATE INDEX idx_metric_snapshot_segment ON analytics.metric_snapshot (segment);
```

#### `analytics.metric_registry`
```sql
CREATE TABLE analytics.metric_registry (
    metric_name       TEXT NOT NULL,
    metric_version    TEXT NOT NULL,
    formula_sql_or_code_ref TEXT NOT NULL,
    units             TEXT NOT NULL,
    grain             TEXT NOT NULL,
    direction_convention TEXT NOT NULL DEFAULT 'LONG=+1 SHORT=-1',
    fee_model_version TEXT NOT NULL DEFAULT '1.0.0',
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deprecated_at     TIMESTAMPTZ,
    
    PRIMARY KEY (metric_name, metric_version)
);

COMMENT ON TABLE analytics.metric_registry IS 'Versioned registry of all canonical metrics with formulas and conventions';
```

---

## 6. Grain / PK / UK / FK Summary

| Table | PK | Natural/Unique Key | FK |
|-------|----|--------------------|----|
| `config_snapshot` | `config_hash` | — | — |
| `strategy_snapshot` | `strategy_hash` | — | — |
| `setup_fact` | `(run_id, setup_id)` | — | `run_id → analysis_run` |
| `entry_attempt_fact` | `attempt_id` | `(run_id, setup_id)` UNIQUE | `run_id → analysis_run`, `trade_id → paper_trade` |
| `trade_fact` | `(run_id, trade_id)` | — | `run_id → analysis_run`, `trade_id → paper_trade` |
| `trade_event` | `event_id` | `source_event_key` UNIQUE | `trade_id → trade_fact` |
| `trade_horizon_metric` | `(trade_id, anchor, horizon, metric_version)` | — | `trade_id → trade_fact` |
| `trade_replay_metric` | `(trade_id, scenario, metric_version)` | — | `trade_id → trade_fact` |
| `setup_counterfactual` | `(setup_id, scenario, metric_version)` | — | `setup_id → scanner_setup` |
| `metric_snapshot` | `(run_id, period, segment, metric_name, metric_version)` | — | `run_id → analysis_run` |
| `metric_registry` | `(metric_name, metric_version)` | — | — |

---

## 7. Snapshot/Hash Specification

### Config Snapshot
- **Hash algorithm**: SHA-256 of canonicalized JSON
- **Canonicalization**: Sort keys recursively, remove secrets (passwords, API keys), normalize numeric types
- **Source map**: `{key: 'YAML'|'ENV'|'default'}` for each config field
- **Deduplication**: Same effective config → same hash → no duplicate row
- **Validity**: `valid_from` = capture time; `valid_to` = next capture time or NULL

### Strategy Snapshot
- **Hash algorithm**: SHA-256 of `(git_commit_sha, scanner_name, scanner_version, strategy_params)`
- **Immutability**: Once created, never updated

---

## 8. Metric Versioning Scheme

### Convention
- Format: `MAJOR.MINOR.PATCH`
- `MAJOR`: Breaking formula change (redefines R, changes sign convention)
- `MINOR`: New metric added, new scenario family
- `PATCH`: Bug fix, rounding change

### Initial Version
- `1.0.0` — all metrics computed in Stage 2

### Registry Entry Example
```json
{
  "metric_name": "net_pnl",
  "metric_version": "1.0.0",
  "formula_sql_or_code_ref": "gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost",
  "units": "USDT",
  "grain": "trade_fact",
  "direction_convention": "LONG=+1 SHORT=-1",
  "fee_model_version": "1.0.0",
  "valid_from": "2026-09-12T00:00:00Z",
  "deprecated_at": null
}
```

---

## 9. PIT Model

### Invariant
Only candles with `close_time <= observation_cutoff` are used for any metric computation.

### Enforcement Points
1. **Candle ingestion**: `market.candle` only stores closed candles (`is_closed = TRUE` enforced by trigger)
2. **Analytics runner**: `analysis_run.observation_cutoff` is set at run creation and fixed for the run
3. **Metric computation**: All SQL/Python queries filter `WHERE open_time < observation_cutoff AND close_time <= observation_cutoff`
4. **Quality gate**: Any source event with `observed_at > observation_cutoff` → PIT violation → run FAILED

### PIT Violation Detection
```sql
-- Check for source events after cutoff
SELECT COUNT(*) 
FROM dds.paper_trade pt
WHERE pt.entered_at > (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = :run_id)
   OR pt.closed_at > (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = :run_id);
```

---

## 10. Publication Model

### Dataset Version
- Format: `YYYYMMDD.N` (e.g. `20260912.1`)
- Incremented on each successful quality gate pass for the same business_date

### Lifecycle
```
PROVISIONAL (dataset can be rebuilt)
    ↓ quality gate PASS
    ↓ reconciliation PASS
PUBLISHED (immutable)
    ↓ published = TRUE, published_at = NOW()
```

### Immutability Enforcement
- Once `published = TRUE`, the row cannot be UPDATEd (application-level guard)
- Rebuild creates a NEW dataset_version row

---

## 11. Reconciliation Plan

### Pre-Publication Checks

| Check | Source | Canonical | Severity |
|-------|--------|-----------|----------|
| Setup count | `COUNT(*) FROM dds.scanner_setup WHERE run_id IN (SELECT run_id FROM dds.scanner_setup WHERE ...)` | `COUNT(*) FROM analytics.setup_fact WHERE run_id = :run_id` | BLOCKING if delta > 0 |
| Trade count | `COUNT(*) FROM dds.paper_trade WHERE ...` | `COUNT(*) FROM analytics.trade_fact WHERE run_id = :run_id` | BLOCKING if delta > 0 |
| Closed trade count | `COUNT(*) FROM dds.paper_trade WHERE status = 'CLOSED'` | `COUNT(*) FROM analytics.trade_fact WHERE status = 'CLOSED'` | BLOCKING if delta > 0 |
| DCA trade count | `COUNT(*) FROM dds.paper_trade WHERE dca_enabled = TRUE` | `COUNT(*) FROM analytics.trade_fact WHERE dca_filled_at IS NOT NULL` | DEGRADED if delta > 0 |
| Total PnL | `SUM(pnl_usdt) FROM dds.paper_trade WHERE status = 'CLOSED'` | `SUM(net_pnl) FROM analytics.trade_fact WHERE status = 'CLOSED'` | BLOCKING if abs(delta) > 0.01 |
| Event count | N/A (no source event table) | `COUNT(*) FROM analytics.trade_event WHERE ...` | WARNING (informational) |

---

## 12. Migration Number Proposal

| Migration | Name | Purpose |
|-----------|------|---------|
| 014 | `canonical_data_schema` | Create all Stage 2 canonical tables, indexes, constraints |
| 015 | `metric_registry_seed` | Seed initial metric registry entries |
| 016 | `event_reconstruction` | Populate `trade_event` from `paper_trade` + `scanner_setup` |
| 017 | `trade_fact_build` | Populate `trade_fact` from `paper_trade` + `signal_outcome` |
| 018 | `setup_fact_build` | Populate `setup_fact` from `scanner_setup` |
| 019 | `config_strategy_snapshots` | Populate `config_snapshot` and `strategy_snapshot` |
| 020 | `horizon_metrics` | Compute and populate `trade_horizon_metric` |
| 021 | `replay_metrics` | Compute and populate `trade_replay_metric` |
| 022 | `metric_snapshots` | Compute `metric_snapshot` aggregates |
| 023 | `quality_rules` | Create quality check functions |
| 024 | `reconciliation` | Create reconciliation functions |

---

## 13. Test Plan

### Unit Tests
1. R sign convention: LONG=+1, SHORT=-1
2. MFE_R >= 0 invariant
3. MAE_R <= 0 invariant
4. DCA does not change initial_risk_distance denominator
5. net_pnl = gross_pnl - fees - funding - slippage
6. config_hash: same config → same hash
7. config_hash: different config → different hash
8. config_hash: secrets never persisted
9. trade_event idempotency (source_event_key UNIQUE)
10. event journal append-only (no UPDATE on trade_event)

### Integration Tests (DB)
11. LONG/SHORT mirror test
12. PIT cutoff: source event after cutoff → FAIL
13. Partial candle excluded from metrics
14. Horizon coverage incomplete → metric not published
15. Intrabar stop + TP → AMBIGUOUS
16. 24h/7d/30d exact [from, to) windows
17. Same dataset build → same hashes
18. Source/canonical reconciliation
19. Publication only after quality PASS

---

## 14. Open Questions / Blockers

### Must Resolve Before Migration

1. **`dds.paper_trade` does not store entry attempt rejections.** We can DERIVE "setup existed but no trade" from `scanner_setup.status IN ('READY_TO_TRADE', 'EXPIRED', 'INVALIDATED')` + no `paper_trade` row, but we CANNOT determine the rejection reason (risk gate, cooldown, etc.) without persisting it.
   - **Proposal**: Accept `reason_code = 'UNKNOWN'` for derived attempts; enhance paper engine in Stage 3 to log rejections.

2. **`STOP_MOVED` event does not exist** because DCA design fixes SL at initial entry. This is correct per current business rules.
   - **Proposal**: Omit STOP_MOVED from event journal for now; add if SL management is implemented later.

3. **`PARTIAL_EXIT` does not exist.** Paper engine exits full position only.
   - **Proposal**: Omit from event journal; add when partial exits are implemented.

4. **`dds.paper_trade.dca_data` JSONB is the only source for DCA fill timestamps and prices.** The denormalized columns (`dca_fill_price`, `dca_fill_qty`, etc.) were added by migration 007 but may not be 100% in sync with JSONB for all historical rows.
   - **Proposal**: Use denormalized columns as primary source; fall back to JSONB if NULL.

5. **`dds.signal_outcome.mfe_r` and `mae_r` use the production R convention** which may differ from the Stage 2 canonical convention (direction_sign * move / initial_risk_distance).
   - **Proposal**: Recompute R values from candles in Stage 2; do NOT inherit from `signal_outcome`.

6. **`analytics_runner` role** currently has SELECT, INSERT, UPDATE on analytics schema. Stage 2 will need additional permissions on `dds.scanner_setup` and `dds.signal_outcome` for reconstruction queries.
   - **Proposal**: Grant SELECT on `dds.scanner_setup`, `dds.signal_outcome`, `dds.scanner_event`, `dds.paper_account` to `analytics_runner` in a separate RBAC migration.

7. **No `config.yaml` or environment config dump** is available in the repository for hashing. The config is loaded at runtime from YAML + ENV.
   - **Proposal**: Add a config snapshot capture at the START of each analytics run, serialized from the in-memory `Settings` object.

8. **Git commit SHA** is not stored in any production table. Strategy versioning requires it.
   - **Proposal**: Capture `git rev-parse HEAD` at scanner startup and store in `scanner_run` or a new `scanner_version` table. For now, use scanner_version TEXT field.

---

## 15. Build Pipeline (Proposed)

```
1. source_watermark       — record max timestamps from source tables
2. staging_extract        — extract relevant data into analytics staging area
3. config_snapshot        — capture and hash effective config
4. strategy_snapshot      — capture scanner versions and strategy params
5. lifecycle_build        — reconstruct event journal from source tables
6. trade_fact_build       — build canonical trade facts
7. setup_fact_build       — build canonical setup facts
8. horizon_metrics        — compute horizon-based metrics from candles
9. replay_metrics         — compute scenario replay metrics
10. setup_counterfactual  — compute counterfactual scenarios
11. metric_snapshot       — compute period aggregates (24h/7d/30d)
12. reconciliation        — verify source/canonical counts and totals
13. quality_gate          — run all quality rules
14. publication           — publish dataset if quality gate PASS
```

---

## 16. Acceptance Criteria Checklist (Stage 2)

| # | Criterion | Status |
|---|-----------|--------|
| 1 | One trade fully reconstructable from events + snapshots + candles | Design ready; implementation pending |
| 2 | Effective DCA source YAML/ENV captured without storing secrets | Config snapshot design ready |
| 3 | Rebuild of same version gives identical hashes + metrics | Deterministic pipeline design ready |
| 4 | PIT violations = 0 | PIT model defined |
| 5 | Duplicate grain = 0 | PK/UK constraints defined |
| 6 | Orphan keys = 0 or classified | Orphan handling in reconciliation plan |
| 7 | LONG/SHORT mirror tests | Test plan includes mirror tests |
| 8 | Intrabar ambiguity not resolved optimistically | Ambiguity model defined |
| 9 | 24h/7d/30d exact [from, to) windows | Window semantics in metric_snapshot |
| 10 | Source totals reconcile with canonical | Reconciliation plan defined |
| 11 | Dataset published only after quality gate | Publication model defined |
| 12 | Tests: 0 failed, 0 skipped | Test plan comprehensive |

---

## 17. Summary of Key Decisions

1. **Entry attempts are DERIVABLE**, not directly recorded. Accept `reason_code = 'UNKNOWN'` for historical data.
2. **DCA stop is fixed** — no STOP_MOVED events needed.
3. **No partial exits** — no PARTIAL_EXIT events needed.
4. **R is recomputed from candles** — not inherited from `signal_outcome`.
5. **Config snapshot** is captured at run start from in-memory `Settings`.
6. **Strategy snapshot** uses `scanner_version` field + optional git commit SHA.
7. **All canonical data** lives in `analytics` schema with explicit `dataset_version` and `published` flags.

---

**END OF PHASE 0 DESIGN REPORT**
**Awaiting approval before proceeding to migration implementation.**
