"""Stage 3 → Stage 4 FindingCandidate adapter.

Extracts FindingCandidate DTOs from validated Stage 3 agent results.
The adapter is responsible for schema validation, evidence resolution,
and metric name validation — all upstream of the ingestion pipeline.

Design constraints:
    - Python/SQL calculates, LLM interprets (no financial metric computation)
    - Only reads analytics.* and research.* schema
    - Skips candidates with unknown schema versions
    - Skips candidates without evidence refs
    - Skips candidates where metric_name cannot be resolved
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from app.analytics.agents.research.models import FindingCandidate

logger = logging.getLogger(__name__)


# Known schema versions for Stage 3 agent results
SUPPORTED_SCHEMA_VERSIONS: set[str] = {
    "agent-result-v1",
    "agent-result-v2",
    "stage3-result-v1",
}


class Stage3FindingCandidateAdapter:
    """Extract FindingCandidate DTOs from Stage 3 agent results.

    The adapter reads a validated agent_result JSON and produces a list
    of FindingCandidate objects for ingestion.
    """

    def __init__(self, repo: Any = None) -> None:
        self._repo = repo

    def extract_candidates(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        result_json: dict,
    ) -> list[FindingCandidate]:
        """Extract FindingCandidates from a validated agent result.

        Parameters:
            analysis_run_id: The analytics analysis_run UUID
            agent_name:      Name of the Stage 3 agent
            result_json:     Validated agent result JSON

        Returns:
            List of FindingCandidate objects.  May be empty if no valid
            observations are found.
        """
        candidates: list[FindingCandidate] = []

        # Check schema version
        schema_version = result_json.get("schema_version", "")
        if schema_version and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            logger.warning(
                "Skipping agent result: unknown schema_version '%s'",
                schema_version,
            )
            return []

        # Extract observations and anomalies
        observations = result_json.get("observations", [])
        anomalies = result_json.get("anomalies", [])

        for obs in observations:
            candidate = self._extract_single(
                analysis_run_id=analysis_run_id,
                agent_name=agent_name,
                item=obs,
                item_type="observation",
                dataset_version=result_json.get("dataset_version", ""),
            )
            if candidate is not None:
                candidates.append(candidate)

        for anom in anomalies:
            candidate = self._extract_single(
                analysis_run_id=analysis_run_id,
                agent_name=agent_name,
                item=anom,
                item_type="anomaly",
                dataset_version=result_json.get("dataset_version", ""),
            )
            if candidate is not None:
                candidates.append(candidate)

        return candidates

    def _extract_single(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        item: dict,
        item_type: str,
        dataset_version: str,
    ) -> Optional[FindingCandidate]:
        """Extract a single FindingCandidate from an observation or anomaly dict.

        Returns None if the item is invalid or missing required fields.
        """
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict %s item", item_type)
            return None

        # Validate evidence refs
        evidence_refs = item.get("evidence_refs", [])
        if not evidence_refs or not isinstance(evidence_refs, list):
            logger.debug(
                "Skipping %s: no evidence refs", item_type
            )
            return None

        # Validate metric_name
        metric_name = (item.get("metric_name") or "").strip()
        if not metric_name:
            logger.debug(
                "Skipping %s: no metric_name", item_type
            )
            return None

        # Validate finding_type
        finding_type = (item.get("finding_type") or "").strip()
        if not finding_type:
            logger.debug(
                "Skipping %s: no finding_type", item_type
            )
            return None

        # Validate scanner_name
        scanner_name = (item.get("scanner_name") or "").strip()
        if not scanner_name:
            logger.debug(
                "Skipping %s: no scanner_name", item_type
            )
            return None

        # Validate comparator
        comparator = (item.get("comparator") or "").strip()
        if not comparator:
            logger.debug(
                "Skipping %s: no comparator", item_type
            )
            return None

        # Parse observed_at
        observed_at_str = item.get("observed_at")
        if observed_at_str:
            try:
                if isinstance(observed_at_str, str):
                    # Try ISO format
                    observed_at = datetime.fromisoformat(
                        observed_at_str.replace("Z", "+00:00")
                    )
                elif isinstance(observed_at_str, datetime):
                    observed_at = observed_at_str
                else:
                    observed_at = datetime.now(timezone.utc)
            except (ValueError, TypeError):
                observed_at = datetime.now(timezone.utc)
        else:
            observed_at = datetime.now(timezone.utc)

        # Parse analysis_run_id from item (override if present)
        item_run_id = item.get("analysis_run_id")
        if item_run_id:
            try:
                analysis_run_id = UUID(str(item_run_id))
            except (ValueError, TypeError):
                pass  # Use the passed-in run_id

        return FindingCandidate(
            finding_type=finding_type,
            scanner_name=scanner_name,
            direction=item.get("direction", "NONE"),
            normalized_segment=item.get("segment", {}),
            metric_name=metric_name,
            comparator=comparator,
            threshold_policy_version=item.get(
                "threshold_policy_version", "finding-thresholds-v1"
            ),
            statement=item.get("statement", item.get("description", "")),
            scope_json=item.get("scope", {}),
            metric_value=item.get("metric_value"),
            sample_size=int(item.get("sample_size", 0)),
            confidence=item.get("confidence", "LOW"),
            evidence_refs=evidence_refs,
            analysis_run_id=analysis_run_id,
            agent_run_id=item.get("agent_run_id"),
            dataset_version=dataset_version,
            observed_at=observed_at,
            source_agent_name=agent_name,
            business_date=item.get("business_date"),
            maturity=item.get("maturity", "PROVISIONAL"),
        )
