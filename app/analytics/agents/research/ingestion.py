"""Finding Ingestion Service — core orchestrator for PR2.

Transforms FindingCandidate DTOs into persisted research.finding +
research.finding_occurrence rows, with fingerprint-based dedup and
repeat-policy-driven status transitions.

CRITICAL TRANSACTION DESIGN:
    The entire ingest_candidate lifecycle runs within a single logical
    transaction.  Repository helper methods called INSIDE ingest_candidate
    must NOT commit independently — they must participate in the same
    transaction managed by the caller.  On any error: ROLLBACK EVERYTHING.

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
    PROMOTION_DISABLED,
)
from app.analytics.agents.research.repository import ResearchRepository

logger = logging.getLogger(__name__)


class FindingIngestionService:
    """Orchestrates the ingestion of FindingCandidate DTOs.

    Lifecycle per candidate (single logical transaction):
        1. Validate candidate (analysis_run_id required)
        2. Normalize → fingerprint payload
        3. Compute fingerprint
        4. Lookup fingerprint → find/create finding (with unique-conflict handling)
        5. Insert occurrence (with idempotency check)
        6. Refresh aggregates
        7. Evaluate repeat policy
        8. Maybe transition status (via transition policy)
        9. Record transition history
        COMMIT
        On any error: ROLLBACK EVERYTHING
    """

    def __init__(
        self,
        repo: ResearchRepository,
        policy_config: Optional[FindingRepeatPolicyConfig] = None,
    ) -> None:
        self._repo = repo
        self._config = policy_config  # None = promotion disabled

    # ------------------------------------------------------------------
    # Single candidate ingestion
    # ------------------------------------------------------------------

    def ingest_candidate(
        self,
        candidate: FindingCandidate,
    ) -> FindingIngestionResult:
        """Ingest a single FindingCandidate.

        Owns the full transaction: BEGIN → work → COMMIT (or ROLLBACK on error).
        All repository calls within execute WITHOUT independent commit/rollback.
        Uses SAVEPOINT for concurrent fingerprint dedup.
        """
        conn = self._repo._conn

        # 1. Validate — analysis_run_id is REQUIRED
        if candidate.analysis_run_id is None:
            raise ValueError(
                "FindingCandidate.analysis_run_id is required — "
                "candidates without an analysis_run_id are rejected"
            )

        # 2. Validate — threshold_policy_version must not silently default
        threshold_version = (candidate.threshold_policy_version or "").strip()
        if not threshold_version:
            raise ValueError(
                "FindingCandidate.threshold_policy_version is required — "
                "candidates without a threshold_policy_version are rejected"
            )

        # 3. Normalize → fingerprint payload
        payload = self._build_fingerprint_payload(candidate)
        if payload is None:
            return FindingIngestionResult(
                finding_id=uuid4(),
                fingerprint="",
                policy_action="SKIPPED_INVALID",
            )

        # 4. Compute fingerprint
        fingerprint_hash = FindingFingerprintV1.compute(payload)

        # ── BEGIN transaction ─────────────────────────────────────────
        try:
            conn.cursor().execute("BEGIN")

            # 5. Lookup existing finding by fingerprint
            existing_finding = self._repo.get_finding_by_fingerprint(fingerprint_hash)

            created_finding = False
            created_occurrence = False
            idempotent_replay = False

            if existing_finding is not None:
                finding = existing_finding
            else:
                # 6. Create new finding + fingerprint with SAVEPOINT
                sp_name = "sp_fingerprint_insert"
                cur = conn.cursor()
                cur.execute(f"SAVEPOINT {sp_name}")
                try:
                    finding = self._create_finding_from_candidate(candidate, fingerprint_hash)
                    created_finding = True
                except Exception as exc:
                    # UNIQUE violation → rollback to savepoint, re-read
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    logger.info(
                        "Concurrent fingerprint conflict for %s, re-reading: %s",
                        fingerprint_hash[:12],
                        exc,
                    )
                    finding = self._repo.get_finding_by_fingerprint(fingerprint_hash)
                    if finding is None:
                        conn.cursor().execute("ROLLBACK")
                        raise ValueError(
                            f"Fingerprint {fingerprint_hash[:12]} conflict but "
                            f"finding not found on re-read"
                        ) from exc
                finally:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")

            # 7. Create occurrence (idempotent)
            occurrence, created_occurrence = self._create_occurrence(candidate, finding.finding_id)
            if not created_occurrence:
                idempotent_replay = True

            # 8. Refresh aggregates
            self._repo.refresh_finding_aggregate(finding.finding_id)

            # 9. Evaluate repeat policy
            all_occurrences = self._repo.list_occurrences_for_finding(finding.finding_id)
            occ_dicts = [
                {
                    "confidence": occ.confidence or "LOW",
                    "business_date": occ.business_date,
                    "analysis_run_id": str(occ.analysis_run_id) if occ.analysis_run_id else None,
                    "maturity": occ.maturity or "PROVISIONAL",
                }
                for occ in all_occurrences
            ]
            policy_action = FindingRepeatPolicyV1.evaluate(
                self._config,
                occ_dicts,
                finding.status,
            )

            # 10. Maybe transition status (within same transaction)
            old_status = finding.status
            new_status = self._apply_policy_action(finding.finding_id, policy_action, finding.status)

            # ── COMMIT ───────────────────────────────────────────────
            conn.cursor().execute("COMMIT")

        except Exception:
            conn.cursor().execute("ROLLBACK")
            raise

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
        """Ingest a batch of candidates.

        Per-candidate transaction.  Malformed candidates are skipped
        (increment skipped_invalid) but do NOT stop the batch.
        DB/system failures propagate and stop the batch (raise).
        Do NOT turn outages into summary.errors.
        """
        summary = IngestionBatchSummary(candidates_seen=len(candidates))

        for candidate in candidates:
            try:
                result = self.ingest_candidate(candidate)
                self._update_summary(summary, result)
            except ValueError as exc:
                # Malformed candidate → continue batch
                logger.warning("Skipping malformed candidate: %s", exc)
                summary.skipped_invalid += 1
            except Exception:
                # DB/system failure → propagate, stop batch
                raise

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

        # threshold_policy_version — already validated as non-empty above
        threshold_policy_version = candidate.threshold_policy_version.strip()

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
        """Create a new finding record from a candidate.

        May raise on UNIQUE constraint violation (caller handles conflict).
        """
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
            "threshold_policy_version": candidate.threshold_policy_version.strip(),
        }

        return self._repo.create_finding_with_fingerprint(finding, fp_payload)

    def _create_occurrence(
        self,
        candidate: FindingCandidate,
        finding_id,
    ) -> tuple[Optional[FindingOccurrence], bool]:
        """Create an occurrence record.

        Returns (occurrence, created: bool).
        created=False if the occurrence already exists (idempotent replay).
        """
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
        # Delegate to repository — returns (occurrence, created: bool)
        return self._repo.create_occurrence_if_absent(occurrence)

    def _apply_policy_action(
        self,
        finding_id,
        policy_action: str,
        current_status: str,
    ) -> str:
        """Apply the repeat policy action within the caller's transaction.

        Uses commit=False because the caller (ingest_candidate) owns the transaction.
        Returns the new status (or old status if no change).
        """
        if policy_action == MARK_REPEATED:
            try:
                self._repo.update_finding_status(
                    finding_id, "REPEATED", reason="repeat_policy", commit=False,
                )
                return "REPEATED"
            except ValueError as exc:
                logger.warning("Status transition failed: %s", exc)
                return current_status

        if policy_action == MARK_RESEARCH_REQUIRED:
            try:
                self._repo.update_finding_status(
                    finding_id, "RESEARCH_REQUIRED", reason="repeat_policy", commit=False,
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
