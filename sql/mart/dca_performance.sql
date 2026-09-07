-- DCA Breakeven Performance Metrics for Grafana
-- Breakdown by scanner_name × direction × DCA status

-- ============================================================
-- DCA Performance by Scanner/Direction
-- ============================================================
CREATE OR REPLACE VIEW mart.dca_performance AS
SELECT
    scanner_name,
    direction,
    -- DCA eligibility
    COUNT(*) AS total_positions,
    COUNT(*) FILTER (WHERE dca_enabled) AS dca_eligible,
    COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED') AS dca_filled,
    -- Recovery (price returned to breakeven)
    COUNT(*) FILTER (
        WHERE dca_state IN ('CLOSED_BREAKEVEN', 'CLOSED_STOP')
        AND dca_state = 'CLOSED_BREAKEVEN'
    ) AS recovered,
    -- DCA fill rate
    ROUND(
        COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE dca_enabled), 0),
        4
    ) AS dca_fill_rate,
    -- Recovery rate (of DCA filled positions)
    ROUND(
        COUNT(*) FILTER (
            WHERE dca_state = 'CLOSED_BREAKEVEN'
        )::numeric
        / NULLIF(COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED'), 0),
        4
    ) AS recovery_rate,
    -- Performance: DCA filled positions
    ROUND(AVG(pnl_r) FILTER (
        WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
    ), 4) AS avg_net_r_dca_filled,
    ROUND(
        SUM(GREATEST(pnl_usdt, 0)) FILTER (
            WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
        )
        / NULLIF(
            ABS(SUM(LEAST(pnl_usdt, 0)) FILTER (
                WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
            )),
            0
        ),
        4
    ) AS profit_factor_dca_filled,
    -- Performance: No DCA positions (closed before DCA)
    ROUND(AVG(pnl_r) FILTER (
        WHERE dca_state = 'CLOSED_NO_DCA' AND status = 'CLOSED'
    ), 4) AS avg_net_r_no_dca,
    -- Win rate
    ROUND(
        COUNT(*) FILTER (
            WHERE dca_state = 'DCA_FILLED'
            AND pnl_usdt > 0 AND status = 'CLOSED'
        )::numeric
        / NULLIF(COUNT(*) FILTER (
            WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
        ), 0),
        4
    ) AS win_rate_dca_filled,
    -- Net PnL
    ROUND(SUM(pnl_usdt) FILTER (
        WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
    ), 2) AS total_pnl_dca_filled,
    ROUND(SUM(pnl_usdt) FILTER (
        WHERE (dca_state IS NULL OR dca_state = 'CLOSED_NO_DCA')
        AND status = 'CLOSED'
    ), 2) AS total_pnl_no_dca
FROM dds.paper_trade
WHERE dca_enabled OR dca_state IS NOT NULL
GROUP BY scanner_name, direction
ORDER BY total_pnl_dca_filled DESC NULLS LAST;

-- ============================================================
-- DCA Overview Summary (single-row for dashboard KPIs)
-- ============================================================
CREATE OR REPLACE VIEW mart.dca_overview AS
SELECT
    COUNT(*) AS total_positions,
    COUNT(*) FILTER (WHERE dca_enabled) AS dca_enabled_positions,
    COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED') AS dca_fills,
    ROUND(
        COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE dca_enabled), 0),
        4
    ) AS dca_fill_rate,
    COUNT(*) FILTER (WHERE dca_state = 'CLOSED_BREAKEVEN') AS recovered,
    ROUND(
        COUNT(*) FILTER (WHERE dca_state = 'CLOSED_BREAKEVEN')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE dca_state = 'DCA_FILLED'), 0),
        4
    ) AS recovery_rate,
    ROUND(AVG(pnl_r) FILTER (
        WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
    ), 4) AS avg_net_r,
    ROUND(SUM(pnl_usdt) FILTER (
        WHERE dca_state = 'DCA_FILLED' AND status = 'CLOSED'
    ), 2) AS total_pnl
FROM dds.paper_trade
WHERE dca_enabled OR dca_state IS NOT NULL;
