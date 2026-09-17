-- Migration 035: Research foundation (Stage 4 PR1)
-- Creates the `research` schema and all core tables for the research pipeline:
-- findings, hypotheses, experiments, validation, change management, monitoring,
-- transition history, and fingerprinting.
--
-- Idempotent: all CREATE TABLE IF NOT EXISTS; DO $$ blocks for triggers.
-- Schema: research (NOT analytics).

-- ============================================================
-- 0. Schema
-- ============================================================
CREATE SCHEMA IF NOT EXISTS research;

-- ============================================================
-- 0.1 Helper functions (idempotent, research-scoped)
-- ============================================================
CREATE OR REPLACE FUNCTION research.fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION research.fn_block_transition_history_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Immutability violation: % on research.transition_history is not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 1. research.finding
-- ============================================================
CREATE TABLE IF NOT EXISTS research.finding (
    finding_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_type       TEXT NOT NULL CHECK (finding_type IN (
                           'ENTRY','DCA','STOP','EXIT','FUNNEL','DRIFT','DATA','INCIDENT'
                       )),
    title              TEXT NOT NULL,
    fingerprint        TEXT,  -- nullable for PR1, populated by PR2
    scope_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrence_count   BIGINT NOT NULL DEFAULT 1,
    status             TEXT NOT NULL DEFAULT 'OPEN'
                       CHECK (status IN ('OPEN','REPEATED','RESEARCH_REQUIRED','CLOSED')),
    confidence         TEXT NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
    evidence_summary   JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_run_id      UUID REFERENCES analytics.analysis_run(run_id),
    agent_name         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_finding_status        ON research.finding (status);
CREATE INDEX IF NOT EXISTS idx_finding_type          ON research.finding (finding_type);
CREATE INDEX IF NOT EXISTS idx_finding_confidence    ON research.finding (confidence);
CREATE INDEX IF NOT EXISTS idx_finding_source_run    ON research.finding (source_run_id);
CREATE INDEX IF NOT EXISTS idx_finding_agent         ON research.finding (agent_name);
CREATE INDEX IF NOT EXISTS idx_finding_first_seen    ON research.finding (first_seen);
CREATE INDEX IF NOT EXISTS idx_finding_last_seen     ON research.finding (last_seen);

COMMENT ON TABLE  research.finding
    IS 'Validated observations from Stage 3 agents that warrant tracking (migration 035)';
COMMENT ON COLUMN research.finding.finding_type
    IS 'Category: ENTRY, DCA, STOP, EXIT, FUNNEL, DRIFT, DATA, INCIDENT';
COMMENT ON COLUMN research.finding.fingerprint
    IS 'Dedup fingerprint hash — nullable for PR1, populated by PR2';
COMMENT ON COLUMN research.finding.scope_json
    IS 'JSONB bag defining the scope/symbol/timeframe this finding applies to';
COMMENT ON COLUMN research.finding.occurrence_count
    IS 'Running total of how many times this finding has been observed';
COMMENT ON COLUMN research.finding.evidence_summary
    IS 'Aggregated evidence references for the finding';

-- ============================================================
-- 2. research.finding_occurrence
-- ============================================================
CREATE TABLE IF NOT EXISTS research.finding_occurrence (
    occurrence_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id         UUID NOT NULL REFERENCES research.finding(finding_id),
    analysis_run_id    UUID REFERENCES analytics.analysis_run(run_id),
    observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_value       NUMERIC,
    sample_size        BIGINT DEFAULT 0,
    confidence         TEXT CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
    evidence_refs      JSONB NOT NULL DEFAULT '[]'::jsonb,
    dataset_version    TEXT,
    details_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (finding_id, analysis_run_id)
);

CREATE INDEX IF NOT EXISTS idx_fo_finding        ON research.finding_occurrence (finding_id);
CREATE INDEX IF NOT EXISTS idx_fo_run            ON research.finding_occurrence (analysis_run_id);
CREATE INDEX IF NOT EXISTS idx_fo_observed_at    ON research.finding_occurrence (observed_at);

COMMENT ON TABLE  research.finding_occurrence
    IS 'Each observed instance of a finding across runs and dates (migration 035)';
COMMENT ON COLUMN research.finding_occurrence.metric_value
    IS 'Key metric value captured at the time of this occurrence';
COMMENT ON COLUMN research.finding_occurrence.dataset_version
    IS 'Version/tag of the dataset used for this occurrence';

-- ============================================================
-- 3. research.hypothesis
-- ============================================================
CREATE TABLE IF NOT EXISTS research.hypothesis (
    hypothesis_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    statement                TEXT NOT NULL,
    falsification_criterion  TEXT NOT NULL,
    population_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
    intervention_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    baseline_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
    primary_metric           TEXT NOT NULL,
    guardrails_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
    minimum_sample           BIGINT DEFAULT 0,
    status                   TEXT NOT NULL DEFAULT 'DRAFT'
                             CHECK (status IN (
                                 'DRAFT','RESEARCH_REQUIRED','EXPERIMENT_DESIGNED',
                                 'BACKTESTING','OOS_VALIDATION','VALIDATED',
                                 'REJECTED','INCONCLUSIVE'
                             )),
    version                  INT NOT NULL DEFAULT 1,
    source_run_id            UUID REFERENCES analytics.analysis_run(run_id),
    agent_name               TEXT NOT NULL,
    evidence_refs            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hypothesis_status   ON research.hypothesis (status);
CREATE INDEX IF NOT EXISTS idx_hypothesis_agent    ON research.hypothesis (agent_name);
CREATE INDEX IF NOT EXISTS idx_hypothesis_version  ON research.hypothesis (version);
CREATE INDEX IF NOT EXISTS idx_hypothesis_source   ON research.hypothesis (source_run_id);

COMMENT ON TABLE  research.hypothesis
    IS 'Hypotheses generated by Stage 3 agents, linked to findings via junction table (migration 035)';
COMMENT ON COLUMN research.hypothesis.falsification_criterion
    IS 'Criterion under which this hypothesis must be rejected';
COMMENT ON COLUMN research.hypothesis.population_json
    IS 'Population definition (symbols, timeframes, regimes)';
COMMENT ON COLUMN research.hypothesis.intervention_json
    IS 'Proposed intervention or parameter change';
COMMENT ON COLUMN research.hypothesis.baseline_json
    IS 'Current baseline values for comparison';
COMMENT ON COLUMN research.hypothesis.primary_metric
    IS 'Name of the primary metric used for validation';
COMMENT ON COLUMN research.hypothesis.guardrails_json
    IS 'Guardrails that must remain satisfied during experimentation';
COMMENT ON COLUMN research.hypothesis.version
    IS 'Version counter for hypothesis iterations';

-- ============================================================
-- 4. research.hypothesis_finding (junction table)
-- ============================================================
CREATE TABLE IF NOT EXISTS research.hypothesis_finding (
    hypothesis_id  UUID NOT NULL REFERENCES research.hypothesis(hypothesis_id),
    finding_id     UUID NOT NULL REFERENCES research.finding(finding_id),
    PRIMARY KEY (hypothesis_id, finding_id)
);

CREATE INDEX IF NOT EXISTS idx_hf_finding ON research.hypothesis_finding (finding_id);

COMMENT ON TABLE research.hypothesis_finding
    IS 'Many-to-many junction: findings that support or relate to hypotheses (migration 035)';

-- ============================================================
-- 5. research.experiment
-- ============================================================
CREATE TABLE IF NOT EXISTS research.experiment (
    experiment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis_id       UUID REFERENCES research.hypothesis(hypothesis_id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    protocol_version    TEXT NOT NULL DEFAULT '1.0',
    protocol_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at           TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'PROPOSED'
                        CHECK (status IN ('PROPOSED','FROZEN','RUNNING','COMPLETED','CANCELLED')),
    source_run_id       UUID REFERENCES analytics.analysis_run(run_id),
    agent_name          TEXT NOT NULL,
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_experiment_status      ON research.experiment (status);
CREATE INDEX IF NOT EXISTS idx_experiment_hypothesis  ON research.experiment (hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_experiment_agent       ON research.experiment (agent_name);
CREATE INDEX IF NOT EXISTS idx_experiment_source      ON research.experiment (source_run_id);

COMMENT ON TABLE  research.experiment
    IS 'Proposed and tracked experiments originating from hypotheses (migration 035)';
COMMENT ON COLUMN research.experiment.protocol_version
    IS 'Semver of the experiment protocol used';
COMMENT ON COLUMN research.experiment.protocol_json
    IS 'Full experiment protocol specification as JSONB';
COMMENT ON COLUMN research.experiment.frozen_at
    IS 'Timestamp when the protocol was frozen (no further changes)';

-- ============================================================
-- 6. research.experiment_run
-- ============================================================
CREATE TABLE IF NOT EXISTS research.experiment_run (
    experiment_run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_id           UUID NOT NULL REFERENCES research.experiment(experiment_id),
    dataset_version         TEXT NOT NULL,
    trad_bot_commit_sha     TEXT,
    backtest_commit_sha     TEXT,
    status                  TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    artifact_location       TEXT,
    reproducibility_command TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_er_experiment  ON research.experiment_run (experiment_id);
CREATE INDEX IF NOT EXISTS idx_er_status      ON research.experiment_run (status);
CREATE INDEX IF NOT EXISTS idx_er_dataset     ON research.experiment_run (dataset_version);

COMMENT ON TABLE  research.experiment_run
    IS 'Individual execution of an experiment (one per dataset/run) (migration 035)';
COMMENT ON COLUMN research.experiment_run.trad_bot_commit_sha
    IS 'Git commit SHA of trad_bot at run time';
COMMENT ON COLUMN research.experiment_run.backtest_commit_sha
    IS 'Git commit SHA of backtest framework at run time';
COMMENT ON COLUMN research.experiment_run.artifact_location
    IS 'Path or URI to stored run artifacts';
COMMENT ON COLUMN research.experiment_run.reproducibility_command
    IS 'Exact command to reproduce this experiment run';

-- ============================================================
-- 7. research.validation_result
-- ============================================================
CREATE TABLE IF NOT EXISTS research.validation_result (
    validation_result_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    experiment_run_id      UUID NOT NULL REFERENCES research.experiment_run(experiment_run_id),
    split                  TEXT NOT NULL CHECK (split IN ('TRAIN','VALIDATION','OOS','ROBUSTNESS')),
    segment_type           TEXT,
    segment_value          TEXT,
    sample_size            BIGINT DEFAULT 0,
    primary_metric_value   NUMERIC,
    metrics_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
    verdict                TEXT NOT NULL DEFAULT 'INCONCLUSIVE'
                           CHECK (verdict IN ('VALIDATED','REJECTED','INCONCLUSIVE')),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vr_run      ON research.validation_result (experiment_run_id);
CREATE INDEX IF NOT EXISTS idx_vr_split    ON research.validation_result (split);
CREATE INDEX IF NOT EXISTS idx_vr_verdict  ON research.validation_result (verdict);

COMMENT ON TABLE  research.validation_result
    IS 'Per-split validation outcome of an experiment run (migration 035)';
COMMENT ON COLUMN research.validation_result.split
    IS 'Data split: TRAIN, VALIDATION, OOS, or ROBUSTNESS';
COMMENT ON COLUMN research.validation_result.segment_type
    IS 'Optional segment dimension (e.g. regime, timeframe)';
COMMENT ON COLUMN research.validation_result.segment_value
    IS 'Value within the segment dimension';
COMMENT ON COLUMN research.validation_result.metrics_json
    IS 'Full set of computed metrics for this split/segment';

-- ============================================================
-- 8. research.change_candidate
-- ============================================================
CREATE TABLE IF NOT EXISTS research.change_candidate (
    candidate_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis_id         UUID REFERENCES research.hypothesis(hypothesis_id),
    experiment_id         UUID REFERENCES research.experiment(experiment_id),
    validation_result_id  UUID REFERENCES research.validation_result(validation_result_id),
    risk_assessment_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                TEXT NOT NULL DEFAULT 'PROPOSED'
                          CHECK (status IN ('PROPOSED','APPROVED','REJECTED','IMPLEMENTED')),
    approved_by           TEXT,
    approved_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cc_status         ON research.change_candidate (status);
CREATE INDEX IF NOT EXISTS idx_cc_hypothesis     ON research.change_candidate (hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_cc_experiment     ON research.change_candidate (experiment_id);
CREATE INDEX IF NOT EXISTS idx_cc_validation     ON research.change_candidate (validation_result_id);

COMMENT ON TABLE  research.change_candidate
    IS 'Proposed production changes derived from validated experiments (migration 035)';
COMMENT ON COLUMN research.change_candidate.risk_assessment_json
    IS 'Risk evaluation for this candidate change';
COMMENT ON COLUMN research.change_candidate.approved_by
    IS 'Human or agent that approved this candidate';

-- ============================================================
-- 9. research.production_change
-- ============================================================
CREATE TABLE IF NOT EXISTS research.production_change (
    change_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id           UUID REFERENCES research.change_candidate(candidate_id),
    branch                 TEXT,
    pr_number              BIGINT,
    commit_sha             TEXT,
    deployment_at          TIMESTAMPTZ,
    config_snapshot_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    affected_scanners_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    deployed_by            TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pc_candidate    ON research.production_change (candidate_id);
CREATE INDEX IF NOT EXISTS idx_pc_branch       ON research.production_change (branch);
CREATE INDEX IF NOT EXISTS idx_pc_commit       ON research.production_change (commit_sha);
CREATE INDEX IF NOT EXISTS idx_pc_deployed_at  ON research.production_change (deployment_at);

COMMENT ON TABLE  research.production_change
    IS 'Records of changes deployed to production (migration 035)';
COMMENT ON COLUMN research.production_change.config_snapshot_json
    IS 'Configuration snapshot at deployment time';
COMMENT ON COLUMN research.production_change.affected_scanners_json
    IS 'List of scanners affected by this change';

-- ============================================================
-- 10. research.monitoring_result
-- ============================================================
CREATE TABLE IF NOT EXISTS research.monitoring_result (
    monitoring_result_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_id               UUID REFERENCES research.production_change(change_id),
    window_label           TEXT NOT NULL,
    observed_from           TIMESTAMPTZ NOT NULL,
    observed_to             TIMESTAMPTZ NOT NULL,
    sample_size             BIGINT DEFAULT 0,
    primary_metric_actual   NUMERIC,
    expected_metric         NUMERIC,
    guardrails_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
    verdict                 TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (verdict IN ('PENDING','PASS','FAIL','INCONCLUSIVE')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mr_change       ON research.monitoring_result (change_id);
CREATE INDEX IF NOT EXISTS idx_mr_verdict      ON research.monitoring_result (verdict);
CREATE INDEX IF NOT EXISTS idx_mr_window       ON research.monitoring_result (window_label);
CREATE INDEX IF NOT EXISTS idx_mr_observed     ON research.monitoring_result (observed_from, observed_to);

COMMENT ON TABLE  research.monitoring_result
    IS 'Post-deployment monitoring outcomes for production changes (migration 035)';
COMMENT ON COLUMN research.monitoring_result.window_label
    IS 'Monitoring window label (e.g. 1h, 4h, 24h, 7d)';
COMMENT ON COLUMN research.monitoring_result.primary_metric_actual
    IS 'Observed value of the primary metric in this window';
COMMENT ON COLUMN research.monitoring_result.expected_metric
    IS 'Expected/target value for the primary metric';
COMMENT ON COLUMN research.monitoring_result.guardrails_json
    IS 'Guardrail evaluations for this monitoring window';

-- ============================================================
-- 11. research.transition_history (append-only)
-- ============================================================
CREATE TABLE IF NOT EXISTS research.transition_history (
    transition_id      BIGSERIAL PRIMARY KEY,
    entity_type        TEXT NOT NULL CHECK (entity_type IN (
                           'finding','hypothesis','experiment','experiment_run',
                           'validation_result','change_candidate',
                           'production_change','monitoring_result'
                       )),
    entity_id          UUID NOT NULL,
    from_status        TEXT,
    to_status          TEXT NOT NULL,
    actor              TEXT NOT NULL DEFAULT 'system',
    reason             TEXT,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transition_entity    ON research.transition_history (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_transition_created   ON research.transition_history (created_at);
CREATE INDEX IF NOT EXISTS idx_transition_to_status ON research.transition_history (to_status);

COMMENT ON TABLE  research.transition_history
    IS 'Append-only audit trail for all research entity state changes (migration 035)';
COMMENT ON COLUMN research.transition_history.entity_type
    IS 'Discriminator: finding, hypothesis, experiment, experiment_run, validation_result, change_candidate, production_change, monitoring_result';

-- ============================================================
-- 12. research.fingerprint (foundation, PR1 schema; PR2 populates)
-- ============================================================
CREATE TABLE IF NOT EXISTS research.fingerprint (
    fingerprint_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id         UUID NOT NULL REFERENCES research.finding(finding_id),
    fingerprint_hash   TEXT NOT NULL,
    finding_type       TEXT NOT NULL,
    scanner_name       TEXT,
    direction          TEXT,
    normalized_segment TEXT,
    metric_name        TEXT,
    comparator         TEXT,
    threshold_policy_version TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (fingerprint_hash)
);

CREATE INDEX IF NOT EXISTS idx_fingerprint_finding   ON research.fingerprint (finding_id);
CREATE INDEX IF NOT EXISTS idx_fingerprint_hash      ON research.fingerprint (fingerprint_hash);
CREATE INDEX IF NOT EXISTS idx_fingerprint_type      ON research.fingerprint (finding_type);
CREATE INDEX IF NOT EXISTS idx_fingerprint_scanner   ON research.fingerprint (scanner_name);

COMMENT ON TABLE  research.fingerprint
    IS 'Finding fingerprints for deduplication — schema laid in PR1, populated by PR2 (migration 035)';
COMMENT ON COLUMN research.fingerprint.fingerprint_hash
    IS 'Unique content hash used for dedup';
COMMENT ON COLUMN research.fingerprint.finding_type
    IS 'Matches the finding_type from research.finding';
COMMENT ON COLUMN research.fingerprint.scanner_name
    IS 'Name of the scanner that produced this fingerprint';
COMMENT ON COLUMN research.fingerprint.direction
    IS 'LONG or SHORT';
COMMENT ON COLUMN research.fingerprint.normalized_segment
    IS 'Normalized segment key for fingerprint grouping';
COMMENT ON COLUMN research.fingerprint.comparator
    IS 'Comparison operator used in the threshold check';
COMMENT ON COLUMN research.fingerprint.threshold_policy_version
    IS 'Version of the threshold policy applied';

-- ============================================================
-- 13. Triggers
-- ============================================================

-- 13a. updated_at auto-touch for finding, hypothesis, experiment
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_finding_updated_at'
    ) THEN
        CREATE TRIGGER trg_finding_updated_at
            BEFORE UPDATE ON research.finding
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_hypothesis_updated_at'
    ) THEN
        CREATE TRIGGER trg_hypothesis_updated_at
            BEFORE UPDATE ON research.hypothesis
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_experiment_updated_at'
    ) THEN
        CREATE TRIGGER trg_experiment_updated_at
            BEFORE UPDATE ON research.experiment
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END
$$;

-- 13b. Immutability guard on transition_history (block UPDATE/DELETE)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_transition_history_immutability'
    ) THEN
        CREATE TRIGGER trg_transition_history_immutability
            BEFORE UPDATE OR DELETE ON research.transition_history
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_block_transition_history_mutation();
    END IF;
END
$$;
