# PR3 SPECIALIST AGENTS AUDIT

**Branch:** feat/stage3-specialist-agents  
**HEAD:** (current, uncommitted)  
**Base:** main @ 3040695d

## Canonical tables/views available

| Table | Columns | Purpose |
|-------|---------|---------|
| `analytics.trade_fact` | 44 columns | Canonical trade with lifecycle, PnL, R-metrics |
| `analytics.setup_fact` | 22 columns | Canonical setup snapshots per run |
| `analytics.trade_event` | 14 columns | Append-only event journal (immutable) |
| `analytics.trade_horizon_metric` | 11 columns | Forward-looking metrics at fixed horizons |
| `analytics.trade_replay_metric` | 11 columns | Scenario-based replay results |
| `analytics.metric_snapshot` | 9 columns | Period aggregates (24h/7d/30d) by segment |
| `analytics.metric_registry` | 9 columns | 20 seeded metric definitions |
| `analytics.data_quality_result` | 14 columns | Quality check results |

## Dataset publication source

`analytics.dataset_publication` — computed `dataset_version` = SHA-256 of publication manifest. Status lifecycle: `BUILDING → READY | FAILED`. Runner creates publication in `_stage_dataset_publication`.

## Existing agent contracts

- `contracts/v1/agent_input.schema.json` — Input manifest schema (strict, no extra fields)
- `contracts/v1/specialist_output.schema.json` — Output schema with observations/hypotheses/experiments
- `contracts/v1/chief_input.schema.json` — Chief input (validated specialist outputs)
- `contracts/v1/chief_output.schema.json` — Chief output (daily report)

## Evidence implementation

- `build_metric_id(scanner, direction, window, metric_name)` → `metric:scanner:{s}:{d}:{w}:{m}`
- `build_case_id(type, id)` → `case:{type}:{id}`
- `build_quality_id(check)` → `quality:{check}`
- `EvidenceCatalog.validate()` / `validate_refs()` — rejects unknown IDs

## Confidence policy

`ConfidencePolicyV1`: HIGH requires ≥30 sample, ≥3 windows, FINAL, PASS quality, no gaps. MEDIUM requires ≥10 sample, ≥2 windows.

## Agent model abstraction

`AgentModelClient` ABC with `generate()` method. `OpenAICompatibleClient` (httpx-based) for production. `StubModelClient` for tests.

## Available LLM dependencies

- `pg8000==1.31.5` (DB)
- `jsonschema>=4.20` (schema validation)
- `httpx` needed for `OpenAICompatibleClient` (to be added to requirements)

## Settings/env framework

`app/config/settings.py` — frozen dataclass with all env vars. No LLM-specific settings yet (model configured per-agent-definition).

## Exact source for specialist data

- **Funnel facts**: `analytics.metric_snapshot` grouped by `segment` (format `scanner_direction:{name}:{dir}`), periods 24h/7d/30d
- **Trade performance**: `analytics.trade_fact` (pnl_r, win_rate, etc.)
- **Execution metrics**: `analytics.trade_horizon_metric` (entry MFE/MAE, post-exit opportunity)
- **Replay metrics**: `analytics.trade_replay_metric` (ACTUAL vs NO_DCA)
- **Drift baselines**: `analytics.metric_snapshot` 24h vs 7d comparison
- **Quality findings**: `analytics.data_quality_result` for the run

## Gaps identified and addressed

1. ✅ No repository methods for canonical tables → Created `SpecialistDataRepository`
2. ✅ Empty evidence_ids in manifests → Input assemblers populate real evidence
3. ✅ No LLM provider SDK → Created `OpenAICompatibleClient` using httpx
4. ✅ No prompt builders → Created versioned prompts for all 3 specialists
5. ✅ No specialist registry → Created `registry.py` with assembler/prompt mappings
6. ⚠️ httpx not in requirements.txt → needs to be added

## Implementation plan

1. Input assemblers (3 files) + data repository
2. Versioned prompts (3 files) + provider client
3. Specialist registry
4. Orchestrator wiring (assemblers → manifest → prompt builder)
5. Tests (39 specialist tests + existing suite)
