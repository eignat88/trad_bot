-- Persist the configuration of the active Paper Runner independently of safety events.
-- This does not change or clear dds.paper_safety_gate_state.
CREATE TABLE IF NOT EXISTS dds.paper_runtime_state (
    state_id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (state_id = 1),
    safety_gate_mode  TEXT NOT NULL CHECK (safety_gate_mode IN ('enforce', 'observe', 'disabled')),
    runner_started_at TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
