-- Migration 028: Stage 3 Agent Foundation
-- Creates agent infrastructure tables:
--   agent_definition  — registry of AI agents with contract/prompt versions
--   agent_run         — per-analysis-run execution records for each agent
--   agent_input_manifest — immutable snapshot of inputs fed to an agent
--   agent_result      — immutable snapshot of an agent's output
--   daily_trading_report — final human-readable report assembled from agent results
--
-- Idempotent: safe to run multiple times.

-- ============================================================
-- 1. AGENT DEFINITION — registry of known agents
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.agent_definition (
    agent_name       TEXT PRIMARY KEY,
    agent_type       TEXT NOT NULL CHECK (agent_type IN ('SPECIALIST', 'CHIEF')),
    contract_version TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT,  -- LLM model identifier (separate from agent_name)
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE analytics.agent_definition
    IS 'Registry of AI agents used in the analytics pipeline (Stage 3)';
COMMENT ON COLUMN analytics.agent_definition.agent_name
    IS 'Unique agent identifier (e.g. ''setup_reviewer'', ''risk_chief'')';
COMMENT ON COLUMN analytics.agent_definition.agent_type
    IS 'SPECIALIST for domain agents, CHIEF for orchestrating/synthesis agent';
COMMENT ON COLUMN analytics.agent_definition.contract_version
    IS 'Schema version of the agent''s input/output contract (e.g. ''v1'')';
COMMENT ON COLUMN analytics.agent_definition.prompt_version
    IS 'Version of the system prompt template (e.g. ''v1'')';
COMMENT ON COLUMN analytics.agent_definition.model
    IS 'LLM model identifier for this agent (e.g. ''gpt-4o'', ''claude-sonnet-4-20250514''). NULL means use agent_name as fallback.';

-- updated_at trigger (reuses analytics.update_updated_at_column from 008)
DROP TRIGGER IF EXISTS trg_agent_definition_updated_at
    ON analytics.agent_definition;

CREATE TRIGGER trg_agent_definition_updated_at
    BEFORE UPDATE ON analytics.agent_definition
    FOR EACH ROW
    EXECUTE FUNCTION analytics.update_updated_at_column();

-- ============================================================
-- 2. AGENT RUN — execution record for each agent invocation
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.agent_run (
    agent_run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_id    UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    agent_name         TEXT NOT NULL REFERENCES analytics.agent_definition(agent_name),
    attempt            INT NOT NULL DEFAULT 1,
    model              TEXT,
    status             TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','DEGRADED','FAILED','SKIPPED')),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    latency_ms         BIGINT,
    input_tokens       INT,
    output_tokens      INT,
    total_tokens       INT,
    error_class        TEXT,
    error_message      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (analysis_run_id, agent_name, attempt),

    CHECK (error_message IS NULL
           OR (error_message NOT LIKE '%password%'
               AND error_message NOT LIKE '%secret%'
               AND error_message NOT LIKE '%token%'))
);

COMMENT ON TABLE analytics.agent_run
    IS 'One row per agent invocation within an analysis run (Stage 3 execution log)';
COMMENT ON COLUMN analytics.agent_run.agent_run_id
    IS 'Surrogate PK — UUID, generated server-side';
COMMENT ON COLUMN analytics.agent_run.analysis_run_id
    IS 'FK to the parent analysis_run that triggered this agent';
COMMENT ON COLUMN analytics.agent_run.agent_name
    IS 'FK to agent_definition — which agent was invoked';
COMMENT ON COLUMN analytics.agent_run.attempt
    IS 'Retry ordinal (1 = first attempt)';
COMMENT ON COLUMN analytics.agent_run.model
    IS 'LLM model identifier used for this invocation (e.g. ''gpt-4o'')';
COMMENT ON COLUMN analytics.agent_run.status
    IS 'Terminal lifecycle: PENDING → RUNNING → SUCCEEDED / DEGRADED / FAILED / SKIPPED';
COMMENT ON COLUMN analytics.agent_run.latency_ms
    IS 'Wall-clock latency of the LLM call in milliseconds';

-- Indexes for agent_run
CREATE INDEX IF NOT EXISTS idx_agent_run_analysis_run_id
    ON analytics.agent_run (analysis_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_run_status
    ON analytics.agent_run (status);
CREATE INDEX IF NOT EXISTS idx_agent_run_agent_name_status
    ON analytics.agent_run (agent_name, status);

-- ============================================================
-- 3. AGENT INPUT MANIFEST — immutable snapshot of agent inputs
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.agent_input_manifest (
    agent_run_id         UUID PRIMARY KEY REFERENCES analytics.agent_run(agent_run_id) ON DELETE CASCADE,
    dataset_version      TEXT NOT NULL,
    input_hash           TEXT NOT NULL,
    schema_version       TEXT NOT NULL,
    analysis_window_from TIMESTAMPTZ NOT NULL,
    analysis_window_to   TIMESTAMPTZ NOT NULL,
    maturity             TEXT NOT NULL CHECK (maturity IN ('PROVISIONAL','FINAL')),
    data_quality_status  TEXT NOT NULL CHECK (data_quality_status IN ('PASS','DEGRADED')),
    limitations          JSONB NOT NULL DEFAULT '[]'::jsonb,
    sample_sizes         JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics              JSONB NOT NULL DEFAULT '{}'::jsonb,
    segments             JSONB NOT NULL DEFAULT '[]'::jsonb,
    cases                JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_ids         JSONB NOT NULL DEFAULT '[]'::jsonb,
    manifest_json        JSONB NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE analytics.agent_input_manifest
    IS 'Immutable snapshot of all data fed to an agent (provenance & reproducibility)';
COMMENT ON COLUMN analytics.agent_input_manifest.input_hash
    IS 'SHA-256 of the canonical serialised input payload';
COMMENT ON COLUMN analytics.agent_input_manifest.dataset_version
    IS 'Version tag of the data product consumed (e.g. ''v2025-01-15'')';
COMMENT ON COLUMN analytics.agent_input_manifest.schema_version
    IS 'Schema version of this manifest itself (e.g. ''v1'')';
COMMENT ON COLUMN analytics.agent_input_manifest.manifest_json
    IS 'Full serialised manifest for audit / replay';

-- Immutability trigger for agent_input_manifest
-- (reuses the same pattern as analytics.block_trade_event_mutation from 014)

CREATE OR REPLACE FUNCTION analytics.block_agent_input_manifest_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agent_input_manifest is append-only — % is not permitted. '
                     'Inputs must not be changed after an agent run starts.',
                     TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION analytics.block_agent_input_manifest_mutation()
    IS 'Blocks UPDATE and DELETE on agent_input_manifest to enforce immutability';

DROP TRIGGER IF EXISTS trg_agent_input_manifest_no_update ON analytics.agent_input_manifest;
DROP TRIGGER IF EXISTS trg_agent_input_manifest_no_delete ON analytics.agent_input_manifest;

CREATE TRIGGER trg_agent_input_manifest_no_update
    BEFORE UPDATE ON analytics.agent_input_manifest
    FOR EACH ROW
    EXECUTE FUNCTION analytics.block_agent_input_manifest_mutation();

CREATE TRIGGER trg_agent_input_manifest_no_delete
    BEFORE DELETE ON analytics.agent_input_manifest
    FOR EACH ROW
    EXECUTE FUNCTION analytics.block_agent_input_manifest_mutation();

-- ============================================================
-- 4. AGENT RESULT — immutable snapshot of agent output
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.agent_result (
    agent_run_id      UUID PRIMARY KEY REFERENCES analytics.agent_run(agent_run_id) ON DELETE CASCADE,
    schema_version    TEXT NOT NULL,
    result_json       JSONB NOT NULL,
    result_hash       TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK (validation_status IN ('PENDING','VALID','INVALID','REPAIR_ATTEMPTED')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE analytics.agent_result
    IS 'Immutable agent output — stored once per successful (or degraded) agent run';
COMMENT ON COLUMN analytics.agent_result.result_hash
    IS 'SHA-256 of the canonical serialised result_json';
COMMENT ON COLUMN analytics.agent_result.validation_status
    IS 'Schema validation outcome: PENDING → VALID / INVALID / REPAIR_ATTEMPTED';

-- Index for validation monitoring
CREATE INDEX IF NOT EXISTS idx_agent_result_validation_status
    ON analytics.agent_result (validation_status);

-- Immutability trigger for agent_result
CREATE OR REPLACE FUNCTION analytics.block_agent_result_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agent_result is append-only — % is not permitted. '
                     'Results must not be altered after recording.',
                     TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION analytics.block_agent_result_mutation()
    IS 'Blocks UPDATE and DELETE on agent_result to enforce immutability';

DROP TRIGGER IF EXISTS trg_agent_result_no_update ON analytics.agent_result;
DROP TRIGGER IF EXISTS trg_agent_result_no_delete ON analytics.agent_result;

CREATE TRIGGER trg_agent_result_no_update
    BEFORE UPDATE ON analytics.agent_result
    FOR EACH ROW
    EXECUTE FUNCTION analytics.block_agent_result_mutation();

CREATE TRIGGER trg_agent_result_no_delete
    BEFORE DELETE ON analytics.agent_result
    FOR EACH ROW
    EXECUTE FUNCTION analytics.block_agent_result_mutation();

-- ============================================================
-- 5. DAILY TRADING REPORT — final human-readable synthesis
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.daily_trading_report (
    report_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_id       UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    report_version        INT NOT NULL DEFAULT 1,
    maturity              TEXT NOT NULL CHECK (maturity IN ('PROVISIONAL','FINAL')),
    executive_summary     TEXT NOT NULL,
    findings              JSONB NOT NULL DEFAULT '[]'::jsonb,
    hypotheses            JSONB NOT NULL DEFAULT '[]'::jsonb,
    proposed_experiments  JSONB NOT NULL DEFAULT '[]'::jsonb,
    action_class          TEXT NOT NULL DEFAULT 'NO_ACTION'
                          CHECK (action_class IN ('NO_ACTION','MONITOR','INVESTIGATE','BACKTEST','DATA_FIX','PRODUCTION_INCIDENT')),
    limitations           JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_refs         JSONB NOT NULL DEFAULT '[]'::jsonb,
    partial               BOOLEAN NOT NULL DEFAULT FALSE,
    missing_agents        JSONB NOT NULL DEFAULT '[]'::jsonb,
    status                TEXT NOT NULL DEFAULT 'DRAFT'
                          CHECK (status IN ('DRAFT','VALIDATED','PUBLISHED','SUPERSEDED')),
    agent_run_ids         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (analysis_run_id, maturity, report_version)
);

COMMENT ON TABLE analytics.daily_trading_report
    IS 'Final daily report synthesised from agent results — the human-readable deliverable';
COMMENT ON COLUMN analytics.daily_trading_report.executive_summary
    IS 'Free-text 2-3 sentence executive summary of the trading day';
COMMENT ON COLUMN analytics.daily_trading_report.action_class
    IS 'Highest-severity recommended action: NO_ACTION → MONITOR → INVESTIGATE → BACKTEST → DATA_FIX → PRODUCTION_INCIDENT';
COMMENT ON COLUMN analytics.daily_trading_report.partial
    IS 'TRUE when the report was assembled from incomplete agent coverage';
COMMENT ON COLUMN analytics.daily_trading_report.missing_agents
    IS 'JSONB array of agent_name strings that did not produce results';
COMMENT ON COLUMN analytics.daily_trading_report.status
    IS 'Lifecycle: DRAFT → VALIDATED → PUBLISHED; SUPERSEDED by a newer version';

-- Indexes for daily_trading_report
CREATE INDEX IF NOT EXISTS idx_daily_trading_report_analysis_run_id
    ON analytics.daily_trading_report (analysis_run_id);
CREATE INDEX IF NOT EXISTS idx_daily_trading_report_status
    ON analytics.daily_trading_report (status);
CREATE INDEX IF NOT EXISTS idx_daily_trading_report_maturity
    ON analytics.daily_trading_report (maturity);

-- ============================================================
-- Done
-- ============================================================
