-- Append-only paper safety-event history. Safe to apply to existing deployments.
CREATE TABLE IF NOT EXISTS dds.paper_safety_event (
    event_id                 BIGSERIAL PRIMARY KEY,
    event_at                 TIMESTAMPTZ NOT NULL,
    event_type               TEXT NOT NULL,
    severity                 TEXT NOT NULL,
    safety_gate_mode         TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    scanner_name             TEXT NOT NULL,
    direction                TEXT NOT NULL,
    entry_price              NUMERIC,
    stop_price               NUMERIC,
    expected_exit            NUMERIC,
    observed_price           NUMERIC,
    raw_stop_r               NUMERIC,
    expected_stop_net_r      NUMERIC,
    actual_net_r             NUMERIC,
    gap_pct                  NUMERIC,
    gap_r                    NUMERIC,
    excess_execution_r       NUMERIC,
    position_size            NUMERIC,
    risk_usdt                NUMERIC,
    duration_sec             NUMERIC,
    would_block              BOOLEAN NOT NULL DEFAULT FALSE,
    gate_blocked             BOOLEAN NOT NULL DEFAULT FALSE,
    details_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT paper_safety_event_mode_chk CHECK (
        safety_gate_mode IN ('enforce', 'observe', 'disabled')
    )
);

CREATE INDEX IF NOT EXISTS idx_paper_safety_event_at
    ON dds.paper_safety_event (event_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_safety_event_type_at
    ON dds.paper_safety_event (event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_safety_event_scanner_at
    ON dds.paper_safety_event (scanner_name, event_at DESC);
