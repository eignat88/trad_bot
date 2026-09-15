-- Migration 018: Setup Fact Build
-- Populates analytics.setup_fact from dds.scanner_setup.
--
-- Each row represents a setup within a specific analytics run context.
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.

INSERT INTO analytics.setup_fact (
    run_id, setup_id, scanner_name, scanner_version, symbol, direction,
    setup_at, signal_at, setup_status,
    entry_zone_low, entry_zone_high, reference_price,
    config_hash, strategy_hash, market_regime,
    coverage_status, ambiguity_status, excluded_reasons,
    dataset_version, published, created_at
)
WITH
latest_run AS (
    SELECT run_id, observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    lr.run_id,
    ss.setup_id,
    ss.scanner_name,
    ss.scanner_version,
    i.symbol,
    ss.direction,
    ss.setup_started_at AS setup_at,
    ss.detected_at AS signal_at,
    ss.status AS setup_status,
    ss.entry_zone_low,
    ss.entry_zone_high,
    ss.reference_price,
    NULL AS config_hash,    -- populated in migration 019
    NULL AS strategy_hash,  -- populated in migration 019
    ss.market_regime,
    'UNKNOWN' AS coverage_status,
    'CLEAR' AS ambiguity_status,
    '[]'::jsonb AS excluded_reasons,
    '00000000.0' AS dataset_version,
    FALSE AS published,
    NOW() AS created_at
FROM dds.scanner_setup ss
JOIN dds.instrument i ON i.instrument_id = ss.instrument_id
CROSS JOIN latest_run lr
WHERE ss.detected_at < lr.observation_cutoff
ON CONFLICT (run_id, setup_id) DO UPDATE SET
    scanner_name = EXCLUDED.scanner_name,
    scanner_version = EXCLUDED.scanner_version,
    symbol = EXCLUDED.symbol,
    direction = EXCLUDED.direction,
    setup_at = EXCLUDED.setup_at,
    signal_at = EXCLUDED.signal_at,
    setup_status = EXCLUDED.setup_status,
    entry_zone_low = EXCLUDED.entry_zone_low,
    entry_zone_high = EXCLUDED.entry_zone_high,
    reference_price = EXCLUDED.reference_price,
    market_regime = EXCLUDED.market_regime;
