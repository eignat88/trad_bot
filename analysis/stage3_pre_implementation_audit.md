# STAGE 3 PRE-IMPLEMENTATION AUDIT

**Date:** 2026-09-16
**Branch:** main
**HEAD:** e17ae2f (Merge pull request #61)

---

## Current branch

```
main @ e17ae2f
```

## Stage 1 objects

| Object | Migration | Status |
|--------|-----------|--------|
| `analytics.analysis_run` | 008 | ✅ Implemented, used by pipeline |
| `analytics.analysis_stage_run` | 008 | ✅ Implemented, used by pipeline |
| `analytics.data_quality_result` | 008 + 027 | ✅ Implemented, idempotent upsert |
| `market.candle` | 009 | ✅ Implemented, populated by scanner |
| `dds.*` (paper_trade, scanner_setup, instrument, etc.) | 008+ | ✅ Populated by scanner/paper |

## Stage 2 dataset publication

| Object | Migration | Status |
|--------|-----------|--------|
| `analytics.config_snapshot` | 014 | ⚠️ Table exists, function exists, never called from Python |
| `analytics.strategy_snapshot` | 014 | ⚠️ Table exists, function exists, never called from Python |
| `analytics.setup_fact` | 014 | ⚠️ Table exists, build function exists, never called from Python |
| `analytics.entry_attempt_fact` | 014 | ⚠️ Table exists, build logic exists, never called from Python |
| `analytics.trade_fact` | 014 | ⚠️ Table exists, build function exists, never called from Python |
| `analytics.trade_event` | 014 | ⚠️ Table exists, append-only with trigger, never called from Python |
| `analytics.trade_horizon_metric` | 014 | ⚠️ Table exists, function exists, never called from Python |
| `analytics.trade_replay_metric` | 014 | ⚠️ Table exists, function exists, never called from Python |
| `analytics.setup_counterfactual` | 014 | ⚠️ Table exists, never populated |
| `analytics.metric_snapshot` | 014 | ⚠️ Table exists, function exists, never called from Python |
| `analytics.metric_registry` | 014-015 | ✅ 20 metrics seeded |

## dataset_version source

**NOT IMPLEMENTED.** Both `setup_fact` and `trade_fact` have `dataset_version TEXT` and `published BOOLEAN` columns (migration 014), but:
- Build functions (migrations 017, 018) hardcode `'00000000.0'` as dataset_version
- `published` is hardcoded to `FALSE`
- No Python code computes or assigns a real dataset_version
- No versioning algorithm exists

**Decision for PR1:** Implement dataset_version as a deterministic hash of (analysis_run_id, maturity, pipeline_version). Assignment happens at dataset publication time.

## READY semantics

**NOT DEFINED.** The analytics pipeline has no formal "READY" state. Current pipeline:
1. `candle_reconciliation` → `quality_gate` → `retention` (PROVISIONAL)
2. `post_exit_backfill` → `final_quality_gate` (FINAL)

"READY" will be defined as: `analysis_run.status = SUCCEEDED AND quality_gate = PASS (no BLOCKING failures)`.

## maturity source

Stored in `analytics.analysis_run.maturity` column. CHECK constraint: `PROVISIONAL | FINAL | PARTIAL | FAILED`. Set by runner after successful stage execution.

## quality source

**Two layers, only one active:**
- **Layer 1 (Python, 8 checks):** Called by `DataQualityGate.run_quality_checks()` in `quality.py`. Checks candle-layer quality. BLOCKING failures → pipeline FAIL.
- **Layer 2 (SQL, 10 checks):** Defined in migration 023 (`analytics.check_*` functions). **NEVER called from Python.** Checks canonical data quality (trade grain, orphans, PIT violations, etc.).

**Decision:** PR1 does not change quality gate. PR2 (orchestration) will invoke Layer 2 quality checks as part of Data Readiness Gate.

## published analytics views

**None for Stage 3 consumption.** Existing views are monitoring/health:
- `analytics.analysis_run_summary` — run listing
- `analytics.stage_performance` — stage detail
- `analytics.quality_gate_summary` — quality results
- `mart.*` — Grafana dashboards (scanner, paper, health)

**Decision:** PR2 will create `analytics.agent_ready_dataset` view as the Data Readiness Gate source.

## existing migration latest

**027** (`027_quality_result_idempotency.sql`)

**Stage 3 next migration: 028**

## existing test count

**14 analytics test files**, ~250+ individual tests across:
- `test_analytics_models.py` (21)
- `test_analytics_repository.py` + `test_analytics_repository_pg8000.py`
- `test_analytics_quality_production.py`
- `test_canonical_analytics.py` (25)
- `test_canonical_analytics_db.py` (large integration)
- `test_data_quality.py` (24)
- `test_analytics_migrations.py`
- `test_analytics_health_mart.py`
- `test_analytics_rbac.py`
- `test_analytics_permissions.py`
- `test_analytics_retention.py`
- `test_analytics_systemd.py`
- `test_analytics_cli_env.py`

## architecture conflicts

1. **No `app/analytics/agents/` directory exists** — clean slate.
2. **SQL functions exist but are never called** — PR2 must wire them into the runner.
3. **dataset_version is stubbed** — must be implemented before agents can reference it.
4. **No Data Readiness Gate** — must be implemented as a programmatic stage (not LLM).
5. **`analytics_runner` DB role (migration 010)** already has SELECT on analytics schema and INSERT on analytics tables. PR5 will tighten to specific tables.
6. **`analytics_config_rbac_boundary` (migration 026)** already denies config updates to analytics_runner. Good foundation.
7. **Logging pattern is consistent** — `logging.getLogger(__name__)` everywhere. Stage 3 should follow same.
8. **Repository pattern is consistent** — single `AnalyticsRepository` class. Stage 3 agents should receive it via DI.
9. **Settings are centralized** in `app/config/settings.py` with env-based loading. Agent config should follow same pattern.
10. **CLI entry point** (`app/analytics/cli.py`) uses argparse. Agent orchestration should be callable from same CLI.

---

## PR1 Implementation Plan: `feat/stage3-agent-foundation`

### Scope

Create the foundational layer for Stage 3 without LLM calls or orchestration:
- DB schema (5 new tables)
- Python models (5 new dataclasses)
- Repository methods (CRUD for new tables)
- JSON Schema contracts (4 schemas)
- Confidence policy (versioned)
- Evidence catalog format
- Migration 028
- Comprehensive tests

### Files to Create/Modify

**New files:**
```
sql/migrations/028_agent_foundation.sql          — 5 tables + constraints
app/analytics/agents/__init__.py                  — package
app/analytics/agents/models.py                    — AgentDefinition, AgentRun, AgentInputManifest, AgentResult, DailyTradingReport dataclasses
app/analytics/agents/contracts/__init__.py
app/analytics/agents/contracts/v1/
    agent_input.schema.json                       — Input manifest schema
    specialist_output.schema.json                 — Specialist output schema
    chief_input.schema.json                       — Chief input schema
    chief_output.schema.json                      — Chief output schema
app/analytics/agents/policies/__init__.py
app/analytics/agents/policies/confidence_v1.py    — Versioned confidence policy
app/analytics/agents/evidence.py                  — Evidence catalog builder and validator
app/analytics/agents/contracts/schema_validator.py — JSON Schema validation
tests/test_agent_foundation.py                    — DB migration, models, constraints, immutability
tests/test_agent_contracts.py                     — Schema validation tests
tests/test_agent_confidence.py                    — Confidence policy tests
tests/test_agent_evidence.py                      — Evidence catalog tests
```

**Modified files:**
```
app/analytics/repository.py                       — Add agent CRUD methods
app/analytics/runner.py                           — Add agent foundation stage (stub)
app/analytics/cli.py                              — Add agent status command
```

### Tables to Create (Migration 028)

1. `analytics.agent_definition` — Registry of known agents
2. `analytics.agent_run` — Individual agent execution tracking
3. `analytics.agent_input_manifest` — Immutable input snapshot
4. `analytics.agent_result` — Immutable result storage
5. `analytics.daily_trading_report` — Daily analytical report

### Key Decisions

1. **dataset_version format:** `{YYYYMMDD}.{maturity_suffix}.{short_hash}` — deterministic, traceable
2. **Evidence ID format:** `{type}:{scope}:{metric_or_entity}:{window}` — e.g. `metric:scanner:ME:SHORT:24h:pnl_r`
3. **Hash algorithm:** SHA-256 for input_hash and result_hash (deterministic, standard)
4. **Immutability enforcement:** SQL triggers that block UPDATE/DELETE on manifest and result tables
5. **Idempotency key:** UNIQUE (agent_run_id) with attempt counter for retries
6. **Report versioning:** report_version increments on correction, old versions preserved
7. **Agent status enum:** PENDING, RUNNING, SUCCEEDED, DEGRADED, FAILED, SKIPPED (matching spec)

### Tests for PR1

1. ✅ Migration 028 applies cleanly (idempotent)
2. ✅ All CHECK constraints work (status enum, action_class enum)
3. ✅ UNIQUE constraints prevent duplicates
4. ✅ Immutability triggers block UPDATE/DELETE on manifest and result
5. ✅ Input hash reproducibility (same input → same hash)
6. ✅ Result hash reproducibility
7. ✅ JSON Schema valid input passes validation
8. ✅ JSON Schema missing required field fails
9. ✅ JSON Schema extra field fails (strict mode)
10. ✅ JSON Schema wrong type fails
11. ✅ Unknown contract version fails
12. ✅ Unknown evidence reference fails validation
13. ✅ Small sample → LOW confidence (policy test)
14. ✅ Confidence policy thresholds are versioned
15. ✅ Agent definition CRUD works
16. ✅ Agent run CRUD with status transitions
17. ✅ Evidence catalog builder produces stable IDs
18. ✅ Evidence validator rejects unknown IDs

---

## What is intentionally left to PR2

- Data Readiness Gate implementation
- Orchestrator state machine
- Agent batch creation
- Retry logic
- Runner integration
- LLM abstraction layer

## What is intentionally left to PR3

- Specialist agent prompts
- LLM client implementation
- Input assembly from canonical data
- Specialist execution and validation
- Golden tests

## What is intentionally left to PR4

- Chief Trading Analyst
- Daily report assembly
- Report renderer (markdown)
- Report view

## What is intentionally left to PR5

- DB role isolation (analytics_agent)
- Tool allowlist
- Secret redaction
- Grafana dashboard
- Security tests
