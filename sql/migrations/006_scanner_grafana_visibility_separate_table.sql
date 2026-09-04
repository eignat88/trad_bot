-- 006_scanner_grafana_visibility_separate_table.sql
-- Creates a dedicated table for Grafana visibility control, independent of runtime config.
--
-- Problem: config.scanner_direction_gate only contains currently running scanners.
-- Historical scanners (e.g. TREND_PULLBACK) that were removed from runtime config
-- still have trades in DB, and config.is_scanner_visible() defaults to TRUE for them.
--
-- Solution: config.scanner_grafana_visibility stores per-scanner visibility overrides.
-- config.is_scanner_visible() checks this table FIRST, then falls back to TRUE.

-- 1. Create dedicated visibility table
CREATE TABLE IF NOT EXISTS config.scanner_grafana_visibility (
    scanner_name TEXT PRIMARY KEY,
    show_in_grafana BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

COMMENT ON TABLE config.scanner_grafana_visibility IS
    'Per-scanner Grafana visibility toggle. Independent of runtime config. '
    'When show_in_grafana=FALSE the scanner is hidden from all Grafana dashboards. '
    'The scanner still runs and writes data; only display is suppressed.';

COMMENT ON COLUMN config.scanner_grafana_visibility.show_in_grafana IS
    'TRUE = visible in Grafana (default). FALSE = hidden from all Grafana dashboards.';

COMMENT ON COLUMN config.scanner_grafana_visibility.updated_by IS
    'Who last changed this setting: MANUAL, MIGRATION, API, etc.';

-- 2. Migrate existing show_in_grafana values from scanner_direction_gate
--    Only migrate rows where show_in_grafana has been explicitly set to FALSE
--    (TRUE is the default, so no need to migrate those).
INSERT INTO config.scanner_grafana_visibility (scanner_name, show_in_grafana, updated_by)
SELECT DISTINCT ON (scanner_name)
    scanner_name,
    show_in_grafana,
    'MIGRATION_006'
FROM config.scanner_direction_gate
WHERE show_in_grafana = FALSE
ON CONFLICT (scanner_name) DO NOTHING;

-- 3. Update is_scanner_visible() to check the new table FIRST
CREATE OR REPLACE FUNCTION config.is_scanner_visible(p_scanner_name TEXT)
RETURNS BOOLEAN
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT COALESCE(
        -- First: check dedicated visibility table (for historical scanners)
        (SELECT v.show_in_grafana
         FROM config.scanner_grafana_visibility v
         WHERE v.scanner_name = p_scanner_name),
        -- Second: check runtime gate table (for currently running scanners)
        (SELECT g.show_in_grafana
         FROM config.scanner_direction_gate g
         WHERE g.scanner_name = p_scanner_name
         LIMIT 1),
        TRUE  -- unknown scanners are visible by default
    );
$$;

COMMENT ON FUNCTION config.is_scanner_visible IS
    'Returns TRUE if the given scanner should be displayed in Grafana. '
    'Checks scanner_grafana_visibility first (historical scanners), '
    'then scanner_direction_gate (runtime scanners). '
    'Defaults to TRUE for scanners not found in either table.';
