-- Migration 029: Dataset publication tracking
-- Adds dataset_publication (source of truth for dataset versions) and
-- dataset_publication_log (append-only event log).
--
-- Depends on: 008 (update_updated_at_column trigger),
--             028 (analysis_run table)

BEGIN;

-- ============================================================
-- Table: analytics.dataset_publication
-- ============================================================
-- Single source of truth for published dataset versions.
-- A row is inserted when a canonical build completes and quality
-- checks pass.  dataset_version is a deterministic SHA-256 hash
-- of the publication manifest.

CREATE TABLE IF NOT EXISTS analytics.dataset_publication (
    publication_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_id         UUID        NOT NULL
                                REFERENCES analytics.analysis_run(run_id),
    dataset_version         TEXT        NOT NULL,           -- SHA-256 of manifest
    maturity                TEXT        NOT NULL
                                CHECK (maturity IN ('PROVISIONAL', 'FINAL')),
    status                  TEXT        NOT NULL DEFAULT 'BUILDING'
                                CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
    quality_status          TEXT
                                CHECK (quality_status IN ('PASS', 'DEGRADED', 'FAIL')),
    canonical_schema_version TEXT       NOT NULL DEFAULT '1.0',
    analysis_window_from    TIMESTAMPTZ NOT NULL,
    analysis_window_to      TIMESTAMPTZ NOT NULL,
    observation_cutoff      TIMESTAMPTZ NOT NULL,
    canonical_build_json    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    quality_summary_json    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    published_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (analysis_run_id, maturity)
);

COMMENT ON TABLE  analytics.dataset_publication IS
    'Source of truth for published dataset versions.  One row per analysis_run + maturity.';
COMMENT ON COLUMN analytics.dataset_publication.dataset_version IS
    'Deterministic SHA-256 hash of the publication manifest.';
COMMENT ON COLUMN analytics.dataset_publication.maturity IS
    'PROVISIONAL = draft/interim dataset; FINAL = audited and locked.';
COMMENT ON COLUMN analytics.dataset_publication.status IS
    'BUILDING → READY (success) or FAILED.';
COMMENT ON COLUMN analytics.dataset_publication.quality_status IS
    'Aggregate quality-gate result: PASS, DEGRADED, or FAIL.';
COMMENT ON COLUMN analytics.dataset_publication.canonical_build_json IS
    'JSONB summary of the canonical build (tables, row counts, artifacts).';
COMMENT ON COLUMN analytics.dataset_publication.quality_summary_json IS
    'JSONB summary of quality-check results per rule.';

-- updated_at trigger (reuses analytics.update_updated_at_column from 008)
CREATE TRIGGER trg_dataset_publication_updated_at
    BEFORE UPDATE ON analytics.dataset_publication
    FOR EACH ROW
    EXECUTE FUNCTION analytics.update_updated_at_column();

-- Indexes
CREATE INDEX IF NOT EXISTS idx_dataset_publication_status
    ON analytics.dataset_publication (status);

CREATE INDEX IF NOT EXISTS idx_dataset_publication_version
    ON analytics.dataset_publication (dataset_version);

CREATE INDEX IF NOT EXISTS idx_dataset_publication_run
    ON analytics.dataset_publication (analysis_run_id);

-- ============================================================
-- Table: analytics.dataset_publication_log  (append-only)
-- ============================================================
-- Event log for the publication lifecycle.

CREATE TABLE IF NOT EXISTS analytics.dataset_publication_log (
    log_id          BIGSERIAL   PRIMARY KEY,
    publication_id  UUID        NOT NULL
                        REFERENCES analytics.dataset_publication(publication_id),
    event_type      TEXT        NOT NULL,  -- CREATED, QUALITY_CHECKED, PUBLISHED, FAILED
    event_json      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  analytics.dataset_publication_log IS
    'Append-only event log for dataset_publication lifecycle events.';
COMMENT ON COLUMN analytics.dataset_publication_log.event_type IS
    'Event kind: CREATED, QUALITY_CHECKED, PUBLISHED, FAILED.';

CREATE INDEX IF NOT EXISTS idx_dataset_publication_log_pub
    ON analytics.dataset_publication_log (publication_id);

COMMIT;
