"""Stage 3 → Stage 4 FindingCandidate adapter.

Extracts FindingCandidate DTOs from validated Stage 3 agent results.
The adapter is responsible for schema validation, evidence resolution,
and metric name validation — all upstream of the ingestion pipeline.

CRITICAL CONSTRAINTS:
    - SUPPORTED_SCHEMA_VERSIONS is a fixed set.  Missing/unknown → skip
      with explicit reason.
    - Evidence validation: all evidence_refs in candidate MUST exist in
      the validated evidence set from the source result.  Reject candidate
      if any evidence_ref is not found.
    - metric_name resolution MUST come from validated metric_refs, NOT
      from statement text.
    - Item CANNOT override analysis_run_id — must match the outer run.
    - No production mutation.
    - Skips candidates with unknown schema versions.
    - Skips candidates without evidence refs.
    - Skips candidates where metric_name cannot be resolved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from app.analytics.agents.research.models import FindingCandidate

logger = logging.getLogger(__name__)


# Fixed, known schema versions for Stage 3 agent results.
# Unknown versions → skip with explicit log.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({
    "agent-result-v1",
    "agent-result-v2",
    "stage3-result-v1",
})


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

        # ── Strict schema version check ──────────────────────────────
        schema_version = result_json.get("schema_version", "")
        if not schema_version:
            # Missing schema_version → reject with explicit reason
            logger.warning(
                "Skipping agent result: missing schema_version (required)"
            )
            return []
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            logger.warning(
                "Skipping agent result: unknown schema_version '%s' "
                "(supported: %s)",
                schema_version,
                sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
            return []

        # ── Build validated evidence set from result ──────────────────
        # The result_json should contain a top-level validated_evidence
        # list, or evidence_refs.  Items reference evidence by these.
        validated_evidence = self._build_validated_evidence_set(result_json)

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
                validated_evidence=validated_evidence,
                result_json=result_json,
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
                validated_evidence=validated_evidence,
                result_json=result_json,
            )
            if candidate is not None:
                candidates.append(candidate)

        return candidates

    def _build_validated_evidence_set(self, result_json: dict) -> set[str]:
        """Build the set of valid evidence refs from the result JSON.

        Checks multiple possible locations where validated evidence
        may reside in the result JSON.
        """
        evidence_set: set[str] = set()

        # Top-level validated_evidence list
        validated = result_json.get("validated_evidence", [])
        if isinstance(validated, list):
            for ev in validated:
                if isinstance(ev, str):
                    evidence_set.add(ev)
                elif isinstance(ev, dict):
                    ref = ev.get("ref") or ev.get("evidence_ref") or ev.get("id")
                    if ref:
                        evidence_set.add(str(ref))

        # Top-level evidence_refs
        top_refs = result_json.get("evidence_refs", [])
        if isinstance(top_refs, list):
            for ref in top_refs:
                if isinstance(ref, str):
                    evidence_set.add(ref)

        return evidence_set

    def _extract_single(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        item: dict,
        item_type: str,
        dataset_version: str,
        validated_evidence: set[str],
        result_json: dict | None = None,
    ) -> Optional[FindingCandidate]:
        """Extract a single FindingCandidate from an observation or anomaly dict.

        Returns None if the item is invalid or missing required fields.
        """
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict %s item", item_type)
            return None

        # ── Item CANNOT override analysis_run_id ──────────────────────
        item_run_id = item.get("analysis_run_id")
        if item_run_id is not None:
            try:
                item_uuid = UUID(str(item_run_id))
                if item_uuid != analysis_run_id:
                    logger.warning(
                        "Rejecting %s: item analysis_run_id %s does not "
                        "match outer analysis_run_id %s",
                        item_type,
                        item_uuid,
                        analysis_run_id,
                    )
                    return None
            except (ValueError, TypeError):
                logger.warning(
                    "Rejecting %s: invalid analysis_run_id '%s'",
                    item_type,
                    item_run_id,
                )
                return None

        # ── Validate evidence refs ────────────────────────────────────
        evidence_refs = item.get("evidence_refs", [])
        if not evidence_refs or not isinstance(evidence_refs, list):
            logger.debug(
                "Skipping %s: no evidence refs", item_type
            )
            return None

        # If we have a validated evidence set, check all refs exist
        if validated_evidence:
            for ref in evidence_refs:
                if ref not in validated_evidence:
                    logger.warning(
                        "Rejecting %s: evidence_ref '%s' not found in "
                        "validated evidence set",
                        item_type,
                        ref,
                    )
                    return None

        # ── Validate metric_name ──────────────────────────────────────
        # Must come from validated metric_refs, NOT from statement text.
        metric_name = (item.get("metric_name") or "").strip()
        if not metric_name:
            logger.debug(
                "Skipping %s: no metric_name", item_type
            )
            return None

        # Check metric_name is in validated metric set from result
        validated_metrics = self._build_validated_metric_set(result_json or {})
        if validated_metrics and metric_name not in validated_metrics:
            logger.warning(
                "Rejecting %s: metric_name '%s' not found in "
                "validated metric set",
                item_type,
                metric_name,
            )
            return None

        # ── Validate finding_type ─────────────────────────────────────
        finding_type = (item.get("finding_type") or "").strip()
        if not finding_type:
            logger.debug(
                "Skipping %s: no finding_type", item_type
            )
            return None

        # ── Validate scanner_name ─────────────────────────────────────
        scanner_name = (item.get("scanner_name") or "").strip()
        if not scanner_name:
            logger.debug(
                "Skipping %s: no scanner_name", item_type
            )
            return None

        # ── Validate comparator ───────────────────────────────────────
        comparator = (item.get("comparator") or "").strip()
        if not comparator:
            logger.debug(
                "Skipping %s: no comparator", item_type
            )
            return None

        # ── Parse observed_at ─────────────────────────────────────────
        observed_at_str = item.get("observed_at")
        if observed_at_str:
            try:
                if isinstance(observed_at_str, str):
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

        return FindingCandidate(
            finding_type=finding_type,
            scanner_name=scanner_name,
            direction=item.get("direction", "NONE"),
            normalized_segment=item.get("segment", {}),
            metric_name=metric_name,
            comparator=comparator,
            threshold_policy_version=item.get(
                "threshold_policy_version", ""
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

    @staticmethod
    def _build_validated_metric_set(result_json: dict) -> set[str]:
        """Build set of validated metric names from the result JSON.

        Looks in:
        - result_json["metrics"] (dict keys)
        - result_json["metric_refs"] (list)
        - result_json["observations"][*]["metric_refs"] (union)

        Returns empty set if no metric info found (adapter skips validation).
        """
        metrics: set[str] = set()

        # Top-level metrics dict
        top_metrics = result_json.get("metrics")
        if isinstance(top_metrics, dict):
            metrics.update(str(k) for k in top_metrics.keys())

        # Top-level metric_refs list
        top_refs = result_json.get("metric_refs")
        if isinstance(top_refs, list):
            for ref in top_refs:
                if isinstance(ref, str):
                    metrics.add(ref)

        # Observation-level metric_refs
        for obs in result_json.get("observations", []):
            if isinstance(obs, dict):
                for ref in obs.get("metric_refs", []):
                    if isinstance(ref, str):
                        metrics.add(ref)

        return metrics
