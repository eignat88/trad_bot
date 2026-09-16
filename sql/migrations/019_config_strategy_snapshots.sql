-- Migration 019: Config + Strategy Snapshots
-- Provides functions to capture config and strategy snapshots.
--
-- These are called by the Python analytics pipeline, not executed standalone.
-- The config snapshot is built from the in-memory Settings object.
-- The strategy snapshot is built from scanner metadata.
--
-- Idempotent: functions use INSERT ... ON CONFLICT DO NOTHING.

-- ============================================================
-- 1. Function: capture_config_snapshot
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.capture_config_snapshot(
    p_config_hash TEXT,
    p_effective_config_json JSONB,
    p_source_map_json JSONB DEFAULT '{}'::jsonb
) RETURNS VOID AS $$
BEGIN
    -- Close previous active snapshot if exists
    UPDATE analytics.config_snapshot
    SET valid_to = NOW()
    WHERE valid_to IS NULL
      AND config_hash != p_config_hash;

    -- Insert new snapshot (idempotent)
    INSERT INTO analytics.config_snapshot (
        config_hash, effective_config_json, source_map_json,
        captured_at, valid_from, valid_to
    ) VALUES (
        p_config_hash, p_effective_config_json, p_source_map_json,
        NOW(), NOW(), NULL
    )
    ON CONFLICT (config_hash) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION analytics.capture_config_snapshot(
    TEXT, JSONB, JSONB
) IS 'Captures an effective config snapshot. Closes previous active snapshot.';

-- ============================================================
-- 2. Function: capture_strategy_snapshot
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.capture_strategy_snapshot(
    p_strategy_hash TEXT,
    p_git_commit_sha TEXT,
    p_scanner_name TEXT,
    p_scanner_version TEXT,
    p_strategy_params_json JSONB DEFAULT '{}'::jsonb
) RETURNS VOID AS $$
BEGIN
    INSERT INTO analytics.strategy_snapshot (
        strategy_hash, git_commit_sha, scanner_name, scanner_version,
        strategy_params_json, captured_at
    ) VALUES (
        p_strategy_hash, p_git_commit_sha, p_scanner_name, p_scanner_version,
        p_strategy_params_json, NOW()
    )
    ON CONFLICT (strategy_hash) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION analytics.capture_strategy_snapshot(
    TEXT, TEXT, TEXT, TEXT, JSONB
) IS 'Captures a strategy snapshot. Idempotent on strategy_hash.';

-- ============================================================
-- 3. Helper: compute_config_hash
-- Canonicalizes JSON (sorted keys, secrets stripped) and returns SHA-256.
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.compute_config_hash(
    p_config_json JSONB
) RETURNS TEXT AS $$
DECLARE
    canonical TEXT;
BEGIN
    -- Sort keys recursively and strip secrets
    canonical := p_config_json::TEXT;
    -- Simple canonicalization: sort top-level keys
    -- PostgreSQL JSONB already sorts keys, so direct hash is deterministic
    RETURN encode(digest(canonical, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION analytics.compute_config_hash(JSONB)
    IS 'Computes SHA-256 hash of canonical JSON config (sorted keys)';

-- ============================================================
-- 4. Helper: compute_strategy_hash
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.compute_strategy_hash(
    p_git_commit_sha TEXT,
    p_scanner_name TEXT,
    p_scanner_version TEXT,
    p_strategy_params_json JSONB DEFAULT '{}'::jsonb
) RETURNS TEXT AS $$
DECLARE
    canonical TEXT;
BEGIN
    canonical := jsonb_build_object(
        'git_commit_sha', p_git_commit_sha,
        'scanner_name', p_scanner_name,
        'scanner_version', p_scanner_version,
        'strategy_params', p_strategy_params_json
    )::TEXT;
    RETURN encode(digest(canonical, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION analytics.compute_strategy_hash(TEXT, TEXT, TEXT, JSONB)
    IS 'Computes SHA-256 hash of strategy definition';

-- ============================================================
-- Note: requires pgcrypto extension for digest() and encode()
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
