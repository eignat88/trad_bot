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
    AgentRunStatus, ValidationStatus,
)
from app.analytics.agents.llm_client import AgentModelClient, ModelResponse
from app.analytics.agents.contracts.schema_validator import (
    validate_input, validate_specialist_output,
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

    async def execute(
        self,
        definition: AgentDefinition,
        manifest: AgentInputManifest,
        timeout_s: float = 120.0,
    ) -> AgentExecutionResult:
        """Execute a single agent invocation."""
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

            # 1. Build prompt
            prompt_data = self._prompt_builder(definition, manifest)

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

            # 4. Schema validation
            schema_errors = validate_specialist_output(parsed)
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
    ) -> AgentExecutionResult:
        """Attempt to repair a failed execution (schema validation error).

        Per spec: one repair attempt, then FAILED.
        """
        # For repair, we add the error to the prompt and retry
        try:
            # Validate model (fail-closed)
            if not definition.model or not definition.model.strip():
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.INVALID_INPUT,
                    error_message=f"Agent '{definition.agent_name}' has no model configured for repair.",
                )

            prompt_data = self._prompt_builder(definition, manifest)
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

            schema_errors = validate_specialist_output(parsed)
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
