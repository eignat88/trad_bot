-- 007_signal_funnel.sql
-- Conversion funnel: scanned instruments → setups → signals → trades → wins.
-- Uses reliable foreign keys where available (setup_id links scanner_setup ↔ paper_trade).

CREATE OR REPLACE VIEW mart.signal_funnel AS
SELECT
    -- Layer 1: instruments scanned (count distinct instruments per run)
    (SELECT SUM(symbols_scanned) FROM dds.scanner_run)          AS instruments_scanned,

    -- Layer 2: total scanner setups
    (SELECT COUNT(*) FROM dds.scanner_setup ss
     WHERE config.is_scanner_visible(ss.scanner_name))          AS scanner_setups,

    -- Layer 3: executed setups (= READY → actually triggered)
    (SELECT COUNT(*) FROM dds.scanner_setup ss
     WHERE ss.status = 'EXECUTED'
       AND config.is_scanner_visible(ss.scanner_name))          AS ready_setups,

    -- Layer 4: market signals
    (SELECT COUNT(*) FROM dds.market_signal ms
     WHERE config.is_scanner_visible(ms.scanner_name))          AS market_signals,

    -- Layer 5: paper trades
    (SELECT COUNT(*) FROM dds.paper_trade pt
     WHERE config.is_scanner_visible(pt.scanner_name))          AS paper_trades,

    -- Layer 6: closed trades
    (SELECT COUNT(*) FROM dds.paper_trade pt
     WHERE pt.status = 'CLOSED'
       AND config.is_scanner_visible(pt.scanner_name))          AS closed_trades,

    -- Layer 7: winning trades
    (SELECT COUNT(*) FROM dds.paper_trade pt
     WHERE pt.status = 'CLOSED' AND pt.pnl_r > 0
       AND config.is_scanner_visible(pt.scanner_name))          AS winning_trades;
