"""Finding Ingestion Service — core orchestrator for PR2.

Transforms FindingCandidate DTOs into persisted research.finding +
research.finding_occurrence rows, with fingerprint-based dedup and
repeat-policy-driven status transitions.

Design constraints:
    - Python/SQL calculates, LLM interprets (no financial metric computation)
    - Only writes to research.* schema
    - Transition policy must be enforced (via repository layer)
    - No production mutation
    - All code must be deterministic and testable
    - Runs under analytics_runner role (not analytics_agent)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.analytics.agents.research.fingerprint import (
    FindingFingerprintPayload,
    FindingFingerprintV1,
)
from app.analytics.agents.research.models import (
    Finding,
    FindingCandidate,
    FindingIngestionResult,
    FindingOccurrence,
    IngestionBatchSummary,
)
from app.analytics.agents.research.normalizer import (
    ComparatorNormalizer,
    DirectionNormalizer,
    FindingSegmentNormalizerV1,
    FindingTypeNormalizer,
    ScannerNormalizer,
)
from app.analytics.agents.research.repeat_policy import (
    FindingRepeatPolicyConfig,
    FindingRepeatPolicyV1,
    KEEP_OPEN,
    MARK_REPEATED,
    MARK_RESEARCH_REQUIRED,
    NO_CHANGE,
)
from app.analytics.agents.research.repository import ResearchRepository

logger = logging.getLogger(__name__)


class FindingIngestionService:
    """Orchestrates the ingestion of FindingCandidate DTOs.

    Lifecycle per candidate:
        1. Validate & normalize inputs
        2. Compute fingerprint
        3. Lookup fingerprint → find existing finding
        4. If found → link occurrence to existing finding
        5. If not found → create finding + fingerprint
        6. Insert occurrence (idempotent on finding_id + analysis_run_id)
        7. Refresh aggregates (first_seen, last_seen, occurrence_count)
        8. Evaluate repeat policy
        9. Maybe transition status (via repository)
        10. Return result
    """

    def __init__(
        self,
        repo: ResearchRepository,
        policy_config: Optional[FindingRepeatPolicyConfig] = None,
    ) -> None:
        self._repo = repo
        self._config = policy_config or FindingRepeatPolicyConfig()

    # ------------------------------------------------------------------
    # Single candidate ingestion
    # ------------------------------------------------------------------

    def ingest_candidate(
        self,
        candidate: FindingCandidate,
    ) -> FindingIngestionResult:
        """Ingest a single FindingCandidate.

        Returns a FindingIngestionResult with full audit trail.
        """
        # 1. Validate & normalize
        payload = self._build_fingerprint_payload(candidate)
        if payload is None:
            # Invalid candidate — cannot compute fingerprint
            return FindingIngestionResult(
                finding_id=uuid4(),
                fingerprint="",
                policy_action="SKIPPED_INVALID",
            )

        # 2. Compute fingerprint
        fingerprint_hash = FindingFingerprintV1.compute(payload)

        # 3. Lookup existing finding by fingerprint
        existing_finding = self._repo.get_finding_by_fingerprint(fingerprint_hash)

        created_finding = False
        created_occurrence = False
        idempotent_replay = False

        if existing_finding is not None:
            # 4. Link to existing finding
            finding = existing_finding
        else:
            # 5. Create new finding + fingerprint
            finding = self._create_finding_from_candidate(candidate, fingerprint_hash)
            created_finding = True

        # 6. Create occurrence (idempotent)
        occurrence = self._create_occurrence(candidate, finding.finding_id)
        if occurrence is not None:
            created_occurrence = True
        else:
            idempotent_replay = True

        # 7. Refresh aggregates
        self._repo.refresh_finding_aggregate(finding.finding_id)

        # 8. Evaluate repeat policy
        all_occurrences = self._repo.list_occurrences_for_finding(finding.finding_id)
        occ_dicts = [
            {
                "confidence": occ.confidence or "LOW",
                "business_date": occ.business_date,
            }
            for occ in all_occurrences
        ]
        policy_action = FindingRepeatPolicyV1.evaluate(
            self._config,
            occ_dicts,
            finding.status,
        )

        # 9. Maybe transition status
        old_status = finding.status
        new_status = self._apply_policy_action(finding.finding_id, policy_action, finding.status)

        return FindingIngestionResult(
            finding_id=finding.finding_id,
            fingerprint=fingerprint_hash,
            occurrence_id=occurrence.occurrence_id if occurrence else None,
            created_finding=created_finding,
            created_occurrence=created_occurrence,
            idempotent_replay=idempotent_replay,
            old_status=old_status,
            new_status=new_status,
            policy_action=policy_action,
        )

    # ------------------------------------------------------------------
    # Batch ingestion
    # ------------------------------------------------------------------

    def ingest_batch(
        self,
        candidates: list[FindingCandidate],
    ) -> IngestionBatchSummary:
        """Ingest a batch of candidates.  Per-candidate transaction.

        Returns an IngestionBatchSummary with aggregate counts.
        """
        summary = IngestionBatchSummary(candidates_seen=len(candidates))

        for candidate in candidates:
            try:
                result = self.ingest_candidate(candidate)
                self._update_summary(summary, result)
            except Exception as exc:
                logger.error("Failed to ingest candidate: %s", exc)
                summary.errors += 1

        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_fingerprint_payload(
        self,
        candidate: FindingCandidate,
    ) -> Optional[FindingFingerprintPayload]:
        """Normalize candidate fields and build a fingerprint payload.

        Returns None if any required field cannot be normalized.
        """
        # Normalize finding_type
        finding_type = FindingTypeNormalizer.normalize(candidate.finding_type)
        if finding_type is None:
            logger.warning(
                "Skipping candidate: unmappable finding_type '%s'",
                candidate.finding_type,
            )
            return None

        # Normalize scanner_name
        scanner_name = ScannerNormalizer.normalize(candidate.scanner_name)
        if scanner_name is None:
            logger.warning("Skipping candidate: empty scanner_name")
            return None

        # Normalize direction
        direction = DirectionNormalizer.normalize(candidate.direction)

        # Normalize segment
        normalized_segment = FindingSegmentNormalizerV1.normalize(
            candidate.normalized_segment
        )

        # Normalize metric_name (trimmed, lowercase)
        metric_name = (candidate.metric_name or "").strip()
        if not metric_name:
            logger.warning("Skipping candidate: empty metric_name")
            return None
        metric_name = metric_name.lower()

        # Normalize comparator
        comparator = ComparatorNormalizer.normalize(candidate.comparator)
        if comparator is None:
            logger.warning(
                "Skipping candidate: unknown comparator '%s'",
                candidate.comparator,
            )
            return None

        # threshold_policy_version — use as-is, must not be empty
        threshold_policy_version = (candidate.threshold_policy_version or "").strip()
        if not threshold_policy_version:
            threshold_policy_version = "finding-thresholds-v1"

        return FindingFingerprintPayload(
            finding_type=finding_type,
            scanner_name=scanner_name,
            direction=direction,
            normalized_segment=normalized_segment,
            metric_name=metric_name,
            comparator=comparator,
            threshold_policy_version=threshold_policy_version,
        )

    def _create_finding_from_candidate(
        self,
        candidate: FindingCandidate,
        fingerprint_hash: str,
    ) -> Finding:
        """Create a new finding record from a candidate."""
        now = datetime.now(timezone.utc)
        finding = Finding(
            finding_id=uuid4(),
            finding_type=FindingTypeNormalizer.normalize(candidate.finding_type) or candidate.finding_type,
            title=candidate.statement,
            fingerprint=fingerprint_hash,
            scope_json=candidate.scope_json or {},
            first_seen=candidate.observed_at or now,
            last_seen=candidate.observed_at or now,
            occurrence_count=0,  # will be incremented by occurrence insert
            status="OPEN",
            confidence=candidate.confidence or "LOW",
            evidence_summary=list(candidate.evidence_refs or []),
            source_run_id=candidate.analysis_run_id,
            agent_name=candidate.source_agent_name or "",
            created_at=now,
            updated_at=now,
        )

        # Build fingerprint payload dict for the repository
        fp_payload = {
            "fingerprint_hash": fingerprint_hash,
            "finding_type": finding.finding_type,
            "scanner_name": ScannerNormalizer.normalize(candidate.scanner_name) or "",
            "direction": DirectionNormalizer.normalize(candidate.direction),
            "normalized_segment": FindingSegmentNormalizerV1.normalize(
                candidate.normalized_segment
            ),
            "metric_name": (candidate.metric_name or "").strip().lower(),
            "comparator": ComparatorNormalizer.normalize(candidate.comparator) or "",
            "threshold_policy_version": (
                candidate.threshold_policy_version or "finding-thresholds-v1"
            ),
        }

        return self._repo.create_finding_with_fingerprint(finding, fp_payload)

    def _create_occurrence(
        self,
        candidate: FindingCandidate,
        finding_id,
    ) -> Optional[FindingOccurrence]:
        """Create an occurrence record.  Returns None if idempotent replay (already exists)."""
        now = datetime.now(timezone.utc)
        occurrence = FindingOccurrence(
            occurrence_id=uuid4(),
            finding_id=finding_id,
            analysis_run_id=candidate.analysis_run_id,
            observed_at=candidate.observed_at or now,
            metric_value=candidate.metric_value,
            sample_size=candidate.sample_size,
            confidence=candidate.confidence,
            evidence_refs=list(candidate.evidence_refs or []),
            dataset_version=candidate.dataset_version,
            details_json={},
            created_at=now,
            agent_run_id=candidate.agent_run_id,
            business_date=candidate.business_date,
            maturity=candidate.maturity,
            source_agent_name=candidate.source_agent_name,
        )
        # ON CONFLICT DO NOTHING — check if occurrence already exists
        existing = self._repo.list_occurrences_for_finding(finding_id)
        for occ in existing:
            if occ.analysis_run_id == candidate.analysis_run_id:
                # Idempotent replay — already exists
                return None
        # Insert new occurrence
        self._repo.create_occurrence_if_absent(occurrence)
        return occurrence

    def _apply_policy_action(
        self,
        finding_id,
        policy_action: str,
        current_status: str,
    ) -> str:
        """Apply the repeat policy action via the repository.

        Returns the new status (or old status if no change).
        """
        if policy_action == MARK_REPEATED:
            try:
                self._repo.update_finding_status(
                    finding_id, "REPEATED", reason="repeat_policy"
                )
                return "REPEATED"
            except ValueError as exc:
                logger.warning("Status transition failed: %s", exc)
                return current_status

        if policy_action == MARK_RESEARCH_REQUIRED:
            try:
                self._repo.update_finding_status(
                    finding_id, "RESEARCH_REQUIRED", reason="repeat_policy"
                )
                return "RESEARCH_REQUIRED"
            except ValueError as exc:
                logger.warning("Status transition failed: %s", exc)
                return current_status

        return current_status

    def _update_summary(
        self,
        summary: IngestionBatchSummary,
        result: FindingIngestionResult,
    ) -> None:
        """Update batch summary from a single ingestion result."""
        if result.policy_action == "SKIPPED_INVALID":
            summary.skipped_invalid += 1
            return

        if result.created_finding:
            summary.created_findings += 1
        else:
            summary.linked_findings += 1

        if result.created_occurrence:
            summary.created_occurrences += 1
        elif result.idempotent_replay:
            summary.idempotent_replays += 1

        if result.policy_action == MARK_REPEATED:
            summary.promoted_repeated += 1
        elif result.policy_action == MARK_RESEARCH_REQUIRED:
            summary.promoted_research_required += 1
