"""Repository for research foundation tables (Stage 4)."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID

from app.analytics.agents.research.models import (
    Experiment,
    Finding,
    Hypothesis,
    TransitionRecord,
)

logger = logging.getLogger(__name__)


class ResearchRepository:
    """CRUD repository for research.finding / hypothesis / experiment / transition_history / fingerprint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint_hash(finding_code: str, scanner: str, direction: str, metric_name: str) -> str:
        """Deterministic SHA-256 of the normalised fingerprint components."""
        raw = f"{finding_code}|{scanner}|{direction}|{metric_name}".lower()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # finding
    # ------------------------------------------------------------------

    def create_finding(self, finding: Finding) -> Finding:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.finding (
                    finding_id, finding_code, title, severity, status,
                    source_run_id, agent_name, scope, sample_size,
                    confidence, evidence_refs, metric_refs, statement,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(finding.finding_id),
                    finding.finding_code,
                    finding.title,
                    finding.severity,
                    finding.status,
                    str(finding.source_run_id) if finding.source_run_id else None,
                    finding.agent_name,
                    json.dumps(finding.scope),
                    finding.sample_size,
                    finding.confidence,
                    json.dumps(finding.evidence_refs),
                    json.dumps(finding.metric_refs),
                    finding.statement,
                    finding.created_at,
                    finding.updated_at,
                ),
            )
            self._conn.commit()
            logger.info("Created finding %s (%s)", finding.finding_id, finding.finding_code)
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

    def update_finding_status(self, finding_id: UUID, new_status: str, reason: str = "", actor: str = "system") -> None:
        try:
            cur = self._conn.cursor()
            # Fetch current status for audit
            cur.execute("SELECT status FROM research.finding WHERE finding_id = %s", (str(finding_id),))
            row = cur.fetchone()
            old_status = row[0] if row else None

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
        source_run_id: Optional[UUID] = None,
    ) -> list[Finding]:
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
            cur.execute(f"SELECT * FROM research.finding{where} ORDER BY created_at DESC", params)
            return [self._row_to_finding(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list findings: %s", exc)
            raise

    # ------------------------------------------------------------------
    # hypothesis
    # ------------------------------------------------------------------

    def create_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.hypothesis (
                    hypothesis_id, finding_id, hypothesis_code, statement,
                    falsifiable_experiment, required_data, validation_criterion,
                    confidence, status, source_run_id, agent_name, evidence_refs,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(hypothesis.hypothesis_id),
                    str(hypothesis.finding_id) if hypothesis.finding_id else None,
                    hypothesis.hypothesis_code,
                    hypothesis.statement,
                    hypothesis.falsifiable_experiment,
                    json.dumps(hypothesis.required_data),
                    hypothesis.validation_criterion,
                    hypothesis.confidence,
                    hypothesis.status,
                    str(hypothesis.source_run_id) if hypothesis.source_run_id else None,
                    hypothesis.agent_name,
                    json.dumps(hypothesis.evidence_refs),
                    hypothesis.created_at,
                    hypothesis.updated_at,
                ),
            )
            self._conn.commit()
            logger.info("Created hypothesis %s (%s)", hypothesis.hypothesis_id, hypothesis.hypothesis_code)
            return hypothesis
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to create hypothesis: %s", exc)
            raise

    def get_hypothesis(self, hypothesis_id: UUID) -> Optional[Hypothesis]:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM research.hypothesis WHERE hypothesis_id = %s", (str(hypothesis_id),))
            row = cur.fetchone()
            return self._row_to_hypothesis(row) if row else None
        except Exception as exc:
            logger.error("Failed to get hypothesis: %s", exc)
            raise

    def update_hypothesis_status(self, hypothesis_id: UUID, new_status: str, reason: str = "", actor: str = "system") -> None:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT status FROM research.hypothesis WHERE hypothesis_id = %s", (str(hypothesis_id),))
            row = cur.fetchone()
            old_status = row[0] if row else None

            cur.execute(
                "UPDATE research.hypothesis SET status = %s, updated_at = NOW() WHERE hypothesis_id = %s",
                (new_status, str(hypothesis_id)),
            )
            self._record_transition(cur, "hypothesis", hypothesis_id, old_status, new_status, actor, reason)
            self._conn.commit()
            logger.info("Hypothesis %s status: %s -> %s", hypothesis_id, old_status, new_status)
        except Exception as exc:
            self._conn.rollback()
            logger.error("Failed to update hypothesis status: %s", exc)
            raise

    def list_hypotheses(
        self,
        status: Optional[str] = None,
        finding_id: Optional[UUID] = None,
    ) -> list[Hypothesis]:
        try:
            cur = self._conn.cursor()
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = %s")
                params.append(status)
            if finding_id is not None:
                clauses.append("finding_id = %s")
                params.append(str(finding_id))

            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT * FROM research.hypothesis{where} ORDER BY created_at DESC", params)
            return [self._row_to_hypothesis(r) for r in cur.fetchall()]
        except Exception as exc:
            logger.error("Failed to list hypotheses: %s", exc)
            raise

    # ------------------------------------------------------------------
    # experiment
    # ------------------------------------------------------------------

    def create_experiment(self, experiment: Experiment) -> Experiment:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO research.experiment (
                    experiment_id, hypothesis_id, title, description,
                    required_data, validation_criterion, status,
                    source_run_id, agent_name, evidence_refs,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(experiment.experiment_id),
                    str(experiment.hypothesis_id) if experiment.hypothesis_id else None,
                    experiment.title,
                    experiment.description,
                    json.dumps(experiment.required_data),
                    experiment.validation_criterion,
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
            cur.execute("SELECT * FROM research.experiment WHERE experiment_id = %s", (str(experiment_id),))
            row = cur.fetchone()
            return self._row_to_experiment(row) if row else None
        except Exception as exc:
            logger.error("Failed to get experiment: %s", exc)
            raise

    def update_experiment_status(self, experiment_id: UUID, new_status: str, reason: str = "", actor: str = "system") -> None:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT status FROM research.experiment WHERE experiment_id = %s", (str(experiment_id),))
            row = cur.fetchone()
            old_status = row[0] if row else None

            cur.execute(
                "UPDATE research.experiment SET status = %s, updated_at = NOW() WHERE experiment_id = %s",
                (new_status, str(experiment_id)),
            )
            self._record_transition(cur, "experiment", experiment_id, old_status, new_status, actor, reason)
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

    # ------------------------------------------------------------------
    # transition_history
    # ------------------------------------------------------------------

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

    def get_transitions(self, entity_type: str, entity_id: UUID) -> list[TransitionRecord]:
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

    # ------------------------------------------------------------------
    # fingerprint
    # ------------------------------------------------------------------

    def create_fingerprint(
        self,
        finding_id: UUID,
        finding_code: str,
        scanner: str,
        direction: str,
        metric_name: str,
    ) -> Optional[UUID]:
        """Insert a fingerprint. Returns the fingerprint_id, or None if hash already exists."""
        try:
            cur = self._conn.cursor()
            fp_hash = self._fingerprint_hash(finding_code, scanner, direction, metric_name)
            cur.execute(
                """
                INSERT INTO research.fingerprint (
                    finding_code, scanner, direction, metric_name,
                    fingerprint_hash, finding_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (fingerprint_hash) DO NOTHING
                RETURNING fingerprint_id
                """,
                (finding_code, scanner, direction, metric_name, fp_hash, str(finding_id)),
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

    # ------------------------------------------------------------------
    # Row -> dataclass mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_finding(row: tuple) -> Finding:
        return Finding(
            finding_id=UUID(row[0]),
            finding_code=row[1],
            title=row[2],
            severity=row[3],
            status=row[4],
            source_run_id=UUID(row[5]) if row[5] else None,
            agent_name=row[6],
            scope=json.loads(row[7]) if row[7] else {},
            sample_size=int(row[8]) if row[8] else 0,
            confidence=row[9],
            evidence_refs=json.loads(row[10]) if row[10] else [],
            metric_refs=json.loads(row[11]) if row[11] else [],
            statement=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    @staticmethod
    def _row_to_hypothesis(row: tuple) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=UUID(row[0]),
            finding_id=UUID(row[1]) if row[1] else None,
            hypothesis_code=row[2],
            statement=row[3],
            falsifiable_experiment=row[4],
            required_data=json.loads(row[5]) if row[5] else [],
            validation_criterion=row[6],
            confidence=row[7],
            status=row[8],
            source_run_id=UUID(row[9]) if row[9] else None,
            agent_name=row[10],
            evidence_refs=json.loads(row[11]) if row[11] else [],
            created_at=row[12],
            updated_at=row[13],
        )

    @staticmethod
    def _row_to_experiment(row: tuple) -> Experiment:
        return Experiment(
            experiment_id=UUID(row[0]),
            hypothesis_id=UUID(row[1]) if row[1] else None,
            title=row[2],
            description=row[3],
            required_data=json.loads(row[4]) if row[4] else [],
            validation_criterion=row[5],
            status=row[6],
            source_run_id=UUID(row[7]) if row[7] else None,
            agent_name=row[8],
            evidence_refs=json.loads(row[9]) if row[9] else [],
            created_at=row[10],
            updated_at=row[11],
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
