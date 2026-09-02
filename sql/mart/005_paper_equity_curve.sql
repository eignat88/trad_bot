-- 005_paper_equity_curve.sql
-- Equity curve from dds.paper_account snapshots.
-- Each row represents a point-in-time snapshot of account state.
-- `cumulative_pnl` is computed from the balance delta vs. initial balance.

CREATE OR REPLACE VIEW mart.paper_equity_curve AS
WITH base AS (
    SELECT MIN(balance) AS initial_balance
    FROM dds.paper_account
)
SELECT
    pa.created_at                                  AS snapshot_time,
    pa.balance,
    pa.equity,
    pa.total_pnl                                   AS realized_pnl,
    ROUND(pa.balance - b.initial_balance, 2)       AS cumulative_pnl,
    pa.open_positions,
    pa.total_trades,
    pa.winning_trades,
    pa.losing_trades,
    pa.max_drawdown
FROM dds.paper_account pa
CROSS JOIN base b
ORDER BY pa.created_at;
