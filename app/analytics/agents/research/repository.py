"""Repository for research foundation tables (Stage 4 — migration 035).

Covers all 12 tables in the ``research`` schema:
    1. finding
    2. finding_occurrence
    3. hypothesis
    4. hypothesis_finding  (junction)
    5. experiment
    6. experiment_run
    7. validation_result
    8. change_candidate
    9. production_change
   10. monitoring_result
   11. transition_history  (append-only)
   12. fingerprint
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID

from app.analytics.agents.research.models import (
    ChangeCandidate,
    Experiment,
    ExperimentRun,
    Finding,
    FindingOccurrence,
    Fingerprint,
    Hypothesis,
    HypothesisFinding,
    MonitoringResult,
    ProductionChange,
    TransitionRecord,
    ValidationResult,
)
from app.analytics.agents.research.transition_policy import ResearchTransitionPolicyV1

logger = logging.getLogger(__name__)


class ResearchRepository:
    """CRUD repository for all research.* tables."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint_hash(
        finding_type: str,
        scanner_name: str,
        direction: str,
        metric_name: str,
    ) -> str:
        """Deterministic SHA-256 of the normalised fingerprint components."""
        raw = f"{finding_type}|{scanner_name}|{direction}|{metric_name}".lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ==================================================================
    # 1. finding
    # ==================================================================

    def create_finding(self, finding: Finding) -> Finding:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.finding (
                    finding_id, finding_type, title, fingerprint,
                    scope_json, first_seen, last_seen, occurrence_count,
                    status, confidence, evidence_summary,
                    source_run_id, agent_name, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(finding.finding_id),
                    finding.finding_type,
                    finding.title,
                    finding.fingerprint,
                    json.dumps(finding.scope_json),
                    finding.first_seen,
                    finding.last_seen,
                    finding.occurrence_count,
                    finding.status,
                    finding.confidence,
                    json.dumps(finding.evidence_summary),
                    str(finding.source_run_id) if finding.source_run_id else None,
                    finding.agent_name,
                    finding.created_at,
                    finding.updated_at,
                ),
            )
            self._conn.commit()
            logger.info("Created finding %s (%s)", finding.finding_id, finding.finding_type)
            return finding
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create finding: %s", exc)
            raise

    def get_finding(self, finding_id: UUID) -> Optional[Finding]:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM research.finding WHERE finding_id = %s", (str(finding_id),))
            row = cur.fetchone()
            return self._row_to_finding(row) if row else None
        except Exception as exc:
            logger.error("Failed to get finding: %s", exc)
            raise

    def update_finding_status(
        self,
        finding_id: UUID,
        new_status: str,
        reason: str = "",
        actor: str = "system",
    ) -> None:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT status FROM research.finding WHERE finding_id = %s", (str(finding_id),))
            row = cur.fetchone()
            old_status = row[0] if row else None

            if old_status is None:
                raise ValueError(f"Finding {finding_id} not found")

            # Validate transition policy BEFORE any writes
            ResearchTransitionPolicyV1.validate("finding", old_status, new_status)

            cur.execute(
                "UPDATE research.finding SET status = %s, updated_at = NOW() WHERE finding_id = %s",
                (new_status, str(finding_id)),
            )
            self._record_transition(cur, "finding", finding_id, old_status, new_status, actor, reason)
            self._conn.commit()
            logger.info("Finding %s status: %s -> %s", finding_id, old_status, new_status)
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update finding status: %s", exc)
            raise

    def list_findings(
        self,
        status: Optional[str] = None,
        finding_type: Optional[str] = None,
        source_run_id: Optional[UUID] = None,
    ) -> list[Finding]:
        try:
            cur = self._conn.cursor()
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = %s")
                params.append(status)
            if finding_type is not None:
                clauses.append("finding_type = %s")
                params.append(finding_type)
            if source_run_id is not None:
                clauses.append("source_run_id = %s")
                params.append(str(source_run_id))

            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT * FROM research.finding{where} ORDER BY created_at DESC", params)
            return [self._row_to_finding(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list findings: %s", exc)
            raise

    # ==================================================================
    # 2. finding_occurrence
    # ==================================================================

    def create_finding_occurrence(self, occ: FindingOccurrence) -> FindingOccurrence:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.finding_occurrence (
                    occurrence_id, finding_id, analysis_run_id, observed_at,
                    metric_value, sample_size, confidence, evidence_refs,
                    dataset_version, details_json, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (finding_id, analysis_run_id) DO NOTHING
                """,
                (
                    str(occ.occurrence_id),
                    str(occ.finding_id),
                    str(occ.analysis_run_id) if occ.analysis_run_id else None,
                    occ.observed_at,
                    occ.metric_value,
                    occ.sample_size,
                    occ.confidence,
                    json.dumps(occ.evidence_refs),
                    occ.dataset_version,
                    json.dumps(occ.details_json),
                    occ.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created finding_occurrence %s", occ.occurrence_id)
            return occ
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create finding_occurrence: %s", exc)
            raise

    def list_finding_occurrences(self, finding_id: UUID) -> list[FindingOccurrence]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.finding_occurrence WHERE finding_id = %s ORDER BY observed_at ASC",
                (str(finding_id),),
            )
            return [self._row_to_finding_occurrence(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list finding_occurrences: %s", exc)
            raise

    # ==================================================================
    # 3. hypothesis
    # ==================================================================

    def create_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.hypothesis (
                    hypothesis_id, statement, falsification_criterion,
                    population_json, intervention_json, baseline_json,
                    primary_metric, guardrails_json, minimum_sample,
                    status, version, source_run_id, agent_name,
                    evidence_refs, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(hypothesis.hypothesis_id),
                    hypothesis.statement,
                    hypothesis.falsification_criterion,
                    json.dumps(hypothesis.population_json),
                    json.dumps(hypothesis.intervention_json),
                    json.dumps(hypothesis.baseline_json),
                    hypothesis.primary_metric,
                    json.dumps(hypothesis.guardrails_json),
                    hypothesis.minimum_sample,
                    hypothesis.status,
                    hypothesis.version,
                    str(hypothesis.source_run_id) if hypothesis.source_run_id else None,
                    hypothesis.agent_name,
                    json.dumps(hypothesis.evidence_refs),
                    hypothesis.created_at,
                    hypothesis.updated_at,
                ),
            )
            self._conn.commit()
            logger.info("Created hypothesis %s", hypothesis.hypothesis_id)
            return hypothesis
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create hypothesis: %s", exc)
            raise

    def get_hypothesis(self, hypothesis_id: UUID) -> Optional[Hypothesis]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.hypothesis WHERE hypothesis_id = %s",
                (str(hypothesis_id),),
            )
            row = cur.fetchone()
            return self._row_to_hypothesis(row) if row else None
        except Exception as exc:
            logger.error("Failed to get hypothesis: %s", exc)
            raise

    def update_hypothesis_status(
        self,
        hypothesis_id: UUID,
        new_status: str,
        reason: str = "",
        actor: str = "system",
    ) -> None:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT status FROM research.hypothesis WHERE hypothesis_id = %s",
                (str(hypothesis_id),),
            )
            row = cur.fetchone()
            old_status = row[0] if row else None

            if old_status is None:
                raise ValueError(f"Hypothesis {hypothesis_id} not found")

            ResearchTransitionPolicyV1.validate("hypothesis", old_status, new_status)

            cur.execute(
                "UPDATE research.hypothesis SET status = %s, updated_at = NOW() WHERE hypothesis_id = %s",
                (new_status, str(hypothesis_id)),
            )
            self._record_transition(
                cur, "hypothesis", hypothesis_id, old_status, new_status, actor, reason
            )
            self._conn.commit()
            logger.info("Hypothesis %s status: %s -> %s", hypothesis_id, old_status, new_status)
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update hypothesis status: %s", exc)
            raise

    def list_hypotheses(
        self,
        status: Optional[str] = None,
        source_run_id: Optional[UUID] = None,
    ) -> list[Hypothesis]:
        try:
            cur = self._conn.cursor()
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = %s")
                params.append(status)
            if source_run_id is not None:
                clauses.append("source_run_id = %s")
                params.append(str(source_run_id))

            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT * FROM research.hypothesis{where} ORDER BY created_at DESC", params)
            return [self._row_to_hypothesis(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list hypotheses: %s", exc)
            raise

    # ==================================================================
    # 4. hypothesis_finding (junction)
    # ==================================================================

    def link_hypothesis_finding(self, hypothesis_id: UUID, finding_id: UUID) -> None:
        """Create a many-to-many link between a hypothesis and a finding."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.hypothesis_finding (hypothesis_id, finding_id)
                VALUES (%s, %s)
                ON CONFLICT (hypothesis_id, finding_id) DO NOTHING
                """,
                (str(hypothesis_id), str(finding_id)),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to link hypothesis_finding: %s", exc)
            raise

    def unlink_hypothesis_finding(self, hypothesis_id: UUID, finding_id: UUID) -> None:
        """Remove a many-to-many link."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM research.hypothesis_finding WHERE hypothesis_id = %s AND finding_id = %s",
                (str(hypothesis_id), str(finding_id)),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to unlink hypothesis_finding: %s", exc)
            raise

    def get_hypothesis_findings(self, hypothesis_id: UUID) -> list[UUID]:
        """Return all finding_ids linked to a hypothesis."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT finding_id FROM research.hypothesis_finding WHERE hypothesis_id = %s",
                (str(hypothesis_id),),
            )
            return [UUID(r[0]) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to get hypothesis_findings: %s", exc)
            raise

    def get_finding_hypotheses(self, finding_id: UUID) -> list[UUID]:
        """Return all hypothesis_ids linked to a finding."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT hypothesis_id FROM research.hypothesis_finding WHERE finding_id = %s",
                (str(finding_id),),
            )
            return [UUID(r[0]) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to get finding_hypotheses: %s", exc)
            raise

    # ==================================================================
    # 5. experiment
    # ==================================================================

    def create_experiment(self, experiment: Experiment) -> Experiment:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.experiment (
                    experiment_id, hypothesis_id, title, description,
                    protocol_version, protocol_json, frozen_at, status,
                    source_run_id, agent_name, evidence_refs,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(experiment.experiment_id),
                    str(experiment.hypothesis_id) if experiment.hypothesis_id else None,
                    experiment.title,
                    experiment.description,
                    experiment.protocol_version,
                    json.dumps(experiment.protocol_json),
                    experiment.frozen_at,
                    experiment.status,
                    str(experiment.source_run_id) if experiment.source_run_id else None,
                    experiment.agent_name,
                    json.dumps(experiment.evidence_refs),
                    experiment.created_at,
                    experiment.updated_at,
                ),
            )
            self._conn.commit()
            logger.info("Created experiment %s", experiment.experiment_id)
            return experiment
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create experiment: %s", exc)
            raise

    def get_experiment(self, experiment_id: UUID) -> Optional[Experiment]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.experiment WHERE experiment_id = %s",
                (str(experiment_id),),
            )
            row = cur.fetchone()
            return self._row_to_experiment(row) if row else None
        except Exception as exc:
            logger.error("Failed to get experiment: %s", exc)
            raise

    def update_experiment_status(
        self,
        experiment_id: UUID,
        new_status: str,
        reason: str = "",
        actor: str = "system",
    ) -> None:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT status FROM research.experiment WHERE experiment_id = %s",
                (str(experiment_id),),
            )
            row = cur.fetchone()
            old_status = row[0] if row else None

            if old_status is None:
                raise ValueError(f"Experiment {experiment_id} not found")

            ResearchTransitionPolicyV1.validate("experiment", old_status, new_status)

            cur.execute(
                "UPDATE research.experiment SET status = %s, updated_at = NOW() WHERE experiment_id = %s",
                (new_status, str(experiment_id)),
            )
            self._record_transition(
                cur, "experiment", experiment_id, old_status, new_status, actor, reason
            )
            self._conn.commit()
            logger.info("Experiment %s status: %s -> %s", experiment_id, old_status, new_status)
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update experiment status: %s", exc)
            raise

    def list_experiments(self, status: Optional[str] = None) -> list[Experiment]:
        try:
            cur = self._conn.cursor()
            if status is not None:
                cur.execute(
                    "SELECT * FROM research.experiment WHERE status = %s ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM research.experiment ORDER BY created_at DESC")
            return [self._row_to_experiment(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list experiments: %s", exc)
            raise

    # ==================================================================
    # 6. experiment_run
    # ==================================================================

    def create_experiment_run(self, run: ExperimentRun) -> ExperimentRun:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.experiment_run (
                    experiment_run_id, experiment_id, dataset_version,
                    trad_bot_commit_sha, backtest_commit_sha, status,
                    started_at, finished_at, artifact_location,
                    reproducibility_command, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(run.experiment_run_id),
                    str(run.experiment_id),
                    run.dataset_version,
                    run.trad_bot_commit_sha,
                    run.backtest_commit_sha,
                    run.status,
                    run.started_at,
                    run.finished_at,
                    run.artifact_location,
                    run.reproducibility_command,
                    run.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created experiment_run %s", run.experiment_run_id)
            return run
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create experiment_run: %s", exc)
            raise

    def get_experiment_run(self, experiment_run_id: UUID) -> Optional[ExperimentRun]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.experiment_run WHERE experiment_run_id = %s",
                (str(experiment_run_id),),
            )
            row = cur.fetchone()
            return self._row_to_experiment_run(row) if row else None
        except Exception as exc:
            logger.error("Failed to get experiment_run: %s", exc)
            raise

    def update_experiment_run_status(
        self,
        experiment_run_id: UUID,
        new_status: str,
        reason: str = "",
        actor: str = "system",
    ) -> None:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT status FROM research.experiment_run WHERE experiment_run_id = %s",
                (str(experiment_run_id),),
            )
            row = cur.fetchone()
            old_status = row[0] if row else None

            if old_status is None:
                raise ValueError(f"ExperimentRun {experiment_run_id} not found")

            ResearchTransitionPolicyV1.validate("experiment_run", old_status, new_status)

            cur.execute(
                "UPDATE research.experiment_run SET status = %s WHERE experiment_run_id = %s",
                (new_status, str(experiment_run_id)),
            )
            self._record_transition(
                cur, "experiment_run", experiment_run_id, old_status, new_status, actor, reason
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update experiment_run status: %s", exc)
            raise

    def list_experiment_runs(self, experiment_id: UUID) -> list[ExperimentRun]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.experiment_run WHERE experiment_id = %s ORDER BY created_at ASC",
                (str(experiment_id),),
            )
            return [self._row_to_experiment_run(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list experiment_runs: %s", exc)
            raise

    # ==================================================================
    # 7. validation_result
    # ==================================================================

    def create_validation_result(self, vr: ValidationResult) -> ValidationResult:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.validation_result (
                    validation_result_id, experiment_run_id, split,
                    segment_type, segment_value, sample_size,
                    primary_metric_value, metrics_json, verdict,
                    created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(vr.validation_result_id),
                    str(vr.experiment_run_id),
                    vr.split,
                    vr.segment_type,
                    vr.segment_value,
                    vr.sample_size,
                    vr.primary_metric_value,
                    json.dumps(vr.metrics_json),
                    vr.verdict,
                    vr.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created validation_result %s", vr.validation_result_id)
            return vr
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create validation_result: %s", exc)
            raise

    def get_validation_result(self, validation_result_id: UUID) -> Optional[ValidationResult]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.validation_result WHERE validation_result_id = %s",
                (str(validation_result_id),),
            )
            row = cur.fetchone()
            return self._row_to_validation_result(row) if row else None
        except Exception as exc:
            logger.error("Failed to get validation_result: %s", exc)
            raise

    def list_validation_results(self, experiment_run_id: UUID) -> list[ValidationResult]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.validation_result WHERE experiment_run_id = %s ORDER BY created_at ASC",
                (str(experiment_run_id),),
            )
            return [self._row_to_validation_result(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list validation_results: %s", exc)
            raise

    # ==================================================================
    # 8. change_candidate
    # ==================================================================

    def create_change_candidate(self, cc: ChangeCandidate) -> ChangeCandidate:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.change_candidate (
                    candidate_id, hypothesis_id, experiment_id,
                    validation_result_id, risk_assessment_json,
                    status, approved_by, approved_at, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(cc.candidate_id),
                    str(cc.hypothesis_id) if cc.hypothesis_id else None,
                    str(cc.experiment_id) if cc.experiment_id else None,
                    str(cc.validation_result_id) if cc.validation_result_id else None,
                    json.dumps(cc.risk_assessment_json),
                    cc.status,
                    cc.approved_by,
                    cc.approved_at,
                    cc.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created change_candidate %s", cc.candidate_id)
            return cc
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create change_candidate: %s", exc)
            raise

    def get_change_candidate(self, candidate_id: UUID) -> Optional[ChangeCandidate]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.change_candidate WHERE candidate_id = %s",
                (str(candidate_id),),
            )
            row = cur.fetchone()
            return self._row_to_change_candidate(row) if row else None
        except Exception as exc:
            logger.error("Failed to get change_candidate: %s", exc)
            raise

    def update_change_candidate_status(
        self,
        candidate_id: UUID,
        new_status: str,
        reason: str = "",
        actor: str = "system",
    ) -> None:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT status FROM research.change_candidate WHERE candidate_id = %s",
                (str(candidate_id),),
            )
            row = cur.fetchone()
            old_status = row[0] if row else None

            if old_status is None:
                raise ValueError(f"ChangeCandidate {candidate_id} not found")

            ResearchTransitionPolicyV1.validate("change_candidate", old_status, new_status)

            cur.execute(
                "UPDATE research.change_candidate SET status = %s WHERE candidate_id = %s",
                (new_status, str(candidate_id)),
            )
            self._record_transition(
                cur, "change_candidate", candidate_id, old_status, new_status, actor, reason
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update change_candidate status: %s", exc)
            raise

    def list_change_candidates(self, status: Optional[str] = None) -> list[ChangeCandidate]:
        try:
            cur = self._conn.cursor()
            if status is not None:
                cur.execute(
                    "SELECT * FROM research.change_candidate WHERE status = %s ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM research.change_candidate ORDER BY created_at DESC")
            return [self._row_to_change_candidate(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list change_candidates: %s", exc)
            raise

    # ==================================================================
    # 9. production_change
    # ==================================================================

    def create_production_change(self, pc: ProductionChange) -> ProductionChange:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.production_change (
                    change_id, candidate_id, branch, pr_number,
                    commit_sha, deployment_at, config_snapshot_json,
                    affected_scanners_json, deployed_by, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(pc.change_id),
                    str(pc.candidate_id) if pc.candidate_id else None,
                    pc.branch,
                    pc.pr_number,
                    pc.commit_sha,
                    pc.deployment_at,
                    json.dumps(pc.config_snapshot_json),
                    json.dumps(pc.affected_scanners_json),
                    pc.deployed_by,
                    pc.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created production_change %s", pc.change_id)
            return pc
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create production_change: %s", exc)
            raise

    def get_production_change(self, change_id: UUID) -> Optional[ProductionChange]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.production_change WHERE change_id = %s",
                (str(change_id),),
            )
            row = cur.fetchone()
            return self._row_to_production_change(row) if row else None
        except Exception as exc:
            logger.error("Failed to get production_change: %s", exc)
            raise

    def list_production_changes(
        self,
        candidate_id: Optional[UUID] = None,
    ) -> list[ProductionChange]:
        try:
            cur = self._conn.cursor()
            if candidate_id is not None:
                cur.execute(
                    "SELECT * FROM research.production_change WHERE candidate_id = %s ORDER BY deployment_at DESC",
                    (str(candidate_id),),
                )
            else:
                cur.execute("SELECT * FROM research.production_change ORDER BY deployment_at DESC")
            return [self._row_to_production_change(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list production_changes: %s", exc)
            raise

    # ==================================================================
    # 10. monitoring_result
    # ==================================================================

    def create_monitoring_result(self, mr: MonitoringResult) -> MonitoringResult:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.monitoring_result (
                    monitoring_result_id, change_id, window,
                    observed_from, observed_to, sample_size,
                    primary_metric_actual, expected_metric,
                    guardrails_json, verdict, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(mr.monitoring_result_id),
                    str(mr.change_id) if mr.change_id else None,
                    mr.window,
                    mr.observed_from,
                    mr.observed_to,
                    mr.sample_size,
                    mr.primary_metric_actual,
                    mr.expected_metric,
                    json.dumps(mr.guardrails_json),
                    mr.verdict,
                    mr.created_at,
                ),
            )
            self._conn.commit()
            logger.info("Created monitoring_result %s", mr.monitoring_result_id)
            return mr
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create monitoring_result: %s", exc)
            raise

    def get_monitoring_result(self, monitoring_result_id: UUID) -> Optional[MonitoringResult]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.monitoring_result WHERE monitoring_result_id = %s",
                (str(monitoring_result_id),),
            )
            row = cur.fetchone()
            return self._row_to_monitoring_result(row) if row else None
        except Exception as exc:
            logger.error("Failed to get monitoring_result: %s", exc)
            raise

    def list_monitoring_results(
        self,
        change_id: Optional[UUID] = None,
    ) -> list[MonitoringResult]:
        try:
            cur = self._conn.cursor()
            if change_id is not None:
                cur.execute(
                    "SELECT * FROM research.monitoring_result WHERE change_id = %s ORDER BY created_at ASC",
                    (str(change_id),),
                )
            else:
                cur.execute("SELECT * FROM research.monitoring_result ORDER BY created_at DESC")
            return [self._row_to_monitoring_result(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list monitoring_results: %s", exc)
            raise

    # ==================================================================
    # 11. transition_history (append-only)
    # ==================================================================

    def _record_transition(
        self,
        cur: Any,
        entity_type: str,
        entity_id: UUID,
        from_status: Optional[str],
        to_status: str,
        actor: str,
        reason: str,
    ) -> None:
        """Insert a transition record (called inside an existing transaction)."""
        cur.execute(
            """
            INSERT INTO research.transition_history (
                entity_type, entity_id, from_status, to_status, actor, reason
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (entity_type, str(entity_id), from_status, to_status, actor, reason or None),
        )

    def record_transition(
        self,
        entity_type: str,
        entity_id: UUID,
        from_status: Optional[str],
        to_status: str,
        actor: str = "system",
        reason: str = "",
    ) -> None:
        """Public wrapper: inserts a transition in its own transaction."""
        try:
            cur = self._conn.cursor()
            self._record_transition(cur, entity_type, entity_id, from_status, to_status, actor, reason)
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to record transition: %s", exc)
            raise

    def get_transitions(
        self, entity_type: str, entity_id: UUID
    ) -> list[TransitionRecord]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT transition_id, entity_type, entity_id, from_status,
                       to_status, actor, reason, metadata, created_at
                FROM research.transition_history
                WHERE entity_type = %s AND entity_id = %s
                ORDER BY created_at ASC
                """,
                (entity_type, str(entity_id)),
            )
            return [self._row_to_transition(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to get transitions: %s", exc)
            raise

    # ==================================================================
    # 12. fingerprint
    # ==================================================================

    def create_fingerprint(
        self,
        finding_id: UUID,
        finding_type: str,
        scanner_name: str,
        direction: str,
        metric_name: str,
        normalized_segment: Optional[str] = None,
        comparator: Optional[str] = None,
        threshold_policy_version: Optional[str] = None,
    ) -> Optional[UUID]:
        """Insert a fingerprint. Returns the fingerprint_id, or None if hash already exists."""
        try:
            cur = self._conn.cursor()
            fp_hash = self._fingerprint_hash(finding_type, scanner_name, direction, metric_name)
            cur.execute(
                """
                INSERT INTO research.fingerprint (
                    finding_id, fingerprint_hash, finding_type,
                    scanner_name, direction, normalized_segment,
                    metric_name, comparator, threshold_policy_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint_hash) DO NOTHING
                RETURNING fingerprint_id
                """,
                (
                    str(finding_id),
                    fp_hash,
                    finding_type,
                    scanner_name,
                    direction,
                    normalized_segment,
                    metric_name,
                    comparator,
                    threshold_policy_version,
                ),
            )
            row = cur.fetchone()
            self._conn.commit()
            return UUID(row[0]) if row else None
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create fingerprint: %s", exc)
            raise

    def find_by_fingerprint(self, fingerprint_hash: str) -> Optional[UUID]:
        """Return the finding_id associated with a fingerprint hash, or None."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT finding_id FROM research.fingerprint WHERE fingerprint_hash = %s",
                (fingerprint_hash,),
            )
            row = cur.fetchone()
            return UUID(row[0]) if row else None
        except Exception as exc:
            logger.error("Failed to find by fingerprint: %s", exc)
            raise

    def get_fingerprints_by_finding(self, finding_id: UUID) -> list[dict]:
        """Return all fingerprint records for a finding."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT fingerprint_id, finding_id, fingerprint_hash, finding_type,
                       scanner_name, direction, normalized_segment, metric_name,
                       comparator, threshold_policy_version, created_at
                FROM research.fingerprint
                WHERE finding_id = %s
                """,
                (str(finding_id),),
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "fingerprint_id": UUID(row[0]),
                    "finding_id": UUID(row[1]),
                    "fingerprint_hash": row[2],
                    "finding_type": row[3],
                    "scanner_name": row[4],
                    "direction": row[5],
                    "normalized_segment": row[6],
                    "metric_name": row[7],
                    "comparator": row[8],
                    "threshold_policy_version": row[9],
                    "created_at": row[10],
                })
            return results
        except Exception as exc:
            logger.error("Failed to get fingerprints by finding: %s", exc)
            raise

    # ==================================================================
    # PR2: Finding Aggregation — lookup helpers
    # ==================================================================

    def get_finding_by_fingerprint(self, fingerprint_hash: str) -> Optional[Finding]:
        """Return the Finding associated with a fingerprint hash, or None.

        Uses the unique index on research.fingerprint.fingerprint_hash
        to resolve to the linked finding.
        """
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT f.*
                FROM research.finding f
                JOIN research.fingerprint fp ON fp.finding_id = f.finding_id
                WHERE fp.fingerprint_hash = %s
                """,
                (fingerprint_hash,),
            )
            row = cur.fetchone()
            return self._row_to_finding(row) if row else None
        except Exception as exc:
            logger.error("Failed to get finding by fingerprint: %s", exc)
            raise

    def create_finding_with_fingerprint(
        self,
        finding: Finding,
        fingerprint_payload: dict,
    ) -> Finding:
        """Create a finding AND its fingerprint record in a single transaction.

        The fingerprint_payload dict must contain:
            fingerprint_hash, finding_type, scanner_name, direction,
            normalized_segment, metric_name, comparator, threshold_policy_version
        """
        try:
            cur = self._conn.cursor()
            # Insert finding
            cur.execute(
                """
                INSERT INTO research.finding (
                    finding_id, finding_type, title, fingerprint,
                    scope_json, first_seen, last_seen, occurrence_count,
                    status, confidence, evidence_summary,
                    source_run_id, agent_name, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(finding.finding_id),
                    finding.finding_type,
                    finding.title,
                    finding.fingerprint,
                    json.dumps(finding.scope_json),
                    finding.first_seen,
                    finding.last_seen,
                    finding.occurrence_count,
                    finding.status,
                    finding.confidence,
                    json.dumps(finding.evidence_summary),
                    str(finding.source_run_id) if finding.source_run_id else None,
                    finding.agent_name,
                    finding.created_at,
                    finding.updated_at,
                ),
            )
            # Insert fingerprint
            cur.execute(
                """
                INSERT INTO research.fingerprint (
                    finding_id, fingerprint_hash, finding_type,
                    scanner_name, direction, normalized_segment,
                    metric_name, comparator, threshold_policy_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint_hash) DO NOTHING
                """,
                (
                    str(finding.finding_id),
                    fingerprint_payload["fingerprint_hash"],
                    fingerprint_payload["finding_type"],
                    fingerprint_payload["scanner_name"],
                    fingerprint_payload["direction"],
                    json.dumps(fingerprint_payload["normalized_segment"]),
                    fingerprint_payload["metric_name"],
                    fingerprint_payload["comparator"],
                    fingerprint_payload["threshold_policy_version"],
                ),
            )
            self._conn.commit()
            logger.info(
                "Created finding %s with fingerprint %s",
                finding.finding_id,
                fingerprint_payload["fingerprint_hash"][:12],
            )
            return finding
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create finding with fingerprint: %s", exc)
            raise

    def upsert_fingerprint(
        self,
        finding_id: UUID,
        fingerprint_hash: str,
        payload: dict,
    ) -> None:
        """Insert or link a fingerprint to a finding.  Idempotent via ON CONFLICT."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.fingerprint (
                    finding_id, fingerprint_hash, finding_type,
                    scanner_name, direction, normalized_segment,
                    metric_name, comparator, threshold_policy_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint_hash) DO UPDATE
                    SET finding_id = EXCLUDED.finding_id
                """,
                (
                    str(finding_id),
                    fingerprint_hash,
                    payload.get("finding_type", ""),
                    payload.get("scanner_name", ""),
                    payload.get("direction", ""),
                    json.dumps(payload.get("normalized_segment", {})),
                    payload.get("metric_name", ""),
                    payload.get("comparator", ""),
                    payload.get("threshold_policy_version", ""),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to upsert fingerprint: %s", exc)
            raise

    def create_occurrence_if_absent(self, occ: FindingOccurrence) -> FindingOccurrence:
        """Insert a finding_occurrence if not already present for this (finding_id, analysis_run_id).

        Uses ON CONFLICT ... DO NOTHING to guarantee idempotency.
        Returns the occurrence (with occurrence_id set, even if it was a no-op insert).
        """
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.finding_occurrence (
                    occurrence_id, finding_id, analysis_run_id, observed_at,
                    metric_value, sample_size, confidence, evidence_refs,
                    dataset_version, details_json, created_at,
                    agent_run_id, business_date, maturity, source_agent_name
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (finding_id, analysis_run_id) DO NOTHING
                """,
                (
                    str(occ.occurrence_id),
                    str(occ.finding_id),
                    str(occ.analysis_run_id) if occ.analysis_run_id else None,
                    occ.observed_at,
                    occ.metric_value,
                    occ.sample_size,
                    occ.confidence,
                    json.dumps(occ.evidence_refs),
                    occ.dataset_version,
                    json.dumps(occ.details_json),
                    occ.created_at,
                    str(occ.agent_run_id) if occ.agent_run_id else None,
                    occ.business_date,
                    occ.maturity,
                    occ.source_agent_name,
                ),
            )
            self._conn.commit()
            return occ
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create occurrence if absent: %s", exc)
            raise

    def list_occurrences_for_finding(self, finding_id: UUID) -> list[FindingOccurrence]:
        """Return all occurrences for a finding, ordered by observed_at ASC."""
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM research.finding_occurrence WHERE finding_id = %s ORDER BY observed_at ASC",
                (str(finding_id),),
            )
            return [self._row_to_finding_occurrence(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list occurrences for finding: %s", exc)
            raise

    def refresh_finding_aggregate(self, finding_id: UUID) -> None:
        """Refresh first_seen, last_seen, occurrence_count for a finding.

        Called after inserting a new occurrence to keep aggregates current.
        """
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE research.finding
                SET first_seen = COALESCE(sub.first_seen, first_seen),
                    last_seen  = COALESCE(sub.last_seen, last_seen),
                    occurrence_count = COALESCE(sub.cnt, occurrence_count),
                    updated_at = NOW()
                FROM (
                    SELECT
                        MIN(observed_at) AS first_seen,
                        MAX(observed_at) AS last_seen,
                        COUNT(*)         AS cnt
                    FROM research.finding_occurrence
                    WHERE finding_id = %s
                ) sub
                WHERE finding.finding_id = %s
                """,
                (str(finding_id), str(finding_id)),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to refresh finding aggregate: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Row -> dataclass mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_finding(row: tuple) -> Finding:
        return Finding(
            finding_id=UUID(row[0]),
            finding_type=row[1],
            title=row[2],
            fingerprint=row[3],
            scope_json=json.loads(row[4]) if row[4] else {},
            first_seen=row[5],
            last_seen=row[6],
            occurrence_count=int(row[7]) if row[7] else 1,
            status=row[8],
            confidence=row[9],
            evidence_summary=json.loads(row[10]) if row[10] else [],
            source_run_id=UUID(row[11]) if row[11] else None,
            agent_name=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    @staticmethod
    def _row_to_finding_occurrence(row: tuple) -> FindingOccurrence:
        # Handle variable-width rows (pre/post migration 037)
        # Core columns (0-10) are always present
        # Extended columns (11+) may or may not exist depending on SELECT columns
        base = FindingOccurrence(
            occurrence_id=UUID(row[0]),
            finding_id=UUID(row[1]),
            analysis_run_id=UUID(row[2]) if row[2] else None,
            observed_at=row[3],
            metric_value=row[4],
            sample_size=int(row[5]) if row[5] else 0,
            confidence=row[6],
            evidence_refs=json.loads(row[7]) if row[7] else [],
            dataset_version=row[8],
            details_json=json.loads(row[9]) if row[9] else {},
            created_at=row[10],
        )
        # Extended fields from migration 037 (indices 11-14 when SELECT *)
        if len(row) > 11 and row[11] is not None:
            base.agent_run_id = UUID(row[11]) if row[11] else None
        if len(row) > 12 and row[12] is not None:
            base.business_date = row[12] if isinstance(row[12], str) else str(row[12]) if row[12] else None
        if len(row) > 13 and row[13] is not None:
            base.maturity = row[13] or "PROVISIONAL"
        if len(row) > 14 and row[14] is not None:
            base.source_agent_name = row[14] or ""
        return base

    @staticmethod
    def _row_to_hypothesis(row: tuple) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=UUID(row[0]),
            statement=row[1],
            falsification_criterion=row[2],
            population_json=json.loads(row[3]) if row[3] else {},
            intervention_json=json.loads(row[4]) if row[4] else {},
            baseline_json=json.loads(row[5]) if row[5] else {},
            primary_metric=row[6],
            guardrails_json=json.loads(row[7]) if row[7] else {},
            minimum_sample=int(row[8]) if row[8] else 0,
            status=row[9],
            version=int(row[10]) if row[10] else 1,
            source_run_id=UUID(row[11]) if row[11] else None,
            agent_name=row[12],
            evidence_refs=json.loads(row[13]) if row[13] else [],
            created_at=row[14],
            updated_at=row[15],
        )

    @staticmethod
    def _row_to_experiment(row: tuple) -> Experiment:
        return Experiment(
            experiment_id=UUID(row[0]),
            hypothesis_id=UUID(row[1]) if row[1] else None,
            title=row[2],
            description=row[3],
            protocol_version=row[4],
            protocol_json=json.loads(row[5]) if row[5] else {},
            frozen_at=row[6],
            status=row[7],
            source_run_id=UUID(row[8]) if row[8] else None,
            agent_name=row[9],
            evidence_refs=json.loads(row[10]) if row[10] else [],
            created_at=row[11],
            updated_at=row[12],
        )

    @staticmethod
    def _row_to_experiment_run(row: tuple) -> ExperimentRun:
        return ExperimentRun(
            experiment_run_id=UUID(row[0]),
            experiment_id=UUID(row[1]),
            dataset_version=row[2],
            trad_bot_commit_sha=row[3],
            backtest_commit_sha=row[4],
            status=row[5],
            started_at=row[6],
            finished_at=row[7],
            artifact_location=row[8],
            reproducibility_command=row[9],
            created_at=row[10],
        )

    @staticmethod
    def _row_to_validation_result(row: tuple) -> ValidationResult:
        return ValidationResult(
            validation_result_id=UUID(row[0]),
            experiment_run_id=UUID(row[1]),
            split=row[2],
            segment_type=row[3],
            segment_value=row[4],
            sample_size=int(row[5]) if row[5] else 0,
            primary_metric_value=row[6],
            metrics_json=json.loads(row[7]) if row[7] else {},
            verdict=row[8],
            created_at=row[9],
        )

    @staticmethod
    def _row_to_change_candidate(row: tuple) -> ChangeCandidate:
        return ChangeCandidate(
            candidate_id=UUID(row[0]),
            hypothesis_id=UUID(row[1]) if row[1] else None,
            experiment_id=UUID(row[2]) if row[2] else None,
            validation_result_id=UUID(row[3]) if row[3] else None,
            risk_assessment_json=json.loads(row[4]) if row[4] else {},
            status=row[5],
            approved_by=row[6],
            approved_at=row[7],
            created_at=row[8],
        )

    @staticmethod
    def _row_to_production_change(row: tuple) -> ProductionChange:
        return ProductionChange(
            change_id=UUID(row[0]),
            candidate_id=UUID(row[1]) if row[1] else None,
            branch=row[2],
            pr_number=int(row[3]) if row[3] else None,
            commit_sha=row[4],
            deployment_at=row[5],
            config_snapshot_json=json.loads(row[6]) if row[6] else {},
            affected_scanners_json=json.loads(row[7]) if row[7] else [],
            deployed_by=row[8],
            created_at=row[9],
        )

    @staticmethod
    def _row_to_monitoring_result(row: tuple) -> MonitoringResult:
        return MonitoringResult(
            monitoring_result_id=UUID(row[0]),
            change_id=UUID(row[1]) if row[1] else None,
            window=row[2],
            observed_from=row[3],
            observed_to=row[4],
            sample_size=int(row[5]) if row[5] else 0,
            primary_metric_actual=row[6],
            expected_metric=row[7],
            guardrails_json=json.loads(row[8]) if row[8] else {},
            verdict=row[9],
            created_at=row[10],
        )

    @staticmethod
    def _row_to_transition(row: tuple) -> TransitionRecord:
        return TransitionRecord(
            transition_id=int(row[0]),
            entity_type=row[1],
            entity_id=UUID(row[2]),
            from_status=row[3],
            to_status=row[4],
            actor=row[5],
            reason=row[6],
            metadata=json.loads(row[7]) if row[7] else {},
            created_at=row[8],
        )
