-- Issue #292: append-only identity of source gaps + bounded occurrence rollup.
-- Apply as postgres on trad_bot. No trading schema objects modified.
BEGIN;
CREATE TABLE IF NOT EXISTS monitoring.source_gap_audit (
  gap_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  unit_name text NOT NULL,
  cursor_sha256 char(64) NOT NULL,
  failure_kind text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  detection_count bigint NOT NULL DEFAULT 1 CHECK (detection_count >= 1),
  resolution text NOT NULL DEFAULT 'OPEN',
  resolved_at timestamptz,
  CONSTRAINT source_gap_audit_identity UNIQUE (unit_name, cursor_sha256, failure_kind)
);
-- Existing grants are not changed. The observer is only allowed to append/
-- increment its own gap evidence and read it, not to delete or truncate.
GRANT USAGE ON SCHEMA monitoring TO tradbot_monitor;
GRANT SELECT, INSERT, UPDATE ON monitoring.source_gap_audit TO tradbot_monitor;
GRANT USAGE, SELECT ON SEQUENCE monitoring.source_gap_audit_gap_id_seq TO tradbot_monitor;
COMMIT;
