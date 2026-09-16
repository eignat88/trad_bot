-- Migration 031: Add model column to agent_definition
-- Adds the LLM model identifier column to agent_definition.
--
-- The model field stores the actual LLM provider model identifier
-- (e.g. "gpt-4o", "claude-sonnet-4-20250514"), separate from
-- agent_name and contract_version.
--
-- Idempotent: uses ADD COLUMN IF NOT EXISTS.

ALTER TABLE analytics.agent_definition
ADD COLUMN IF NOT EXISTS model TEXT;

COMMENT ON COLUMN analytics.agent_definition.model
    IS 'LLM provider model identifier (e.g. gpt-4o). Separate from agent_name and contract_version.';
