-- ============================================================
-- Shadow/Counterfactual Paper Trades for Experimental Scanners
-- ME SHORT REVERSE LONG V1
-- ============================================================
-- This table tracks shadow/counterfactual trades that are derived
-- from existing scanner signals but test alternative hypotheses
-- without affecting the main paper balance or live trading.
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.paper_shadow_trade (
    shadow_trade_id     BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'ME_SHORT_REVERSE_LONG_V1',
    source_trade_id     BIGINT REFERENCES dds.paper_trade(trade_id),
    source_setup_id     TEXT REFERENCES dds.scanner_setup(setup_id),
    source_scanner      TEXT NOT NULL,
    source_direction    TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    scanner_name        TEXT NOT NULL,
    direction           TEXT NOT NULL,
    score               NUMERIC NOT NULL,
    -- entry / exit
    entry_price         NUMERIC NOT NULL,
    entry_fee           NUMERIC NOT NULL DEFAULT 0,
    stop_price          NUMERIC NOT NULL,
    target_1            NUMERIC NOT NULL,
    target_2            NUMERIC,
    entry_timeframe     TEXT NOT NULL DEFAULT '5m',
    position_size       NUMERIC NOT NULL,
    risk_usdt           NUMERIC NOT NULL,
    -- exit (NULL → still open)
    exit_price          NUMERIC,
    exit_reason         TEXT,
    exit_fee            NUMERIC NOT NULL DEFAULT 0,
    -- P&L
    gross_pnl           NUMERIC NOT NULL DEFAULT 0,
    pnl_usdt            NUMERIC NOT NULL DEFAULT 0,
    pnl_r               NUMERIC NOT NULL DEFAULT 0,
    pnl_percent         NUMERIC NOT NULL DEFAULT 0,
    slippage            NUMERIC NOT NULL DEFAULT 0,
    entry_market_price  NUMERIC,
    mfe                 NUMERIC NOT NULL DEFAULT 0,
    mae                 NUMERIC NOT NULL DEFAULT 0,
    mfe_r               NUMERIC NOT NULL DEFAULT 0,
    mae_r               NUMERIC NOT NULL DEFAULT 0,
    funding_paid        NUMERIC NOT NULL DEFAULT 0,
    -- status & timestamps
    status              TEXT NOT NULL DEFAULT 'PENDING',
    entered_at          TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    duration_sec        NUMERIC,
    -- metadata
    market_regime       TEXT,
    balance_before      NUMERIC NOT NULL DEFAULT 0,
    balance_after       NUMERIC NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shadow_trade_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT shadow_trade_status_chk CHECK (
        status IN ('PENDING', 'OPEN', 'CLOSED', 'EXPIRED', 'CANCELLED')
    ),
    CONSTRAINT shadow_trade_exit_reason_chk CHECK (
        exit_reason IS NULL OR exit_reason IN (
            'TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_SLIPPAGE',
            'STOP_LOSS', 'STOP_LOSS_GAP', 'TRAILING_STOP',
            'EXPIRED', 'EXPIRED_PROFITABLE', 'TIMEOUT', 'MANUAL', 'RISK_LIMIT',
            'DCA_BREAKEVEN', 'DCA_STOP', 'FIXED_HORIZON'
        )
    )
);

-- Indexes for paired analytics and lifecycle
CREATE INDEX IF NOT EXISTS idx_shadow_trade_experiment ON dds.paper_shadow_trade (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_trade_source ON dds.paper_shadow_trade (source_setup_id) WHERE source_setup_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_shadow_trade_source_trade ON dds.paper_shadow_trade (source_trade_id) WHERE source_trade_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_shadow_trade_symbol ON dds.paper_shadow_trade (symbol, status);
CREATE INDEX IF NOT EXISTS idx_shadow_trade_status ON dds.paper_shadow_trade (status);
CREATE INDEX IF NOT EXISTS idx_shadow_trade_entered ON dds.paper_shadow_trade (entered_at DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_trade_scanner ON dds.paper_shadow_trade (scanner_name);

-- Unique: one shadow trade per experiment per source setup
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_trade_experiment_setup
ON dds.paper_shadow_trade (experiment_id, source_setup_id)
WHERE source_setup_id IS NOT NULL;

-- ============================================================
-- Paired analytics view: original ME SHORT vs reverse LONG
-- ============================================================
CREATE OR REPLACE VIEW dds.v_me_short_reverse_paired AS
SELECT
    orig.trade_id AS original_trade_id,
    orig.symbol,
    orig.scanner_name AS original_scanner,
    orig.direction AS original_direction,
    orig.entry_price AS original_entry,
    orig.exit_price AS original_exit,
    orig.exit_reason AS original_exit_reason,
    orig.pnl_usdt AS original_pnl_usdt,
    orig.pnl_r AS original_pnl_r,
    orig.entered_at AS original_entered_at,
    orig.closed_at AS original_closed_at,
    rev.shadow_trade_id AS reverse_trade_id,
    rev.scanner_name AS reverse_scanner,
    rev.direction AS reverse_direction,
    rev.entry_price AS reverse_entry,
    rev.stop_price AS reverse_stop,
    rev.target_1 AS reverse_target,
    rev.exit_price AS reverse_exit,
    rev.exit_reason AS reverse_exit_reason,
    rev.pnl_usdt AS reverse_pnl_usdt,
    rev.pnl_r AS reverse_pnl_r,
    rev.entered_at AS reverse_entered_at,
    rev.closed_at AS reverse_closed_at,
    rev.pnl_r - orig.pnl_r AS delta_r
FROM dds.paper_trade orig
JOIN dds.paper_shadow_trade rev
    ON rev.source_trade_id = orig.trade_id
WHERE orig.scanner_name = 'MOMENTUM_EXHAUSTION'
  AND orig.direction = 'SHORT'
  AND rev.experiment_id = 'ME_SHORT_REVERSE_LONG_V1';
