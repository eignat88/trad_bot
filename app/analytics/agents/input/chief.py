"""Chief Trading Analyst input assembler.

Builds chief input from validated specialist results.
Chief receives ONLY validated outputs — no raw DB access.
"""
from __future__ import annotations
import json
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from app.analytics.agents.chief_policy import ChiefEligibility

logger = logging.getLogger(__name__)

# Required specialist agent names (order matches prompt expectations)
_REQUIRED_SPECIALISTS = (
    "FUNNEL_AND_PERFORMANCE",
    "EXECUTION_QUALITY",
    "DRIFT_AND_ANOMALY",
)

MAX_EVIDENCE_REFS = 200
MAX_SPECIALIST_OUTPUT_FIELDS = 50


@dataclass
class ChiefInput:
    """Assembled input for the Chief Trading Analyst.

    Attributes
    ----------
    run_id:
        Unique identifier for this analysis run.
    dataset_version:
        Version string identifying the data snapshot.
    maturity:
        PROVISIONAL or FINAL.
    data_quality_status:
        PASS or DEGRADED.
    limitations:
        Data quality limitations merged across all sources.
    specialists:
        Ordered list of specialist result dicts, each containing
        agent_name, status, and output (or None).
    missing_agents:
        Names of specialists that failed or were unavailable.
    aggregate_evidence_refs:
        Deduplicated evidence references from all specialist outputs.
    aggregate_evidence_catalog:
        Catalog entries populated by the caller (orchestrator).
    specialist_result_hashes:
        Mapping of agent_name → result_hash for integrity checks.
    analysis_window_from:
        Start of the analysis window (ISO 8601).
    analysis_window_to:
        End of the analysis window (ISO 8601).
    """
    run_id: UUID
    dataset_version: str
    maturity: str
    data_quality_status: str
    limitations: list[str]
    specialists: list[dict[str, Any]]
    missing_agents: list[str]
    aggregate_evidence_refs: list[str]
    aggregate_evidence_catalog: list[dict[str, Any]]
    specialist_result_hashes: dict[str, str]
    analysis_window_from: str
    analysis_window_to: str


class ChiefInputAssembler:
    """Assembles Chief input from validated specialist results.

    The assembler does NOT access any database — it operates purely on
    validated specialist outputs passed by the orchestrator.  This
    enforces the architectural boundary where the Chief only sees
    sanitized, validated outputs.
    """

    def assemble(
        self,
        *,
        run_id: UUID,
        dataset_version: str,
        maturity: str,
        quality_status: str,
        limitations: list[str],
        analysis_window_from: str,
        analysis_window_to: str,
        specialist_results: dict[str, Any],
        chief_eligibility: ChiefEligibility,
    ) -> ChiefInput:
        """Build Chief input from specialist outputs.

        Parameters
        ----------
        run_id:
            Analysis run identifier.
        dataset_version:
            Data snapshot version.
        maturity:
            PROVISIONAL or FINAL.
        quality_status:
            PASS or DEGRADED.
        limitations:
            Data quality limitations from the orchestrator.
        analysis_window_from:
            Start of the analysis window.
        analysis_window_to:
            End of the analysis window.
        specialist_results:
            Mapping of ``agent_name`` → ``AgentExecutionResult`` (or None).
            The ``AgentExecutionResult`` contains ``status`` (AgentRunStatus)
            and ``result`` (AgentResult with ``result_json`` and ``result_hash``).
        chief_eligibility:
            Result from ``check_chief_eligibility()``.

        Returns
        -------
        ChiefInput
            Fully assembled input ready for the Chief Trading Analyst prompt.
        """
        specialists: list[dict[str, Any]] = []
        all_evidence_refs: list[str] = []
        result_hashes: dict[str, str] = {}

        for agent_name in _REQUIRED_SPECIALISTS:
            exec_result = specialist_results.get(agent_name)

            if exec_result is None or exec_result.status.value not in ("SUCCEEDED", "DEGRADED"):
                # Specialist not available — include status but no output
                status_str = "UNAVAILABLE"
                if exec_result is not None:
                    status_str = exec_result.status.value  # FAILED, SKIPPED, etc.
                specialists.append({
                    "agent_name": agent_name,
                    "status": status_str,
                    "output": None,
                })
                continue

            # Valid specialist result
            agent_result = exec_result.result
            output = agent_result.result_json if agent_result else {}
            result_hash = agent_result.result_hash if agent_result else ""

            specialists.append({
                "agent_name": agent_name,
                "status": exec_result.status.value,
                "output": output,
            })

            # Collect evidence refs from top-level
            output_evidence = output.get("evidence_refs", [])
            all_evidence_refs.extend(output_evidence)

            # Collect evidence refs from observations
            for obs in output.get("observations", []):
                all_evidence_refs.extend(obs.get("evidence_refs", []))

            # Collect evidence refs from hypotheses
            for hyp in output.get("hypotheses", []):
                all_evidence_refs.extend(hyp.get("evidence_refs", []))

            result_hashes[agent_name] = result_hash

        # Deduplicate evidence refs while preserving order
        unique_evidence = list(dict.fromkeys(all_evidence_refs))[:MAX_EVIDENCE_REFS]

        # Merge limitations from orchestrator and specialist outputs
        all_limitations = list(limitations)
        for s in specialists:
            if s["output"]:
                all_limitations.extend(s["output"].get("limitations", []))

        return ChiefInput(
            run_id=run_id,
            dataset_version=dataset_version,
            maturity=maturity,
            data_quality_status=quality_status,
            limitations=list(dict.fromkeys(all_limitations)),
            specialists=specialists,
            missing_agents=list(chief_eligibility.missing_agents),
            aggregate_evidence_refs=unique_evidence,
            aggregate_evidence_catalog=[],  # populated by caller
            specialist_result_hashes=result_hashes,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
        )
