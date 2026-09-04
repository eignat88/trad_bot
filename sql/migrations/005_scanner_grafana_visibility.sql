-- 005_scanner_grafana_visibility.sql
-- Adds per-scanner Grafana visibility toggle.
-- show_in_grafana = TRUE  → scanner appears in all Grafana dashboards (default)
-- show_in_grafana = FALSE → scanner is hidden from all Grafana dashboards
--
-- The scanner continues operating normally and writing data to dds.* tables.
-- Only the Grafana display layer is affected.

-- 1. Add column to config.scanner_direction_gate
ALTER TABLE config.scanner_direction_gate
    ADD COLUMN IF NOT EXISTS show_in_grafana BOOLEAN DEFAULT TRUE;

COMMENT ON COLUMN config.scanner_direction_gate.show_in_grafana IS
    'When FALSE the scanner is hidden from all Grafana dashboards. '
    'The scanner still runs and writes data; only display is suppressed.';

-- 2. Helper function: single call usable in any mart view
CREATE OR REPLACE FUNCTION config.is_scanner_visible(p_scanner_name TEXT)
RETURNS BOOLEAN
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT COALESCE(
        (SELECT g.show_in_grafana
         FROM config.scanner_direction_gate g
         WHERE g.scanner_name = p_scanner_name
         LIMIT 1),
        TRUE  -- unknown scanners are visible by default
    );
$$;

COMMENT ON FUNCTION config.is_scanner_visible IS
    'Returns TRUE if the given scanner should be displayed in Grafana. '
    'Defaults to TRUE for scanners not present in the gate table.';
