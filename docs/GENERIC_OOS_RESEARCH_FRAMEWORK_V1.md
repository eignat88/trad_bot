# GENERIC_OOS_RESEARCH_FRAMEWORK_V1

## Technical Design & Migration Plan

**Author:** MiMo-v2.5  
**Date:** 2026-09-28  
**Status:** DESIGN — no code deployed, no DB migration executed  
**Scope:** Unified research observation layer for all scanners

---

## Содержание

1. [Existing Reusable Components](#1-existing-reusable-components)
2. [Generic Architecture](#2-generic-architecture)
3. [DB Schema Proposal](#3-db-schema-proposal)
4. [Python Interfaces / Classes](#4-python-interfaces--classes)
5. [Runtime / Evaluator Design](#5-runtime--evaluator-design)
6. [Candidate / Reject Instrumentation Design](#6-candidate--reject-instrumentation-design)
7. [Parameter Versioning Design](#7-parameter-versioning-design)
8. [Deduplication Strategy](#8-deduplication-strategy)
9. [Migration Strategy for Existing Experiments](#9-migration-strategy-for-existing-experiments)
10. [Pilot Scanner Recommendation](#10-pilot-scanner-recommendation)
11. [Implementation Phases](#11-implementation-phases)
12. [Required Tests](#12-required-tests)
13. [Risks](#13-risks)
14. [Minimal Development Answer](#14-minimal-development-answer)

---

## 1. Existing Reusable Components

### 1.1. Код, который можно переиспользовать «as-is»

| Компонент | Файл | Что делает | Переиспользование |
|-----------|------|-----------|-------------------|
| `HORIZONS` | `shadow/evaluator.py`, `shadow/v2d_evaluator.py`, `shadow/srr_research_evaluator.py` | Константы горизонтов `[(label, minutes)]` = 15/30/60/120/240m | **Общая константа** в `app/research/constants.py` |
| `_calculate_mfe_mae_for_window()` | `shadow/evaluator.py` (generic), `shadow/srr_research_evaluator.py` (LONG-only) | MFE/MAE по свечам в окне | **Общая функция** direction-aware |
| `_is_horizon_mature()` | `shadow/v2d_evaluator.py` | Проверка зрелости горизонта | **Общая функция** |
| `FunnelCollector` + `FunnelCounters` | `app/scanners/funnel_diagnostics.py` | Потокобезопасный сбор диагностических счётчиков воронки | **Расширить** на generic counters (spec-agnostic) |
| `setup_id` fingerprint | `SetupCandidate.fingerprint` | `scanner_name\|symbol\|direction\|entry_timeframe\|signal_candle_open_time` | **Использовать как dedup key** |
| `SetupCandidate.features` (JSONB) | `app/scanners/models.py` | Универсальный feature bag | **Использовать как feature snapshot container** |
| `BybitClient.get_klines()` | `app/exchange/bybit_client.py` | Получение свечей | **Общая зависимость evaluator'а** |
| `scanner_direction_gate` | `app/scanners/direction_gate.py` | Gate policy | **Research branch отсоединяется от gate** |

### 1.2. Паттерны, которые нужно вынести в общий код

| Паттерн | Текущее состояние | Generic версия |
|---------|-------------------|----------------|
| **eligible signals query** | Дублируется в 3 evaluator'ах (v2d, srr, me_r_cl_oos) с идентичной SQL-логикой | Одна generic функция `get_eligible_research_signals(experiment_id)` |
| **outcome upsert** | Дублируется в 3 repository-классах | Один generic `upsert_research_outcome()` |
| **candle fetch for evaluation** | Дублируется в 3 evaluator'ах | Одна generic `_fetch_evaluation_candles()` |
| **incremental horizon evaluation loop** | Дублируется в 3 evaluator'ах | Один generic `ResearchEvaluator` |
| **save result enum** | 3 одинаковых enum'а (`SaveSignalStatus`, `V2DSaveStatus`, `MERLongCLoOosSaveStatus`) | Один `ResearchSaveStatus` |

### 1.3. Ключевые наблюдения по существующему коду

**Что НЕЛЬЗЯ переиспользовать:**
- `dds.shadow_signal` / `dds.shadow_signal_outcome` — не существуют на VPS (не развернуты). Таблицы `v2d_signal`, `srr_research_signal`, `me_r_long_close_location_oos_signal` тоже **не развернуты** на production DB. Существуют только миграции 041–048 в Git.

**Что теряется в текущей production pipeline (orchestrator.py):**

```
scanner.scan(ctx)
    ↓
candidates (SetupCandidate[])
    ↓
score_candidate() → scored
    ↓
dedup.filter_new() → unique  ← [1] дедуп отбрасывает дубли
    ↓
validate_risk_geometry()        ← [2] risk geometry rejects
    ↓
score >= 30                     ← [3] score threshold rejects
    ↓
gate_policy.evaluate()          ← [4] direction gate rejects ← ГЛАВНАЯ ТОЧКА ПОТЕРЬ
    ↓
expectancy_filter               ← [5] expectancy filter rejects
    ↓
regime_filter                   ← [6] regime filter rejects
    ↓
valid → save_setup() → dds.scanner_setup
```

**Точки, где candidates теряются БЕЗ записи:**
1. **dedup.filter_new()** — дубли по fingerprint отбрасываются без логирования
2. **validate_risk_geometry()** — логируется в warning, но не в БД
3. **score < 30** — молча отбрасываются
4. **direction gate BLOCKED** — логируется в info, но не в БД (кроме SHADOW_CONTROL_SCANNERS)
5. **expectancy filter** — отбрасываются без записи
6. **regime filter** — отбрасываются в debug

**Исключение:** SHADOW_CONTROL_SCANNERS и `_oos_rejected` кандидаты проходят gate. RESEARCH_CAPTURE_SCANNERS (только SRR LONG) захватываются ДО gate. Но это hard-coded whitelists — не generic mechanism.

---

## 2. Generic Architecture

### 2.1. Два независимых контура

```
┌─────────────────────────────────────────────────────────┐
│                  PRODUCTION PIPELINE                     │
│                                                          │
│  scanner.scan(ctx)                                       │
│       ↓                                                  │
│  SetupCandidate[]                                        │
│       ↓                                                  │
│  scoring → dedup → geometry → score gate → gate →       │
│  expectancy → regime                                    │
│       ↓                                                  │
│  dds.scanner_setup (status=SETUP_READY/EXPIRED/etc.)     │
│       ↓                                                  │
│  paper engine → dds.paper_trade                          │
│       ↓                                                  │
│  dds.signal_outcome                                      │
│                                                          │
│  ⚠ Production pipeline ОСТАЕТСЯ НЕИЗМЕННЫМ               │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                  RESEARCH BRANCH                         │
│                                                          │
│  candidate_detected (SetupCandidate)                     │
│       │                                                  │
│       ├──→ research.research_observation                 │
│       │    (ВСЕ candidates, включая rejected)             │
│       │                                                  │
│       │    ↓                                             │
│       │    research.research_signal                       │
│       │    (immutable snapshot: features + parameters)    │
│       │                                                  │
│       │    ↓                                             │
│       │    research.research_outcome                      │
│       │    (MFE/MAE/TP/SL at horizons)                   │
│       │                                                  │
│       │    ↓                                             │
│       │    research.research_analysis                     │
│       │    (offline parameter optimization)               │
│       │                                                  │
│       └──→ production pipeline (UNCHANGED)                │
│                                                          │
│  ⚠ Research branch — append-only, no production impact   │
└─────────────────────────────────────────────────────────┘
```

### 2.2. Точка ветвления: «сразу после сканера»

Research branch ответвляется **на самом раннем этапе** — сразу после `scanner.scan(ctx)` возвращает candidates. Это критическое отличие от текущего подхода, где research capture происходит после scoring/dedup/geometry.

```
scanner.scan(ctx) → candidate[]
       │
       ├─→ RESEARCH_BRANCH: запись в research.research_observation
       │   (ВСЕ candidates без фильтров)
       │
       └─→ PRODUCTION_BRANCH: scoring → dedup → gates → paper
```

**Почему именно здесь:**
- Мы хотим видеть ВСЕ candidates, включая те, которые отсеиваются на scoring, dedup, geometry, gate
- Research branch никогда не блокирует production
- Research branch — append-only: INSERT, без UPDATE/DELETE

### 2.3. Схема «observation → signal → outcome»

```
research_observation     ← raw candidate from scanner (any status)
       ↓
research_signal          ← frozen snapshot with parameters + features
       ↓
research_outcome         ← MFE/MAE/TP/SL evaluation at horizons
       ↓
research_analysis        ← offline parameter grid search results
```

**Observation** = «scanner что-то увидел». Записывается ВСЕГДА. Содержит rejection chain.

**Signal** = «наблюдение прошло minimum quality bar для research». Необязательно = production setup. Может включатьrejected-by-gate candidates. Содержит immutable snapshots.

**Outcome** = «что реально случилось с ценой после signal_time». Один signal → один outcome row (upsert поhorizons).

**Analysis** = offline batch job. Один analysis = одна grid search run поparameter combinations.

---

## 3. DB Schema Proposal

### 3.1. OPTION A vs B vs C — Сравнение

#### OPTION A: Расширение shadow_signal / shadow_signal_outcome

```
Плюсы:
  + Минимум нового кода
  + Используем существующий паттерн

Минусы:
  — Таблицы НЕ развернуты на VPS (только миграции в Git)
  — shadow_signal привязан к ATR_WICK (hardcoded columns)
  - Нет rejection tracking (только pass candidates)
  - Нет parameter_set_id / parameter snapshot
  - Каждый scanner = своя таблица (explosion)
  - Невозможно добавить scanner-specific columns без ALTER TABLE
  
Сложность миграции: LOW (таблиц нет на VPS — создаём с нуля)
Queryability: LOW (scanner-specific schemas, нет generic queries)
Storage growth: LOW-MEDIUM (pass-only)
```

#### OPTION B: Generic research_signal / research_outcome

```
Плюсы:
  + Одна таблица на ВСЕ scanners
  + Generic queries across scanners
  + Parameter versioning built-in
  + Rejection chain tracked
  + Features в JSONB (flexible)
  + Outcomes с горизонтами —通用

Минусы:
  — Больше начальной работы
  — JSONB features могут быть медленнее для analytical queries
  — Нужны GIN индексы для JSONB
  — Storage больше (все candidates, не только pass)

Сложность миграции: MEDIUM (одна migration, чистый дизайн)
Queryability: HIGH (одна таблица, generic SQL)
Storage growth: MEDIUM (полные данные)
```

#### OPTION C: Base generic tables + scanner-specific extension tables

```
Плюсы:
  + Generic core + scanner-specific features
  + Баланс между generic и specific

Минусы:
  — Сложность: JOIN между base и extension
  — Каждый новый scanner = новая extension table + migration
  — Extension table = ещё один Repository класс
  — Двойной INSERT (base + extension) на каждый candidate
  — Хрупко: base schema changes ломают все extensions

Сложность миграции: HIGH (base + extension per scanner)
Queryability: MEDIUM (JOINs required)
Storage growth: MEDIUM
```

### 3.2. РЕКОМЕНДАЦИЯ: OPTION B — Generic Tables

**Причина:** Мы строим **research layer**, а не production schema. Research layer должен быть:
1. **Generic** — один scanner = одна строка в общей таблице, zero migration per scanner
2. **Immutable** — feature snapshot frozen at detection time
3. **Queryable** — один SQL для сравнения любого scanner с любым
4. **Versioned** — parameter_set_id для reproducibility

JSONB для features — это **правильный выбор** для research, потому что:
- Каждый scanner имеет уникальный набор features
- Features не требуют JOIN для анализа (GIN index)
- Offline optimization читает features как JSON, не как columns

### 3.3. Схема: research

```sql
-- ============================================================
-- SCHEMA: research
-- ============================================================
CREATE SCHEMA IF NOT EXISTS research;
```

#### 3.3.1. `research.research_experiment`

Реестр экспериментов. Одна строка = один research framework.

```sql
CREATE TABLE IF NOT EXISTS research.research_experiment (
    experiment_id       TEXT PRIMARY KEY,           -- e.g. 'GENERIC_OOS_V1'
    scanner_name        TEXT NOT NULL,              -- e.g. 'VOLATILITY_COMPRESSION'
    scanner_version     TEXT NOT NULL,              -- e.g. '2.0.0'
    description         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','PAUSED','COMPLETED')),
    parameter_set_id    TEXT NOT NULL,              -- versioned parameter snapshot ID
    parameters_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,  -- frozen parameters
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE research.research_experiment
    IS 'Registry of active research experiments per scanner (migration 049)';
```

**Зачем:** При подключении нового scanner создаётся одна строка. Parameters冻结 в JSONB.

#### 3.3.2. `research.research_observation`

Сырой кандидат. Записывается **всегда** — и PASS и REJECT. Это самая большая таблица.

```sql
CREATE TABLE IF NOT EXISTS research.research_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES research.research_experiment(experiment_id),

    -- Scanner identity
    scanner_name        TEXT NOT NULL,
    scanner_version     TEXT NOT NULL,
    parameter_set_id    TEXT NOT NULL,

    -- Signal identity
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,

    -- Price levels (from scanner)
    reference_price     NUMERIC NOT NULL,
    entry_zone_low      NUMERIC,
    entry_zone_high     NUMERIC,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,

    -- Rejection chain
    status              TEXT NOT NULL DEFAULT 'DETECTED'
                        CHECK (status IN (
                            'DETECTED',           -- scanner found it
                            'SCORE_REJECTED',     -- score < threshold
                            'GEOMETRY_REJECTED',  -- invalid risk geometry
                            'DEDUP_REJECTED',     -- duplicate by fingerprint
                            'GATE_REJECTED',      -- direction gate blocked
                            'EXPECTANCY_REJECTED',-- negative expectancy
                            'REGIME_REJECTED',    -- regime filter
                            'SETUP_READY',        -- passed all production gates
                            'OOS_REJECTED'        -- OOS treatment filter
                        )),
    rejection_stage     TEXT,                       -- detailed stage name
    rejection_reason    TEXT,                       -- human-readable reason

    -- Immutable feature snapshot (JSONB — scanner-specific keys)
    features            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Immutable parameter snapshot
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Market context snapshot (at detection time)
    market_regime       TEXT,
    htf_timeframe       TEXT,
    setup_timeframe     TEXT,
    entry_timeframe     TEXT,

    -- Metadata
    setup_id            TEXT,                       -- production SetupCandidate.setup_id (UUID)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Core indexes
CREATE INDEX IF NOT EXISTS idx_ro_experiment
    ON research.research_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_ro_scanner
    ON research.research_observation (scanner_name, direction);
CREATE INDEX IF NOT EXISTS idx_ro_symbol_time
    ON research.research_observation (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_ro_status
    ON research.research_observation (status);
CREATE INDEX IF NOT EXISTS idx_ro_time
    ON research.research_observation (signal_time DESC);

-- Dedup: one observation per experiment + symbol + candle
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_observation_candle
    ON research.research_observation (experiment_id, symbol, signal_candle_open_time)
    WHERE signal_candle_open_time > 0;

-- GIN index for JSONB features queries
CREATE INDEX IF NOT EXISTS idx_ro_features
    ON research.research_observation USING GIN (features);

COMMENT ON TABLE research.research_observation
    IS 'Immutable record of every scanner candidate observation (pass + reject)';

COMMENT ON COLUMN research.research_observation.status
    IS 'Final status in the rejection chain: DETECTED → SCORE/GEOMETRY/DEDUP/GATE/EXPECTANCY/REGIME_REJECTED → SETUP_READY';
COMMENT ON COLUMN research.research_observation.features
    IS 'Frozen JSONB snapshot of all scanner-specific features at detection time';
COMMENT ON COLUMN research.research_observation.parameters
    IS 'Frozen JSONB snapshot of scanner parameters at detection time (from experiment.parameters_snapshot)';
```

**Ключевые design decisions:**
- `status` — финальный статус в rejection chain. Каждый кандидат получает **ровно одну запись**.
- `rejection_stage` — на каком этапе отброшен (dedup, geometry, score, gate, expectancy, regime)
- `rejection_reason` — детальная причина (например "STOCH_RSI_BELOW_MIN (0.15 < 0.20)")
- `features` — JSONB со всеми features сканера. **Заморожен** при detection.
- `parameters` — JSONB с параметрами сканера. **Заморожен** из `experiment.parameters_snapshot`.
- `signal_candle_open_time` — стабильный dedup key (не меняется пока свеча формируется)

#### 3.3.3. `research.research_signal`

Подмножество observations, прошедших minimum quality bar для outcome evaluation.

```sql
CREATE TABLE IF NOT EXISTS research.research_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    observation_id      BIGINT NOT NULL REFERENCES research.research_observation(observation_id),

    -- Denormalized for fast queries (copied from observation)
    experiment_id       TEXT NOT NULL,
    scanner_name        TEXT NOT NULL,
    parameter_set_id    TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,
    reference_price     NUMERIC NOT NULL,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,
    features            JSONB NOT NULL DEFAULT '{}'::jsonb,
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_regime       TEXT,

    -- Dedup
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup: one signal per experiment + symbol + candle
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_signal_candle
    ON research.research_signal (experiment_id, symbol, signal_candle_open_time)
    WHERE signal_candle_open_time > 0;

CREATE INDEX IF NOT EXISTS idx_rs_experiment ON research.research_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_rs_scanner ON research.research_signal (scanner_name, direction);
CREATE INDEX IF NOT EXISTS idx_rs_symbol ON research.research_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_rs_features ON research.research_signal USING GIN (features);

COMMENT ON TABLE research.research_signal
    IS 'Quality-filtered research signals linked to observations — basis for outcome evaluation';
```

**Зачем отдельная таблица:**
- `research_observation` — huge (millions rows), включает rejected
- `research_signal` — tiny subset (pass-only), используется evaluator'ом
- Evaluator query: `SELECT * FROM research_signal WHERE experiment_id = X AND NOT finalized`
- Это separated hot/cold storage: observations = cold archive, signals = hot working set

**Минимальный quality bar для signal:**
- `features` не пустой
- `reference_price > 0`
- `invalidation_price > 0` (есть стоп)
- `target_1 > 0` (есть таргет)
- Не дубликат

#### 3.3.4. `research.research_outcome`

```sql
CREATE TABLE IF NOT EXISTS research.research_outcome (
    signal_id           BIGINT PRIMARY KEY REFERENCES research.research_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL,
    symbol              TEXT NOT NULL,

    -- MFE/MAE by horizon (in % from entry)
    mfe_15m  NUMERIC, mae_15m  NUMERIC,
    mfe_30m  NUMERIC, mae_30m  NUMERIC,
    mfe_60m  NUMERIC, mae_60m  NUMERIC,
    mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC,

    -- MFE/MAE normalised to R (1R = abs(entry - stop))
    mfe_r_15m  NUMERIC, mae_r_15m  NUMERIC,
    mfe_r_30m  NUMERIC, mae_r_30m  NUMERIC,
    mfe_r_60m  NUMERIC, mae_r_60m  NUMERIC,
    mfe_r_120m NUMERIC, mae_r_120m NUMERIC,
    mfe_r_240m NUMERIC, mae_r_240m NUMERIC,

    -- Return at horizon (total return if exit at horizon close)
    return_at_15m  NUMERIC,
    return_at_30m  NUMERIC,
    return_at_60m  NUMERIC,
    return_at_120m NUMERIC,
    return_at_240m NUMERIC,

    -- Target/stop hit flags
    tp_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    sl_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    tp_before_sl    BOOLEAN NOT NULL DEFAULT FALSE,
    sl_before_tp    BOOLEAN NOT NULL DEFAULT FALSE,

    -- Time to TP / SL (in minutes, NULL if not hit within 240m)
    time_to_tp      NUMERIC,
    time_to_sl      NUMERIC,

    -- Per-horizon evaluation timestamps
    evaluated_15m_at  TIMESTAMPTZ,
    evaluated_30m_at  TIMESTAMPTZ,
    evaluated_60m_at  TIMESTAMPTZ,
    evaluated_120m_at TIMESTAMPTZ,
    evaluated_240m_at TIMESTAMPTZ,

    is_final        BOOLEAN NOT NULL DEFAULT FALSE,

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rou_experiment ON research.research_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_rou_symbol ON research.research_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_rou_mfe60 ON research.research_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_rou_pending ON research.research_outcome (is_final) WHERE is_final = FALSE;

COMMENT ON TABLE research.research_outcome
    IS 'Multi-horizon MFE/MAE/TP/SL evaluation for research signals';
```

#### 3.3.5. `research.research_analysis`

Результат offline parameter optimization.

```sql
CREATE TABLE IF NOT EXISTS research.research_analysis (
    analysis_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id       TEXT NOT NULL REFERENCES research.research_experiment(experiment_id),
    parameter_set_id    TEXT NOT NULL,
    parameters_snapshot JSONB NOT NULL,

    -- Time window analyzed
    period_from         TIMESTAMPTZ NOT NULL,
    period_to           TIMESTAMPTZ NOT NULL,
    signals_analyzed    BIGINT NOT NULL DEFAULT 0,

    -- Aggregate metrics
    avg_mfe_60m         NUMERIC,
    avg_mae_60m         NUMERIC,
    avg_mfe_r_60m       NUMERIC,
    avg_mae_r_60m       NUMERIC,
    win_rate_60m        NUMERIC,         -- % signals with mfe_60m > 0
    avg_return_60m      NUMERIC,
    expectancy_r        NUMERIC,         -- avg(R) per signal
    profit_factor       NUMERIC,
    max_drawdown_r      NUMERIC,

    -- Per-horizon aggregates (JSONB for flexibility)
    horizons_json       JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Feature importance (from grid search / optimization)
    feature_importance  JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Metadata
    runner_version      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ra_experiment ON research.research_analysis (experiment_id);
CREATE INDEX IF NOT EXISTS idx_ra_params ON research.research_analysis (parameter_set_id);

COMMENT ON TABLE research.research_analysis
    IS 'Offline parameter optimization results — one row per grid search run';
```

#### 3.3.6. Observability Views

```sql
-- Accumulation stats per experiment
CREATE OR REPLACE VIEW research.v_experiment_accumulation AS
SELECT
    o.experiment_id,
    o.scanner_name,
    COUNT(*)                                                        AS observations_total,
    COUNT(*) FILTER (WHERE o.status = 'SETUP_READY')                AS observations_setup_ready,
    COUNT(*) FILTER (WHERE o.status = 'DETECTED')                   AS observations_detected,
    COUNT(*) FILTER (WHERE o.status LIKE '%REJECTED')               AS observations_rejected,
    COUNT(*) FILTER (WHERE o.status = 'GATE_REJECTED')              AS observations_gate_rejected,
    COUNT(*) FILTER (WHERE o.status = 'SCORE_REJECTED')             AS observations_score_rejected,
    COUNT(*) FILTER (WHERE o.status = 'GEOMETRY_REJECTED')          AS observations_geometry_rejected,
    COUNT(*) FILTER (WHERE o.status = 'DEDUP_REJECTED')             AS observations_dedup_rejected,
    COUNT(*) FILTER (WHERE o.status = 'EXPECTANCY_REJECTED')        AS observations_expectancy_rejected,
    COUNT(*) FILTER (WHERE o.status = 'REGIME_REJECTED')            AS observations_regime_rejected,
    COUNT(s.signal_id)                                              AS signals_created,
    COUNT(r.signal_id)                                              AS outcomes_created,
    COUNT(r.signal_id) FILTER (WHERE r.is_final = TRUE)             AS outcomes_finalized,
    MIN(o.signal_time)                                              AS oldest_observation,
    MAX(o.signal_time)                                              AS newest_observation
FROM research.research_observation o
LEFT JOIN research.research_signal s ON s.observation_id = o.observation_id
LEFT JOIN research.research_outcome r ON r.signal_id = s.signal_id
GROUP BY o.experiment_id, o.scanner_name;

-- Rejection breakdown per experiment
CREATE OR REPLACE VIEW research.v_rejection_breakdown AS
SELECT
    experiment_id,
    rejection_stage,
    rejection_reason,
    COUNT(*) AS count,
    MIN(signal_time) AS first_seen,
    MAX(signal_time) AS last_seen
FROM research.research_observation
WHERE status LIKE '%REJECTED'
GROUP BY experiment_id, rejection_stage, rejection_reason
ORDER BY experiment_id, count DESC;

-- Parameter bucketing for a specific experiment
-- (template — parameters are JSONB, specific buckets defined per experiment)
CREATE OR REPLACE VIEW research.v_outcome_by_regime AS
SELECT
    s.experiment_id,
    s.scanner_name,
    s.market_regime,
    s.direction,
    COUNT(*) AS signals,
    ROUND(AVG(o.mfe_60m)::numeric, 4) AS avg_mfe_60m,
    ROUND(AVG(o.mae_60m)::numeric, 4) AS avg_mae_60m,
    ROUND(AVG(o.mfe_r_60m)::numeric, 4) AS avg_mfe_r_60m,
    ROUND(AVG(o.mae_r_60m)::numeric, 4) AS avg_mae_r_60m,
    ROUND(AVG(CASE WHEN o.mfe_60m > 0 THEN 1.0 ELSE 0.0 END)::numeric, 4) AS win_rate_60m,
    ROUND(AVG(o.return_at_60m)::numeric, 4) AS avg_return_60m
FROM research.research_signal s
JOIN research.research_outcome o ON o.signal_id = s.signal_id
WHERE o.is_final = TRUE
GROUP BY s.experiment_id, s.scanner_name, s.market_regime, s.direction
ORDER BY s.experiment_id, signals DESC;
```

---

## 4. Python Interfaces / Classes

### 4.1. Module Structure

```
app/research/
    __init__.py
    constants.py              # HORIZONS, DIRECTIONS, STATUS enums
    models.py                 # dataclasses: Observation, Signal, Outcome, Analysis
    repository.py             # ResearchRepository — generic DB layer
    observer.py               # ResearchObserver — captures candidates from orchestrator
    evaluator.py              # ResearchEvaluator — multi-horizon MFE/MAE
    signal_factory.py         # observation → signal promotion logic
    adapters/
        __init__.py
        base.py               # BaseScannerAdapter Protocol
        atr_wick.py           # ATR_WICK adapter
        volatility.py         # VOLATILITY_COMPRESSION adapter
        trend_pullback.py     # TREND_PULLBACK adapter
        breakout_retest.py    # BREAKOUT_RETEST adapter
        momentum_exhaustion_r.py  # MOMENTUM_EXHAUSTION_R adapter
        srr.py                # SUPPORT_RESISTANCE_REACTION adapter
        me_r_long.py          # ME_R_LONG adapter
```

### 4.2. Core Interfaces

#### 4.2.1. `constants.py`

```python
"""Generic research framework constants."""
from __future__ import annotations

# Evaluation horizons — shared across all experiments
HORIZONS: list[tuple[str, int]] = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]

# Observation statuses (rejection chain)
class ObservationStatus:
    DETECTED = "DETECTED"
    SCORE_REJECTED = "SCORE_REJECTED"
    GEOMETRY_REJECTED = "GEOMETRY_REJECTED"
    DEDUP_REJECTED = "DEDUP_REJECTED"
    GATE_REJECTED = "GATE_REJECTED"
    EXPECTANCY_REJECTED = "EXPECTANCY_REJECTED"
    REGIME_REJECTED = "REGIME_REJECTED"
    SETUP_READY = "SETUP_READY"
    OOS_REJECTED = "OOS_REJECTED"
```

#### 4.2.2. `models.py`

```python
"""Data models for research framework."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

@dataclass(frozen=True)
class ResearchObservation:
    """Immutable record of a scanner candidate."""
    experiment_id: str
    scanner_name: str
    scanner_version: str
    parameter_set_id: str
    symbol: str
    direction: str
    signal_time: datetime
    signal_candle_open_time: int
    reference_price: float
    entry_zone_low: float | None
    entry_zone_high: float | None
    invalidation_price: float | None
    target_1: float | None
    target_2: float | None
    score: float
    status: str
    rejection_stage: str | None
    rejection_reason: str | None
    features: dict[str, Any]
    parameters: dict[str, Any]
    market_regime: str | None
    htf_timeframe: str
    setup_timeframe: str
    entry_timeframe: str
    setup_id: str | None = None

@dataclass(frozen=True)
class ResearchSignal:
    """Quality-filtered signal for outcome evaluation."""
    signal_id: int
    observation_id: int
    experiment_id: str
    scanner_name: str
    parameter_set_id: str
    symbol: str
    direction: str
    signal_time: datetime
    signal_candle_open_time: int
    reference_price: float
    invalidation_price: float | None
    target_1: float | None
    target_2: float | None
    score: float
    features: dict[str, Any]
    parameters: dict[str, Any]
    market_regime: str | None
```

#### 4.2.3. `repository.py`

```python
"""Generic research repository — single DB layer for all experiments."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.research.models import ResearchObservation

logger = logging.getLogger(__name__)


class ResearchRepository:
    """Generic repository for research observations, signals, and outcomes."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def save_observation(self, obs: ResearchObservation) -> int | None:
        """Insert a research observation. Returns observation_id or None on duplicate/error."""
        if not self._conn:
            return None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO research.research_observation (
                    experiment_id, scanner_name, scanner_version, parameter_set_id,
                    symbol, direction, signal_time, signal_candle_open_time,
                    reference_price, entry_zone_low, entry_zone_high,
                    invalidation_price, target_1, target_2, score,
                    status, rejection_stage, rejection_reason,
                    features, parameters, market_regime,
                    htf_timeframe, setup_timeframe, entry_timeframe, setup_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (experiment_id, symbol, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO NOTHING
                RETURNING observation_id
                """,
                (
                    obs.experiment_id, obs.scanner_name, obs.scanner_version,
                    obs.parameter_set_id,
                    obs.symbol, obs.direction, obs.signal_time,
                    obs.signal_candle_open_time,
                    obs.reference_price, obs.entry_zone_low, obs.entry_zone_high,
                    obs.invalidation_price, obs.target_1, obs.target_2, obs.score,
                    obs.status, obs.rejection_stage, obs.rejection_reason,
                    json.dumps(obs.features), json.dumps(obs.parameters),
                    obs.market_regime,
                    obs.htf_timeframe, obs.setup_timeframe, obs.entry_timeframe,
                    obs.setup_id,
                ),
            )
            row = cursor.fetchone()
            self._conn.commit()
            return row[0] if row else None
        except Exception:
            self._conn.rollback()
            logger.exception("Failed to save research observation for %s", obs.symbol)
            return None

    def promote_to_signal(self, observation_id: int) -> int | None:
        """Promote an observation to a research signal. Returns signal_id."""
        if not self._conn:
            return None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO research.research_signal (
                    observation_id, experiment_id, scanner_name, parameter_set_id,
                    symbol, direction, signal_time, signal_candle_open_time,
                    reference_price, invalidation_price, target_1, target_2,
                    score, features, parameters, market_regime
                )
                SELECT
                    o.observation_id, o.experiment_id, o.scanner_name, o.parameter_set_id,
                    o.symbol, o.direction, o.signal_time, o.signal_candle_open_time,
                    o.reference_price, o.invalidation_price, o.target_1, o.target_2,
                    o.score, o.features, o.parameters, o.market_regime
                FROM research.research_observation o
                WHERE o.observation_id = %s
                  AND o.reference_price > 0
                  AND o.invalidation_price > 0
                  AND o.target_1 > 0
                ON CONFLICT (experiment_id, symbol, signal_candle_open_time)
                WHERE signal_candle_open_time > 0
                DO NOTHING
                RETURNING signal_id
                """,
                (observation_id,),
            )
            row = cursor.fetchone()
            self._conn.commit()
            return row[0] if row else None
        except Exception:
            self._conn.rollback()
            logger.exception("Failed to promote observation %d to signal", observation_id)
            return None

    def get_eligible_signals(self, experiment_id: str, limit: int = 5000) -> list[dict]:
        """Get signals needing at least one horizon evaluation."""
        # ... (same pattern as existing srr/v2d/me_r evaluators, but generic)
        ...

    def upsert_outcome(self, signal_id: int, experiment_id: str, symbol: str,
                       updates: dict[str, Any]) -> bool:
        """Generic outcome upsert with dynamic columns."""
        # ... (same pattern as existing _save_outcome_partial, but generic)
        ...
```

#### 4.2.4. `observer.py`

```python
"""Research Observer — captures candidates from scanner pipeline.

Positioned IMMEDIATELY after scanner.scan(ctx), BEFORE scoring/dedup/gates.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any

from app.research.models import ResearchObservation
from app.research.repository import ResearchRepository
from app.scanners.models import SetupCandidate

logger = logging.getLogger(__name__)


class ResearchObserver:
    """Captures ALL scanner candidates for research, regardless of production fate."""

    def __init__(self, repository: ResearchRepository, experiments: dict[str, dict]) -> None:
        self._repo = repository
        self._experiments = experiments  # {scanner_name: {experiment_id, parameter_set_id, parameters, ...}}
        self._stats: dict[str, int] = {}

    def observe(self, candidate: SetupCandidate) -> None:
        """Record a candidate as a research observation.

        Called for EVERY candidate from scanner.scan(ctx), BEFORE any filtering.
        """
        exp = self._experiments.get(candidate.scanner_name)
        if exp is None:
            return  # scanner not registered for research

        obs = ResearchObservation(
            experiment_id=exp["experiment_id"],
            scanner_name=candidate.scanner_name,
            scanner_version=candidate.scanner_version,
            parameter_set_id=exp["parameter_set_id"],
            symbol=candidate.symbol,
            direction=candidate.direction,
            signal_time=candidate.detected_at,
            signal_candle_open_time=candidate.signal_candle_open_time,
            reference_price=candidate.reference_price,
            entry_zone_low=candidate.entry_zone_low,
            entry_zone_high=candidate.entry_zone_high,
            invalidation_price=candidate.invalidation_price,
            target_1=candidate.target_1,
            target_2=candidate.target_2,
            score=candidate.score,
            status="DETECTED",
            rejection_stage=None,
            rejection_reason=None,
            features=candidate.features,
            parameters=exp["parameters"],
            market_regime=candidate.market_regime,
            htf_timeframe=candidate.htf_timeframe,
            setup_timeframe=candidate.setup_timeframe,
            entry_timeframe=candidate.entry_timeframe,
            setup_id=str(candidate.setup_id),
        )

        obs_id = self._repo.save_observation(obs)
        if obs_id:
            self._stats["inserted"] = self._stats.get("inserted", 0) + 1
        else:
            self._stats["duplicates"] = self._stats.get("duplicates", 0) + 1
```

#### 4.2.5. `adapters/base.py`

```python
"""Base adapter protocol for scanner-specific configuration."""
from __future__ import annotations
from typing import Any, Protocol


class ScannerResearchAdapter(Protocol):
    """Protocol that each scanner adapter must implement."""

    @property
    def scanner_name(self) -> str: ...

    @property
    def experiment_id(self) -> str: ...

    @property
    def parameter_set_id(self) -> str: ...

    def get_frozen_parameters(self) -> dict[str, Any]:
        """Return the frozen parameter snapshot for this experiment."""
        ...

    def get_research_timeframes(self) -> tuple[str, str, str]:
        """Return (htf_timeframe, setup_timeframe, entry_timeframe)."""
        ...

    def should_create_signal(self, features: dict[str, Any]) -> bool:
        """Minimum quality bar for promoting observation to signal.
        
        Default: reference_price > 0, invalidation_price > 0, target_1 > 0.
        Override for scanner-specific rules.
        """
        ...
```

Example adapter:

```python
# app/research/adapters/volatility.py
class VolatilityCompressionAdapter:
    scanner_name = "VOLATILITY_COMPRESSION"
    experiment_id = "VOLCOMP_GENERIC_V1"
    parameter_set_id = "vc_2.0.0_default"

    def get_frozen_parameters(self) -> dict[str, Any]:
        return {
            "atr_period": 14,
            "squeeze_lookback": 20,
            "bb_squeeze_threshold": 0.02,
            # frozen — do not change after deployment
        }

    def get_research_timeframes(self) -> tuple[str, str, str]:
        return ("1h", "15m", "5m")

    def should_create_signal(self, features: dict[str, Any]) -> bool:
        return (
            features.get("squeeze_detected", False)
            and features.get("volume_spike", False)
        )
```

---

## 5. Runtime / Evaluator Design

### 5.1. Integration Point in Orchestrator

```python
# В scan_all_with_stats(), СРАЗУ после scanner.scan(ctx):

for name, scanner in self.scanners.items():
    candidates = scanner.scan(ctx)
    
    # >>> RESEARCH BRANCH — captures ALL candidates <<<
    for candidate in candidates:
        self.research_observer.observe(candidate)
    
    all_candidates.extend(candidates)

# ... existing production pipeline unchanged ...
```

**Это единственное изменение в production code: 3 строки.** Всё остальное — new code.

### 5.2. Evaluator Architecture

```
ResearchEvaluator (generic, one instance per experiment)
    │
    ├── get_eligible_signals(experiment_id)
    │   → SQL: SELECT from research_signal WHERE NOT finalized
    │
    ├── evaluate_signal_batch(signals, candles, now)
    │   for each signal:
    │     for each horizon:
    │       if horizon_mature and not yet_evaluated:
    │         calculate MFE/MAE/return
    │         calculate TP/SL hit + sequence
    │         calculate time_to_TP / time_to_SL
    │
    ├── upsert_outcome(signal_id, updates)
    │   → Generic dynamic-column upsert
    │
    └── finalize(signal_id, target_flags)
        → Mark is_final = TRUE
```

### 5.3. Evaluator as Background Service

```python
# research/evaluator_runner.py

class ResearchEvaluatorRunner:
    """Background runner for all research evaluators."""
    
    def __init__(self, conn, client: BybitClient):
        self._conn = conn
        self._client = client
        self._evaluators: dict[str, ResearchEvaluator] = {}
    
    def register_experiment(self, experiment_id: str) -> None:
        self._evaluators[experiment_id] = ResearchEvaluator(
            conn=self._conn,
            client=self._client,
            experiment_id=experiment_id,
        )
    
    def run_cycle(self) -> dict[str, Any]:
        """Evaluate all registered experiments."""
        stats = {}
        for exp_id, evaluator in self._evaluators.items():
            stats[exp_id] = evaluator.run_evaluation_cycle()
        return stats
```

---

## 6. Candidate / Reject Instrumentation Design

### 6.1. Where Candidates Get Lost (Current State)

```
orchestrator.scan_all_with_stats():

scanner.scan(ctx) → candidates[]
    │
    ├──[1] scoring (score_candidate)  → ALL scored, none lost
    │
    ├──[2] dedup.filter_new()         → DEDUP_REJECTED (not logged to DB!)
    │
    ├──[3] validate_risk_geometry()   → GEOMETRY_REJECTED (logged to WARNING only)
    │
    ├──[4] score >= 30                → SCORE_REJECTED (not logged to DB!)
    │
    ├──[5] direction gate             → GATE_REJECTED (logged to INFO only)
    │
    ├──[6] expectancy filter          → EXPECTANCY_REJECTED (not logged to DB!)
    │
    └──[7] regime filter              → REGIME_REJECTED (logged to DEBUG only)
```

### 6.2. New Design: Research Branch Captures BEFORE All Filters

```
scanner.scan(ctx) → candidates[]
    │
    ├── RESEARCH_BRANCH: for each candidate:
    │       research_observation.observe(candidate)
    │       → status = "DETECTED"
    │       → observation saved to research.research_observation
    │
    └── PRODUCTION_BRANCH (unchanged):
            scoring → dedup → geometry → score gate → direction gate
            → expectancy → regime → save_setup

After production pipeline completes:
    UPDATE research.research_observation
    SET status = <final_production_status>,
        rejection_stage = <stage>,
        rejection_reason = <reason>
    WHERE setup_id = <candidate.setup_id>
      AND status = 'DETECTED'
```

### 6.3. Two-Phase Status Update

**Phase 1 — Detection (immediate):**
```python
# In orchestrator, right after scanner.scan():
for candidate in candidates:
    research_observer.observe(candidate)  # saves with status="DETECTED"
```

**Phase 2 — Resolution (after production pipeline):**
```python
# After the full production pipeline for each candidate:
for candidate in all_candidates:
    if candidate passed all gates:
        research_repo.update_observation_status(
            setup_id=candidate.setup_id,
            status="SETUP_READY",
        )
    elif candidate was dedup-rejected:
        research_repo.update_observation_status(
            setup_id=candidate.setup_id,
            status="DEDUP_REJECTED",
            rejection_stage="deduplication",
            rejection_reason="duplicate fingerprint",
        )
    # ... etc for each rejection point
```

### 6.4. Alternative: Track at Orchestrator Level

Более простой подход — трекать **pass/fate каждого candidate** в orchestrator и обновлять observation после:

```python
# В scan_all_with_stats(), в конце:
for candidate in scored:
    if not candidate.features.get("_research_captured"):
        continue
    
    # Determine final fate
    if candidate in [c for c in valid if not c.features.get("_shadow_control")]:
        final_status = "SETUP_READY"
    elif candidate.features.get("_oos_rejected"):
        final_status = "OOS_REJECTED"
    else:
        # Determine exact rejection point
        ...
    
    research_repo.update_observation_status(
        setup_id=str(candidate.setup_id),
        status=final_status,
    )
```

**Рекомендация:** Phase 1 + Phase 2 подход. Phase 1 = observe immediately (append-only). Phase 2 = resolve status after production pipeline. Это сохраняет append-only nature research branch.

---

## 7. Parameter Versioning Design

### 7.1. Проблема

Сейчас parameters читаются из `config.yaml` в runtime. Если scanner params изменились, historical signals теряют контекст — мы не знаем, с какими параметрами был сгенерирован сигнал.

### 7.2. Решение: Immutable Parameter Snapshot

```python
# При создании experiment registration:
experiment = {
    "experiment_id": "VOLCOMP_GENERIC_V1",
    "scanner_name": "VOLATILITY_COMPRESSION",
    "scanner_version": "2.0.0",
    "parameter_set_id": "vc_2.0.0_20260928",  # versioned
    "parameters": {
        "atr_period": 14,
        "squeeze_lookback": 20,
        "bb_squeeze_threshold": 0.02,
        "swing_lookback": 5,
        "breakout_margin": 0.001,
        "retest_margin": 0.003,
    },
}
```

**Ключевые правила:**
1. `parameter_set_id` = `"{scanner_prefix}_{version}_{date}"` — уникальный, монотонный
2. `parameters` = JSONB snapshot, **заморожен** при создании experiment row
3. Каждое research_observation хранит `parameter_set_id` + `parameters` (denormalized)
4. При изменении параметров scanner → создать **новый** experiment row с новым `parameter_set_id`
5. Старый experiment → `status = 'COMPLETED'`, новый → `status = 'ACTIVE'`
6. Evaluator обрабатывает observations **только ACTIVE experiments** (или指定ённых)

### 7.3. Version Flow

```
config.yaml: atr_period: 14 → parameter_set_id: "vc_2.0.0_20260928"
    ↓
INSERT INTO research.research_experiment (parameter_set_id, parameters_snapshot)
    ↓
research_observation.parameters = {"atr_period": 14, ...}  (frozen copy)
    ↓
[config.yaml changed: atr_period: 12]
    ↓
INSERT INTO research.research_experiment (parameter_set_id: "vc_2.0.1_20261001", ...)
    ↓
New observations get new parameter_set_id, old observations keep old one
```

---

## 8. Deduplication Strategy

### 8.1. Текущая проблема

> ATR генерирует ~12,500 signals/day. После подключения 4+ scanners получим миллионы одинаковых строк.

### 8.2. Multi-Level Dedup

**Level 1: Signal Candle Dedup (database-level)**
```sql
UNIQUE INDEX: (experiment_id, symbol, signal_candle_open_time)
```
Это **уже реализовано** в существующих экспериментах (srr, v2d, me_r). На同一 свече = одна запись.

**Level 2: Candidate Dedup (application-level)**
Используем `SetupCandidate.fingerprint` = `scanner_name|symbol|direction|entry_timeframe|signal_candle_open_time`.

Это **тот же ключ**, что и Level 1, но на application layer.

**Level 3: Time-Based Observation Throttling**
```python
# В observer: не записывать observation, если для same (experiment, symbol)
# уже есть observation за последние N минут
MIN_OBSERVATION_INTERVAL = {
    "5m": 30,    # 30 минут для 5m scanners
    "15m": 60,   # 60 минут для 15m scanners
    "1h": 240,   # 4 часа для 1h scanners
}
```

**Level 4: Feature-Based Deduplication (advanced, Phase 2+)**
```python
# Hash features: if feature_hash same as last observation for (experiment, symbol)
# within lookback window → skip
import hashlib, json
feature_hash = hashlib.md5(json.dumps(features, sort_keys=True).encode()).hexdigest()
```

### 8.3. Estimate Storage

| Scanner | Raw candidates/day | After L1+L2 dedup | With L3 throttle |
|---------|-------------------|-------------------|------------------|
| ATR_WICK (SHORT) | ~12,500 | ~12,500 | ~1,000–2,000 |
| VOLATILITY_COMPRESSION | ~200 | ~200 | ~50–100 |
| MOMENTUM_EXHAUSTION_R | ~150 | ~150 | ~30–60 |
| BREAKOUT_RETEST | ~100 | ~100 | ~20–40 |
| TREND_PULLBACK | ~80 | ~80 | ~20–40 |
| **Total/day** | **~13,000** | **~13,000** | **~1,100–2,200** |
| **Total/month** | | | **~33,000–66,000** |

Без throttling: ~400K rows/month. С throttling: ~50K rows/month.

**Рекомендация:** Level 1+2 mandatory, Level 3 recommended (с config-urable interval), Level 4 optional.

---

## 9. Migration Strategy for Existing Experiments

### 9.1. Current Experiments Inventory

| Experiment ID | Type | Source Scanner | Tables (NOT on VPS) | Status |
|--------------|------|---------------|---------------------|--------|
| ATR_WICK_REJECTION_SHORT_V1 | Shadow | ATR_WICK | shadow_signal, shadow_signal_outcome | NOT deployed |
| ATR_WICK_FILTER_OOS_V2_D | OOS | ATR_WICK + StochRSI filter | v2d_signal, v2d_outcome | NOT deployed |
| SRR_LONG_OUTCOME_V1 | Research | SUPPORT_RESISTANCE_REACTION | srr_research_signal, srr_research_outcome | NOT deployed |
| ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 | OOS | MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 | me_r_long_close_location_oos_signal, me_r_long_close_location_oos_outcome | NOT deployed |

### 9.2. Migration Plan

**Phase 1: Create generic tables (migration 049)**
- `research.research_experiment`
- `research.research_observation`
- `research.research_signal`
- `research.research_outcome`
- `research.research_analysis`
- Views: `v_experiment_accumulation`, `v_rejection_breakdown`, `v_outcome_by_regime`

**Phase 2: Register existing experiments**
```sql
INSERT INTO research.research_experiment VALUES
('ATR_WICK_V1', 'ATR_WICK_REJECTION_SHORT_V1', '1.0.0', '...', 'PAUSED',
 'atr_wick_1.0.0_default', '{"wick_atr_threshold": 1.5, ...}', ...),
('ATR_WICK_V2D', 'ATR_WICK_REJECTION_SHORT_V1', '1.0.0', '...', 'PAUSED',
 'atr_wick_v2d_stochrsi', '{"stoch_rsi_min": 0.20, "stoch_rsi_max": 0.60, ...}', ...),
('SRR_LONG_V1', 'SUPPORT_RESISTANCE_REACTION', '2.0.0', '...', 'PAUSED',
 'srr_2.0.0_default', '{"level_touch_count": 3, ...}', ...),
('ME_R_CL_V1', 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', '1.0.0', '...', 'PAUSED',
 'me_r_cl_1.0.0_threshold_070', '{"close_location_threshold": 0.70, ...}', ...);
```

**Phase 3: Adapter implementation (one per existing experiment)**
Each adapter wraps the existing scanner-specific logic and maps to the generic framework.

**Phase 4: Forward-only collection**
Start collecting via generic framework. Do NOT backfill historical data from old experiments.

**Phase 5: Retire old tables**
After confirming generic framework works for 2+ weeks:
- Rename old tables to `_legacy_*`
- Update evaluator to use generic tables
- Eventually DROP legacy tables

### 9.3. Migration NOT Blocker

**Важно:** Ни одна существующая таблица НЕ удаляется и НЕ изменяется. Generic tables — это **новый schema `research`**. Существующие таблицы (`dds.scanner_setup`, `dds.signal_outcome`) продолжают работать.

---

## 10. Pilot Scanner Recommendation

### 10.1. Кандидаты

| Scanner | Candidates (all time) | Setups (all time) | Candidates → Setups Ratio | Notes |
|---------|----------------------|-------------------|--------------------------|-------|
| VOLATILITY_COMPRESSION | 235 | 3 | 1.3% | Good feature set, both LONG+SHORT |
| MOMENTUM_EXHAUSTION_R | 65 | 0 | 0% | ALL expired, zero executions |
| BREAKOUT_RETEST | 185 | 16 | 8.6% | Mixed, good retest features |
| TREND_PULLBACK | 439 | 1 | 0.2% | Highest volume, but low execution |
| SUPPORT_RESISTANCE_REACTION | 131 | 2 | 1.5% | Already has SRR research framework |

### 10.2. Критерии выбора

1. **Candidates > 0, Setups ≈ 0** — чтобы проверить research rejected candidates
2. **Rich features** — scanner должен генерировать достаточно features для offline optimization
3. **Simple parameters** — параметры должны быть versionable и frozen
4. **Direction coverage** — ideally LONG+SHORT
5. **No existing research framework** — чтобы не конфликтовать

### 10.3. РЕКОМЕНДАЦИЯ: MOMENTUM_EXHAUSTION_R

**Причины:**
1. **65 candidates, 0 executions (100% expired)** — идеально для проверки rejected candidates research
2. **Rich features:** `exhaustion_magnitude`, `body_ratio`, `rsi_confirmation`, `volume_ratio`, `rr_ratio`, `stop_distance_atr` — 6 numeric features для parameter optimization
3. **Both LONG and SHORT directions** — 65 setups (all LONG), но scanner генерирует оба direction
4. **Clean parameters:** `swing_lookback=5`, `exhaustion_threshold=0.003` — simple, versionable
5. **No existing OOS framework** — clean start
6. **Small volume** — ~65 setups/day, manageable for pilot

**Second choice:** VOLATILITY_COMPRESSION (235 candidates, 3 executions, squeeze + expansion features)

---

## 11. Implementation Phases

### Phase 0: Design Approval (current)
- [ ] Technical design review
- [ ] Schema review
- [ ] Architecture sign-off
- [ ] Pilot scanner confirmed

### Phase 1: Database Schema (1-2 hours)
- [ ] Create `research` schema
- [ ] Create `research.research_experiment` table
- [ ] Create `research.research_observation` table
- [ ] Create `research.research_signal` table
- [ ] Create `research.research_outcome` table
- [ ] Create `research.research_analysis` table
- [ ] Create observability views
- [ ] Migration: `049_generic_research_framework.sql`
- [ ] Verify on VPS

### Phase 2: Core Python Framework (4-6 hours)
- [ ] `app/research/constants.py`
- [ ] `app/research/models.py`
- [ ] `app/research/repository.py`
- [ ] `app/research/observer.py`
- [ ] `app/research/signal_factory.py`
- [ ] `app/research/evaluator.py` (generic multi-horizon)
- [ ] Unit tests for all core modules

### Phase 3: Pilot Scanner Adapter (2-3 hours)
- [ ] `app/research/adapters/base.py`
- [ ] `app/research/adapters/momentum_exhaustion_r.py`
- [ ] Register MOMENTUM_EXHAUSTION_R in `research.research_experiment`
- [ ] Integration test with real scanner

### Phase 4: Orchestrator Integration (1-2 hours)
- [ ] Add `research_observer.observe()` call in `orchestrator.scan_all_with_stats()`
- [ ] Add status resolution after production pipeline
- [ ] Verify zero production impact

### Phase 5: Evaluator Service (2-3 hours)
- [ ] `app/research/evaluator_runner.py`
- [ ] Background evaluator loop (300s interval)
- [ ] Verify MFE/MAE/TP/SL calculations
- [ ] Verify is_final logic

### Phase 6: Deduplication & Throttling (1-2 hours)
- [ ] Level 1+2: database unique indexes (already in schema)
- [ ] Level 3: time-based throttling in observer
- [ ] Verify storage growth rates

### Phase 7: Observability (1-2 hours)
- [ ] Verify Grafana views work with new schema
- [ ] Create Grafana dashboard for research metrics
- [ ] Alert on evaluation backlog

### Phase 8: Extension to Other Scanners (1-2 hours each)
- [ ] VOLATILITY_COMPRESSION adapter
- [ ] BREAKOUT_RETEST adapter
- [ ] TREND_PULLBACK adapter
- [ ] ATR_WICK adapter
- [ ] SRR adapter
- [ ] ME_R_LONG adapter

**Total estimated time: 15-25 hours (spread across 3-5 sessions)**

---

## 12. Required Tests

### 12.1. Unit Tests

| Test | What it verifies |
|------|-----------------|
| `test_research_repository_save_observation()` | INSERT + dedup |
| `test_research_repository_promote_to_signal()` | observation → signal promotion |
| `test_research_repository_upsert_outcome()` | Dynamic column upsert |
| `test_research_observer_observe()` | Observer creates ResearchObservation from SetupCandidate |
| `test_research_observer_experiment_filter()` | Only registered scanners observed |
| `test_research_evaluator_long_mfe_mae()` | LONG MFE/MAE calculation |
| `test_research_evaluator_short_mfe_mae()` | SHORT MFE/MAE calculation |
| `test_research_evaluator_horizon_maturity()` | Horizon maturity check |
| `test_research_evaluator_tp_sl_sequence()` | TP-before-SL / SL-before-TP logic |
| `test_research_evaluator_time_to_tp()` | Time to TP calculation |
| `test_research_evaluator_is_final()` | Finalization after all horizons |
| `test_research_adapter_base()` | Protocol conformance |

### 12.2. Integration Tests

| Test | What it verifies |
|------|-----------------|
| `test_orchestrator_research_capture()` | Research capture happens before production filtering |
| `test_orchestrator_no_production_impact()` | Production pipeline unchanged |
| `test_full_cycle_observe_evaluate()` | End-to-end: scanner → observe → signal → outcome |

### 12.3. Migration Tests

| Test | What it verifies |
|------|-----------------|
| `test_migration_049_idempotent()` | Migration can be re-run |
| `test_migration_049_schema_created()` | All tables/views exist |
| `test_migration_049_indexes()` | All indexes created |

---

## 13. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Production performance degradation** | LOW | HIGH | Research observer is append-only INSERT, happens in same transaction as existing scan. Measure scan latency before/after. |
| **Storage growth** | MEDIUM | MEDIUM | Dedup + throttling. Monitor table sizes weekly. Set retention policy. |
| **Evaluator backlog** | MEDIUM | LOW | Evaluator runs every 5min, processes 5000 signals/cycle. For 10K signals/day, backlog clears in ~2 cycles. |
| **Feature snapshot staleness** | LOW | LOW | Features frozen at detection time. Parameters versioned. No UPDATE on features. |
| **Experiment config drift** | LOW | HIGH | `parameter_set_id` + `parameters_snapshot` make every observation reproducible. |
| **Migration failure on VPS** | LOW | HIGH | Migration is idempotent (CREATE IF NOT EXISTS). Test on local PostgreSQL first. |
| **Research queries slow on large tables** | MEDIUM | MEDIUM | GIN indexes on features JSONB. Consider partitioning by month if >1M rows. |
| **Old experiment code breaks** | LOW | LOW | Old experiment tables are untouched. New framework is additive. |

---

## 14. Minimal Development Answer

### Какая минимальная разработка нужна, чтобы:

> «после подключения любого scanner мы могли автоматически накапливать его candidates + features + future outcomes и затем искать лучшие комбинации параметров offline»

### Ответ: 5 компонентов

```
1. SCHEMA (1 migration file)
   research.research_experiment    — registry
   research.research_observation   — raw candidates
   research.research_signal        — quality-filtered signals
   research.research_outcome       — MFE/MAE/TP/SL evaluations
   research.research_analysis      — offline optimization results

2. REPOSITORY (1 Python file)
   ResearchRepository
   - save_observation(obs) → int
   - promote_to_signal(obs_id) → int
   - get_eligible_signals(experiment_id) → list[dict]
   - upsert_outcome(signal_id, updates) → bool

3. OBSERVER (1 Python file, ~50 lines)
   ResearchObserver
   - observe(candidate: SetupCandidate) → None
   - Called once per candidate in orchestrator (3 lines of integration)

4. EVALUATOR (1 Python file, ~200 lines)
   ResearchEvaluator
   - run_evaluation_cycle() → dict
   - Generic MFE/MAE/TP/SL calculation (reuses existing patterns)
   - Background loop, 300s interval

5. SCANNER ADAPTER (1 Protocol + 1 implementation per scanner)
   ScannerResearchAdapter protocol
   - experiment_id, parameter_set_id, parameters
   - get_research_timeframes()
   - should_create_signal(features)
```

### Точка входа: 3 строки в orchestrator.py

```python
# В scan_all_with_stats(), сразу после scanner.scan(ctx):
for candidate in candidates:
    self.research_observer.observe(candidate)  # ← NEW LINE 1
```

```python
# В конце scan_all_with_stats(), после production pipeline:
self.research_observer.resolve_statuses(candidates, valid)  # ← NEW LINE 2
```

```python
# В scanner_runner.py (или где запускается周期):
research_evaluator_runner.run_cycle()  # ← NEW LINE 3 (background service)
```

### Итого:

| Компонент | Файлов | Строк кода | Production impact |
|-----------|--------|-----------|-------------------|
| DB schema | 1 SQL | ~150 | None (new schema) |
| Repository | 1 Python | ~200 | None (new module) |
| Observer | 1 Python | ~80 | None (new module) |
| Evaluator | 1 Python | ~250 | None (new service) |
| Adapter Protocol | 1 Python | ~30 | None (new module) |
| Pilot Adapter | 1 Python | ~50 | None (new module) |
| Orchestrator change | 1 Python | **3 lines** | Minimal (append-only INSERT) |
| **Total** | **7 files** | **~760 lines** | **3 lines in production code** |

**После этого:**
- Подключаешь scanner → создаёшь adapter (30 строк)
- Scanner автоматически копит candidates + features
- Evaluator автоматически считает outcomes
- Offline: SQL queries по `research.research_outcome` + `research.research_signal` + `features` JSONB → parameter optimization
