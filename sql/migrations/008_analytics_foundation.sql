-- Migration 008: Analytics Foundation
-- Creates analytics schema and core tables for daily analytics pipeline

-- 1. Create analytics schema
CREATE SCHEMA IF NOT EXISTS analytics;

-- 2. Create analysis_run table
CREATE TABLE IF NOT EXISTS analytics.analysis_run (
    run_id UUID PRIMARY KEY,
    business_date DATE NOT NULL,
    schedule_timezone TEXT NOT NULL DEFAULT 'Europe/Sofia',
    analysis_from TIMESTAMPTZ NOT NULL,
    analysis_to TIMESTAMPTZ NOT NULL,
    observation_cutoff TIMESTAMPTZ NOT NULL,
    post_exit_horizon INTERVAL NOT NULL DEFAULT '4 hours',
    maturity TEXT NOT NULL DEFAULT 'PROVISIONAL' 
        CHECK (maturity IN ('PROVISIONAL', 'FINAL', 'PARTIAL', 'FAILED')),
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    pipeline_version TEXT NOT NULL DEFAULT '1.0.0',
    source_watermarks JSONB NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_code TEXT,
    error_message TEXT,
    
    -- Constraints
    CHECK (analysis_from < analysis_to),
    CHECK (error_message IS NULL OR error_message NOT LIKE '%password%' AND error_message NOT LIKE '%secret%')
);

-- 3. Add unique constraint for logical run (one FINAL per business_date per version)
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_run_logical 
    ON analytics.analysis_run (business_date, pipeline_version) 
    WHERE maturity = 'FINAL';

-- 4. Add index for common queries
CREATE INDEX IF NOT EXISTS idx_analysis_run_business_date 
    ON analytics.analysis_run (business_date);
CREATE INDEX IF NOT EXISTS idx_analysis_run_maturity 
    ON analytics.analysis_run (maturity);
CREATE INDEX IF NOT EXISTS idx_analysis_run_status 
    ON analytics.analysis_run (status);

-- 5. Create analysis_stage_run table
CREATE TABLE IF NOT EXISTS analytics.analysis_stage_run (
    stage_run_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    stage_name TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    input_rows BIGINT,
    output_rows BIGINT,
    watermark TIMESTAMPTZ,
    result_json JSONB,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_code TEXT,
    error_message TEXT,
    
    -- Unique constraint for stage attempts
    UNIQUE (run_id, stage_name, attempt)
);

-- 6. Add indexes for stage queries
CREATE INDEX IF NOT EXISTS idx_analysis_stage_run_run_id 
    ON analytics.analysis_stage_run (run_id);
CREATE INDEX IF NOT EXISTS idx_analysis_stage_run_stage_name 
    ON analytics.analysis_stage_run (stage_name);
CREATE INDEX IF NOT EXISTS idx_analysis_stage_run_status 
    ON analytics.analysis_stage_run (status);

-- 7. Create data_quality_result table
CREATE TABLE IF NOT EXISTS analytics.data_quality_result (
    quality_result_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    stage_name TEXT NOT NULL,
    check_name TEXT NOT NULL,
    scope_type TEXT,
    scope_id TEXT,
    severity TEXT NOT NULL 
        CHECK (severity IN ('BLOCKING', 'DEGRADED', 'WARNING')),
    status TEXT NOT NULL 
        CHECK (status IN ('PASS', 'FAIL', 'SKIPPED')),
    expected_value JSONB,
    actual_value JSONB,
    affected_entity_count BIGINT,
    affected_entity_ids JSONB,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details JSONB
);

-- 8. Add indexes for quality checks
CREATE INDEX IF NOT EXISTS idx_data_quality_result_run_id 
    ON analytics.data_quality_result (run_id);
CREATE INDEX IF NOT EXISTS idx_data_quality_result_check_name 
    ON analytics.data_quality_result (check_name);
CREATE INDEX IF NOT EXISTS idx_data_quality_result_severity 
    ON analytics.data_quality_result (severity);
CREATE INDEX IF NOT EXISTS idx_data_quality_result_status 
    ON analytics.data_quality_result (status);

-- 9. Create function to update updated_at timestamp
CREATE OR REPLACE FUNCTION analytics.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 10. Add trigger to analysis_run
CREATE TRIGGER update_analysis_run_updated_at
    BEFORE UPDATE ON analytics.analysis_run
    FOR EACH ROW
    EXECUTE FUNCTION analytics.update_updated_at_column();

-- 11. Create function to generate unique run_id
CREATE OR REPLACE FUNCTION analytics.generate_run_id()
RETURNS UUID AS $$
BEGIN
    RETURN gen_random_uuid();
END;
$$ LANGUAGE plpgsql;

-- 12. Create view for analysis run summary
CREATE OR REPLACE VIEW analytics.analysis_run_summary AS
SELECT 
    run_id,
    business_date,
    maturity,
    status,
    pipeline_version,
    analysis_from,
    analysis_to,
    started_at,
    finished_at,
    EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_seconds,
    error_code,
    error_message,
    created_at,
    updated_at
FROM analytics.analysis_run
ORDER BY business_date DESC, created_at DESC;

-- 13. Create view for stage performance
CREATE OR REPLACE VIEW analytics.stage_performance AS
SELECT 
    ar.business_date,
    ar.pipeline_version,
    asr.stage_name,
    asr.attempt,
    asr.status,
    asr.input_rows,
    asr.output_rows,
    EXTRACT(EPOCH FROM (asr.finished_at - asr.started_at)) AS duration_seconds,
    asr.error_code,
    asr.error_message
FROM analytics.analysis_stage_run asr
JOIN analytics.analysis_run ar ON asr.run_id = ar.run_id
ORDER BY ar.business_date DESC, asr.started_at;

-- 14. Create view for quality gate summary
CREATE OR REPLACE VIEW analytics.quality_gate_summary AS
SELECT 
    ar.business_date,
    ar.pipeline_version,
    dqr.stage_name,
    dqr.check_name,
    dqr.severity,
    dqr.status,
    dqr.affected_entity_count,
    dqr.checked_at
FROM analytics.data_quality_result dqr
JOIN analytics.analysis_run ar ON dqr.run_id = ar.run_id
ORDER BY ar.business_date DESC, dqr.checked_at;

-- 15. Add comments for documentation
COMMENT ON TABLE analytics.analysis_run IS 'Daily analytics pipeline run tracking';
COMMENT ON TABLE analytics.analysis_stage_run IS 'Individual stage execution tracking';
COMMENT ON TABLE analytics.data_quality_result IS 'Data quality check results';
COMMENT ON COLUMN analytics.analysis_run.maturity IS 'PROVISIONAL: initial run, FINAL: complete with post-exit data, PARTIAL: incomplete, FAILED: error';
COMMENT ON COLUMN analytics.analysis_run.analysis_from IS 'Start of analysis window (inclusive)';
COMMENT ON COLUMN analytics.analysis_run.analysis_to IS 'End of analysis window (exclusive)';
COMMENT ON COLUMN analytics.analysis_run.observation_cutoff IS 'Timestamp after which data is considered fresh';
COMMENT ON COLUMN analytics.analysis_run.post_exit_horizon IS 'Required post-exit data window (default 4 hours)';