-- Persist the current Paper Engine runtime configuration independently of events.
-- Safe for existing deployments; NULL remains valid until Paper Engine starts.
ALTER TABLE dds.paper_safety_gate_state
    ADD COLUMN IF NOT EXISTS safety_gate_mode TEXT;
