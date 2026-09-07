-- Migration 007: DCA Breakeven — position management policy
-- Adds DCA state columns to paper_trade table (backward-compatible).
-- All columns have safe defaults so existing rows are unaffected.

-- ============================================================
-- DCA columns on dds.paper_trade
-- ============================================================
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_state TEXT;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_data JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ATR frozen at entry
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS atr_at_entry NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_level_atr NUMERIC;

-- DCA price level (pre-calculated at entry)
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_price NUMERIC;

-- Position split tracking
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS initial_fill_price NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS initial_fill_qty NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_fill_price NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_fill_qty NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_filled_at TIMESTAMPTZ;

-- Weighted average after DCA
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS avg_entry_price NUMERIC;

-- TP management (scanner original vs breakeven)
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS original_tp NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS active_tp NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS tp_mode TEXT DEFAULT 'scanner';

-- Position split percentages
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS initial_entry_pct NUMERIC DEFAULT 1.0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_target_pct NUMERIC DEFAULT 0.0;

-- Execution cost per leg
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_fee NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS dca_slippage NUMERIC NOT NULL DEFAULT 0;

-- ============================================================
-- Extend exit_reason CHECK constraint to include DCA reasons
-- ============================================================
ALTER TABLE dds.paper_trade DROP CONSTRAINT IF EXISTS paper_trade_exit_reason_chk;
ALTER TABLE dds.paper_trade ADD CONSTRAINT paper_trade_exit_reason_chk CHECK (
    exit_reason IS NULL OR exit_reason IN (
        'TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_SLIPPAGE',
        'STOP_LOSS', 'STOP_LOSS_GAP', 'TRAILING_STOP',
        'EXPIRED', 'EXPIRED_PROFITABLE', 'TIMEOUT', 'MANUAL', 'RISK_LIMIT',
        'DCA_BREAKEVEN', 'DCA_STOP'
    )
);

-- ============================================================
-- Indexes for DCA monitoring queries
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_paper_trade_dca_state
    ON dds.paper_trade (dca_state) WHERE dca_state IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_trade_dca_enabled
    ON dds.paper_trade (dca_enabled, status) WHERE dca_enabled = TRUE;
