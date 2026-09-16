"""Agent executor abstraction for Stage 3.

The executor handles a single agent invocation:
- Build prompt from manifest
- Call LLM
- Parse response
- Validate schema
- Validate evidence
- Return structured result
"""
from __future__ import annotations
import json
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.analytics.agents.models import (
    AgentDefinition, AgentInputManifest, AgentResult,
    AgentRunStatus, AgentType, ValidationStatus,
)
from app.analytics.agents.llm_client import AgentModelClient, ModelResponse
from app.analytics.agents.contracts.schema_validator import (
    validate_input, validate_specialist_output, validate_chief_output,
)
from app.analytics.agents.evidence import EvidenceCatalog
from app.analytics.agents.errors import (
    AgentErrorCode, TerminalError, RepairableError, classify_error,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentExecutionResult:
    """Result of a single agent execution attempt."""
    status: AgentRunStatus
    result: Optional[AgentResult]
    error_code: Optional[AgentErrorCode] = None
    error_message: Optional[str] = None
    model_response: Optional[ModelResponse] = None


class AgentExecutor:
    """Executes a single agent: LLM call + validation."""

    def __init__(
        self,
        model_client: AgentModelClient,
        prompt_builder: Any,  # Callable[[AgentDefinition, AgentInputManifest], dict]
    ):
        self._model_client = model_client
        self._prompt_builder = prompt_builder

    @staticmethod
    def _validate_output_schema(
        definition: AgentDefinition,
        parsed: dict,
    ) -> list[str]:
        """Validate LLM output against the correct schema for this agent type.

        CHIEF agents use chief_output.schema.json.
        SPECIALIST agents use specialist_output.schema.json.
        """
        if definition.agent_type == AgentType.CHIEF:
            return validate_chief_output(parsed)
        return validate_specialist_output(parsed)

    @staticmethod
    def _apply_confidence_policy(
        parsed: dict[str, Any],
        manifest: AgentInputManifest,
    ) -> dict[str, Any]:
        """Apply ConfidencePolicyV1 to the parsed LLM output.

        Downgrades confidence at all levels (top-level, observations,
        hypotheses) if LLM-reported confidence exceeds policy allowance.
        Returns the (potentially modified) parsed dict.
        """
        from app.analytics.agents.policies.confidence_v1 import ConfidencePolicyV1

        policy = ConfidencePolicyV1()

        total_sample = sum(manifest.sample_sizes.values()) if manifest.sample_sizes else 0

        window_periods: set[str] = set()
        for k in (manifest.metrics or {}).keys():
            for period in ["24h", "7d", "30d"]:
                if period in str(k):
                    window_periods.add(period)
        windows_with_effect = len(window_periods)
        has_gaps = any("gap" in lim.lower() for lim in (manifest.limitations or []))

        allowed = policy.evaluate(
            sample_size=total_sample,
            maturity=manifest.maturity,
            data_quality_status=manifest.data_quality_status,
            windows_with_effect=windows_with_effect,
            has_gaps=has_gaps,
        )

        reported = parsed.get("confidence", "LOW")
        confidence_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        if confidence_order.get(reported, 0) > confidence_order.get(allowed.value, 0):
            downgrade_msg = (
                f"Confidence downgraded from {reported} to {allowed.value} "
                f"by ConfidencePolicyV1: sample_size={total_sample}, "
                f"maturity={manifest.maturity}, quality={manifest.data_quality_status}"
            )
            parsed["confidence"] = allowed.value
            if "limitations" not in parsed:
                parsed["limitations"] = []
            parsed["limitations"].append(downgrade_msg)
            logger.info("Confidence downgraded: %s", downgrade_msg)

            for obs in parsed.get("observations", []):
                obs_conf = obs.get("confidence", "LOW")
                if confidence_order.get(obs_conf, 0) > confidence_order.get(allowed.value, 0):
                    obs["confidence"] = allowed.value

            for hyp in parsed.get("hypotheses", []):
                hyp_conf = hyp.get("confidence", "LOW")
                if confidence_order.get(hyp_conf, 0) > confidence_order.get(allowed.value, 0):
                    hyp["confidence"] = allowed.value

        return parsed

    async def execute(
        self,
        definition: AgentDefinition,
        manifest: AgentInputManifest,
        timeout_s: float = 120.0,
        prompt_builder_override: Optional[Any] = None,
    ) -> AgentExecutionResult:
        """Execute a single agent invocation.
        
        prompt_builder_override allows the orchestrator to inject a
        specialist-specific prompt builder instead of the default one.
        """
        try:
            # 0. Validate model is configured (fail-closed)
            if not definition.model or not definition.model.strip():
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.INVALID_INPUT,
                    error_message=(
                        f"Agent '{definition.agent_name}' has no model configured. "
                        "Set definition.model before execution."
                    ),
                )

            # 1. Build prompt (use override if provided, else default)
            prompt_builder = prompt_builder_override or self._prompt_builder
            prompt_data = prompt_builder(definition, manifest)

            # 2. Call LLM — model comes from definition.model (validated above)
            response = await self._model_client.generate(
                model=definition.model,
                system_prompt=prompt_data.get("system_prompt", ""),
                input_json=prompt_data.get("input_json", {}),
                response_schema=prompt_data.get("response_schema"),
                timeout_s=timeout_s,
            )

            # 3. Parse response
            if response.parsed_json is None:
                try:
                    parsed = json.loads(response.content)
                except (json.JSONDecodeError, TypeError):
                    return AgentExecutionResult(
                        status=AgentRunStatus.FAILED,
                        result=None,
                        error_code=AgentErrorCode.SCHEMA_VALIDATION_ERROR,
                        error_message="Failed to parse LLM response as JSON",
                        model_response=response,
                    )
            else:
                parsed = response.parsed_json

            # 4. Schema validation — contract-aware
            schema_errors = self._validate_output_schema(definition, parsed)
            if schema_errors:
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.SCHEMA_VALIDATION_ERROR,
                    error_message=f"Schema validation failed: {'; '.join(schema_errors)}",
                    model_response=response,
                )

            # 5. Evidence validation — build NEW list to avoid mutating parsed result
            catalog = EvidenceCatalog(manifest.evidence_ids)
            evidence_refs: list[str] = list(parsed.get("evidence_refs", []))
            # Also collect from observations and hypotheses (extend the copy, not original)
            for obs in parsed.get("observations", []):
                evidence_refs.extend(obs.get("evidence_refs", []))
            for hyp in parsed.get("hypotheses", []):
                evidence_refs.extend(hyp.get("evidence_refs", []))

            unique_refs = list(set(evidence_refs))
            invalid_refs = catalog.validate_refs(unique_refs)
            if invalid_refs:
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.EVIDENCE_VALIDATION_ERROR,
                    error_message=f"Unknown evidence references: {invalid_refs}",
                    model_response=response,
                )

            # 5b. Confidence policy enforcement
            parsed = self._apply_confidence_policy(parsed, manifest)

            # 6. Compute result hash
            result_json = parsed
            result_hash = hashlib.sha256(
                json.dumps(result_json, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            # 7. Build AgentResult
            agent_result = AgentResult(
                agent_run_id=manifest.agent_run_id,
                schema_version=manifest.schema_version,
                result_json=result_json,
                result_hash=result_hash,
                validation_status=ValidationStatus.VALID,
            )

            return AgentExecutionResult(
                status=AgentRunStatus.SUCCEEDED,
                result=agent_result,
                model_response=response,
            )

        except Exception as e:
            error_code = classify_error(e)
            logger.exception("Agent execution failed: %s", e)
            return AgentExecutionResult(
                status=AgentRunStatus.FAILED,
                result=None,
                error_code=error_code,
                error_message=str(e),
            )

    async def repair(
        self,
        definition: AgentDefinition,
        manifest: AgentInputManifest,
        original_error: str,
        timeout_s: float = 120.0,
        prompt_builder_override: Optional[Any] = None,
    ) -> AgentExecutionResult:
        """Attempt to repair a failed execution (schema validation error).

        Per spec: one repair attempt, then FAILED.
        Uses the SAME input and specialist prompt as the original execution.
        """
        try:
            # Validate model (fail-closed)
            if not definition.model or not definition.model.strip():
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.INVALID_INPUT,
                    error_message=f"Agent '{definition.agent_name}' has no model configured for repair.",
                )

            prompt_builder = prompt_builder_override or self._prompt_builder
            prompt_data = prompt_builder(definition, manifest)
            repair_prompt = (
                prompt_data.get("system_prompt", "")
                + f"\n\nIMPORTANT: Your previous response had validation errors: {original_error}. "
                  "Please fix the issues and return valid JSON matching the required schema."
            )

            response = await self._model_client.generate(
                model=definition.model,
                system_prompt=repair_prompt,
                input_json=prompt_data.get("input_json", {}),
                response_schema=prompt_data.get("response_schema"),
                timeout_s=timeout_s,
            )

            if response.parsed_json is None:
                try:
                    parsed = json.loads(response.content)
                except (json.JSONDecodeError, TypeError):
                    return AgentExecutionResult(
                        status=AgentRunStatus.FAILED,
                        result=None,
                        error_code=AgentErrorCode.REPAIR_FAILED,
                        error_message="Repair attempt also failed to parse JSON",
                        model_response=response,
                    )
            else:
                parsed = response.parsed_json

            schema_errors = self._validate_output_schema(definition, parsed)
            if schema_errors:
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.REPAIR_FAILED,
                    error_message=f"Repair validation also failed: {'; '.join(schema_errors)}",
                    model_response=response,
                )

            # Evidence validation for repair — NEW list to avoid mutating parsed
            catalog = EvidenceCatalog(manifest.evidence_ids)
            evidence_refs: list[str] = list(parsed.get("evidence_refs", []))
            for obs in parsed.get("observations", []):
                evidence_refs.extend(obs.get("evidence_refs", []))
            for hyp in parsed.get("hypotheses", []):
                evidence_refs.extend(hyp.get("evidence_refs", []))
            invalid_refs = catalog.validate_refs(list(set(evidence_refs)))
            if invalid_refs:
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.REPAIR_FAILED,
                    error_message=f"Repair evidence validation failed: {invalid_refs}",
                    model_response=response,
                )

            # Confidence enforcement (same as execute path)
            parsed = self._apply_confidence_policy(parsed, manifest)

            result_hash = hashlib.sha256(
                json.dumps(parsed, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            agent_result = AgentResult(
                agent_run_id=manifest.agent_run_id,
                schema_version=manifest.schema_version,
                result_json=parsed,
                result_hash=result_hash,
                validation_status=ValidationStatus.VALID,
            )

            return AgentExecutionResult(
                status=AgentRunStatus.SUCCEEDED,
                result=agent_result,
                model_response=response,
            )

        except Exception as e:
            return AgentExecutionResult(
                status=AgentRunStatus.FAILED,
                result=None,
                error_code=AgentErrorCode.REPAIR_FAILED,
                error_message=str(e),
            )
