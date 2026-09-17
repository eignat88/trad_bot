-- Migration 038: Add execution policy fields to paper_trade
-- Supports ME SHORT FIXED_240M_V1 execution policy
-- Backward compatible: defaults preserve existing behavior

-- Add execution_policy column (default = DEFAULT for all existing trades)
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS execution_policy TEXT NOT NULL DEFAULT 'DEFAULT';

-- Add planned_exit_at for fixed-horizon time exits
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS planned_exit_at TIMESTAMPTZ;

-- Index for efficient time-exit queries
CREATE INDEX IF NOT EXISTS idx_paper_trade_planned_exit
    ON dds.paper_trade (planned_exit_at)
    WHERE planned_exit_at IS NOT NULL AND status = 'OPEN';

-- Index for policy-based queries
CREATE INDEX IF NOT EXISTS idx_paper_trade_policy
    ON dds.paper_trade (execution_policy, status);

-- Update CHECK constraint for exit_reason to include FIXED_HORIZON
ALTER TABLE dds.paper_trade DROP CONSTRAINT IF EXISTS paper_trade_exit_reason_chk;
ALTER TABLE dds.paper_trade ADD CONSTRAINT paper_trade_exit_reason_chk CHECK (
    exit_reason IS NULL OR exit_reason IN (
        'TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_SLIPPAGE',
        'STOP_LOSS', 'STOP_LOSS_GAP',
        'TRAILING_STOP',
        'EXPIRED', 'EXPIRED_PROFITABLE', 'TIMEOUT',
        'MANUAL', 'RISK_LIMIT',
        'DCA_BREAKEVEN', 'DCA_STOP',
        'FIXED_HORIZON'
    )
);
