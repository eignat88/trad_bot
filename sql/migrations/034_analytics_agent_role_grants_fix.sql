-- Migration 034: Analytics Agent Role — grant corrections
-- Fixes insufficient UPDATE grants from migration 033.
-- The agent pipeline requires INSERT + UPDATE on several tables.
--
-- If migration 033 is already applied, this adds the missing UPDATE grants.
-- If migration 033 is NOT yet applied, this is harmlessly redundant after 033.
--
-- Idempotent: GRANT is idempotent in PostgreSQL.

-- ============================================================
-- 1. AgentRun — pipeline INSERTs then UPDATEs status, tokens, timestamps
-- ============================================================
GRANT INSERT, UPDATE ON analytics.agent_run TO analytics_agent;

-- ============================================================
-- 2. DailyTradingReport — DRAFT → VALIDATED lifecycle requires UPDATE
-- ============================================================
GRANT INSERT, UPDATE ON analytics.daily_trading_report TO analytics_agent;

-- ============================================================
-- 3. AnalysisRun — runner INSERTs then UPDATEs status, finished_at, error
-- ============================================================
GRANT INSERT, UPDATE ON analytics.analysis_run TO analytics_agent;

-- ============================================================
-- 4. AnalysisStageRun — runner INSERTs then UPDATEs status, timing
-- ============================================================
GRANT INSERT, UPDATE ON analytics.analysis_stage_run TO analytics_agent;

-- ============================================================
-- 5. DataQualityResult — may need UPDATE for idempotent upsert
-- ============================================================
GRANT INSERT, UPDATE ON analytics.data_quality_result TO analytics_agent;

-- ============================================================
-- 6. Ensure sequences available for all tables
-- ============================================================
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO analytics_agent;

-- ============================================================
-- 7. Documentation
-- ============================================================
COMMENT ON ROLE analytics_agent
    IS 'Stage 3 agent runtime role — read market/dds, write analytics-owned tables (migrations 033+034)';
