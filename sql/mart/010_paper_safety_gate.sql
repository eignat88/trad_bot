-- 010_paper_safety_gate.sql
-- Persisted Paper Bot safety observations, effective gate status, and durable gate history.

CREATE OR REPLACE VIEW mart.paper_safety_metrics AS
WITH latest_snapshot AS (
    SELECT balance, equity, open_positions, total_trades, winning_trades, losing_trades
    FROM dds.paper_account
    ORDER BY snapshot_id DESC
    LIMIT 1
),
recent_events AS (
    SELECT
        COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP') AS stop_gap_24h,
        COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP' AND severity = 'SEVERE') AS severe_stop_gap_24h,
        COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP' AND would_block) AS would_block_24h,
        COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP' AND gate_blocked) AS actual_blocks_24h,
        ROUND(AVG(gap_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS avg_gap_r_24h,
        ROUND(MAX(gap_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS max_gap_r_24h,
        ROUND(AVG(excess_execution_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS avg_excess_execution_r_24h,
        ROUND(MAX(excess_execution_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS max_excess_execution_r_24h,
        MAX(event_at) FILTER (WHERE event_type = 'STOP_LOSS_GAP') AS last_stop_gap_at,
        MAX(event_at) FILTER (WHERE severity = 'SEVERE') AS last_severe_event_at
    FROM dds.paper_safety_event
    WHERE event_at >= NOW() - INTERVAL '24 hours'
),
active_signals AS (
    SELECT COUNT(*) AS active_count FROM dds.market_signal WHERE status = 'ACTIVE'
),
safety_gate AS (
    SELECT
        COALESCE(g.is_blocked, FALSE) AS is_blocked,
        g.reason,
        g.blocked_since,
        g.safety_gate_mode
    FROM (SELECT 1) singleton
    LEFT JOIN dds.paper_safety_gate_state g ON g.gate_id = 1
)
SELECT
    ls.balance, ls.equity, ls.open_positions, ls.total_trades, ls.winning_trades, ls.losing_trades,
    COALESCE(re.stop_gap_24h, 0) AS stop_gap_24h,
    COALESCE(re.severe_stop_gap_24h, 0) AS severe_stop_gap_24h,
    COALESCE(re.would_block_24h, 0) AS would_block_24h,
    COALESCE(re.actual_blocks_24h, 0) AS actual_blocks_24h,
    re.avg_gap_r_24h, re.max_gap_r_24h,
    re.avg_excess_execution_r_24h, re.max_excess_execution_r_24h,
    re.last_stop_gap_at, re.last_severe_event_at,
    gate.safety_gate_mode,
    gate.is_blocked, gate.reason AS gate_reason, gate.blocked_since AS gate_blocked_since,
    asig.active_count AS active_signals,
    -- is_blocked is durable history; only enforce mode applies it to new entries.
    -- NULL or unrecognised runtime modes therefore remain explicitly OPEN.
    CASE
        WHEN gate.safety_gate_mode = 'enforce' AND gate.is_blocked THEN 'BLOCKED'
        ELSE 'OPEN'
    END AS gate_status
FROM latest_snapshot ls
CROSS JOIN recent_events re
CROSS JOIN active_signals asig
CROSS JOIN safety_gate gate;

COMMENT ON VIEW mart.paper_safety_metrics IS
'Persisted 24-hour paper safety observations, effective runtime gate status, and durable gate history.';

-- Compatibility name used by existing dashboard queries.
CREATE OR REPLACE VIEW mart.paper_safety_gate AS
SELECT * FROM mart.paper_safety_metrics;

CREATE OR REPLACE VIEW mart.paper_safety_by_scanner AS
SELECT
    scanner_name, symbol, direction,
    COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP') AS total_stop_gaps,
    COUNT(*) FILTER (WHERE event_type = 'STOP_LOSS_GAP' AND severity = 'SEVERE') AS severe_stop_gaps,
    ROUND(AVG(gap_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS avg_gap_r,
    ROUND(MAX(gap_r) FILTER (WHERE event_type = 'STOP_LOSS_GAP'), 4) AS max_gap_r,
    MAX(event_at) AS last_event_at
FROM dds.paper_safety_event
GROUP BY scanner_name, symbol, direction;

CREATE OR REPLACE VIEW mart.paper_safety_recent_events AS
SELECT event_at, symbol, scanner_name, direction, event_type, severity,
       expected_stop_net_r, actual_net_r, gap_r, would_block, gate_blocked
FROM dds.paper_safety_event
ORDER BY event_at DESC;
