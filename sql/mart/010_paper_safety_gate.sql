-- 010_paper_safety_gate.sql
-- Paper Bot Safety Gate status.

-- =============================================
-- 1. paper_safety_gate — Current gate status
-- =============================================
CREATE OR REPLACE VIEW mart.paper_safety_gate AS
WITH latest_snapshot AS (
    SELECT balance, equity, open_positions, total_trades, winning_trades, losing_trades
    FROM dds.paper_account
    ORDER BY snapshot_id DESC
    LIMIT 1
),
recent_stop_gap AS (
    SELECT 
        COUNT(*) AS stop_gap_count_24h,
        MAX(closed_at) AS last_stop_gap_at,
        ROUND(AVG(pnl_r), 2) AS avg_gap_r
    FROM dds.paper_trade
    WHERE exit_reason = 'STOP_LOSS_GAP'
      AND closed_at >= NOW() - INTERVAL '24 hours'
),
last_trade AS (
    SELECT 
        closed_at AS last_trade_closed,
        exit_reason AS last_exit_reason,
        ROUND(pnl_r, 2) AS last_trade_r,
        symbol AS last_symbol
    FROM dds.paper_trade
    WHERE status = 'CLOSED'
    ORDER BY closed_at DESC
    LIMIT 1
),
active_signals AS (
    SELECT COUNT(*) AS active_count
    FROM dds.market_signal
    WHERE status = 'ACTIVE'
)
SELECT
    ls.balance,
    ls.equity,
    ls.open_positions,
    ls.total_trades,
    ls.winning_trades,
    ls.losing_trades,
    sg.stop_gap_count_24h,
    sg.last_stop_gap_at,
    sg.avg_gap_r,
    lt.last_trade_closed,
    lt.last_exit_reason,
    lt.last_trade_r,
    lt.last_symbol,
    asig.active_count AS active_signals,
    CASE
        WHEN sg.stop_gap_count_24h > 0 AND ls.open_positions = 0 THEN 'BLOCKED'
        WHEN ls.open_positions > 0 THEN 'TRADING'
        WHEN ls.total_trades = 0 THEN 'NO_TRADES'
        ELSE 'OK'
    END AS gate_status
FROM latest_snapshot ls
CROSS JOIN recent_stop_gap sg
CROSS JOIN last_trade lt
CROSS JOIN active_signals asig;

COMMENT ON VIEW mart.paper_safety_gate IS 'Paper bot safety gate: BLOCKED if STOP_LOSS_GAP in 24h with no open positions.';

-- =============================================
-- 2. paper_trade_summary — Extended stats
-- =============================================
CREATE OR REPLACE VIEW mart.paper_trade_summary AS
WITH stats AS (
    SELECT
        COUNT(*) AS total_trades,
        COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_trades,
        COUNT(*) FILTER (WHERE status = 'OPEN') AS open_trades,
        COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0) AS wins,
        COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_r <= 0) AS losses,
        ROUND(SUM(pnl_usdt) FILTER (WHERE status = 'CLOSED'), 2) AS total_pnl_usdt,
        ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_r,
        COUNT(*) FILTER (WHERE exit_reason = 'STOP_LOSS_GAP') AS stop_gap_total,
        COUNT(*) FILTER (WHERE exit_reason = 'STOP_LOSS_GAP' AND closed_at >= NOW() - INTERVAL '24 hours') AS stop_gap_24h
    FROM dds.paper_trade
)
SELECT
    total_trades,
    closed_trades,
    open_trades,
    wins,
    losses,
    CASE WHEN closed_trades > 0 THEN ROUND(100.0 * wins / closed_trades, 1) ELSE 0 END AS win_rate_pct,
    total_pnl_usdt,
    avg_r,
    stop_gap_total,
    stop_gap_24h
FROM stats;

COMMENT ON VIEW mart.paper_trade_summary IS 'Extended paper trade summary with STOP_LOSS_GAP tracking.';
