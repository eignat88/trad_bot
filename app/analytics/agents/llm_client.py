"""Provider-neutral LLM abstraction for Stage 3 agents."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class ModelResponse:
    """Result from an LLM invocation."""
    content: str                          # raw text response
    parsed_json: Optional[dict] = None    # structured JSON if parseable
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""               # e.g. "stop", "length", "tool_call"
    provider_request_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentModelClient(ABC):
    """Abstract base for LLM providers.

    Implementations should handle:
    - API key management (from environment/settings)
    - Rate limiting
    - Retries at HTTP level
    - Timeout enforcement

    Business-level retry is handled by the orchestrator.
    """

    @abstractmethod
    async def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        input_json: Mapping[str, Any],
        response_schema: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 120.0,
    ) -> ModelResponse:
        """Generate a response from the model.

        Args:
            model: Model identifier (e.g. "gpt-4o")
            system_prompt: System message
            input_json: User message as structured data
            response_schema: Optional JSON Schema for structured output
            timeout_s: Timeout in seconds

        Returns:
            ModelResponse with parsed result

        Raises:
            TimeoutError: if request exceeds timeout
            ConnectionError: if provider unreachable
            ValueError: if request is malformed
        """
        ...


class StubModelClient(AgentModelClient):
    """Stub client for testing. Returns pre-configured responses."""

    def __init__(self, responses: Optional[list[dict]] = None):
        self._responses = responses or []
        self._call_count = 0
        self.calls: list[dict] = []

    async def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        input_json: Mapping[str, Any],
        response_schema: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 120.0,
    ) -> ModelResponse:
        self._call_count += 1
        self.calls.append({
            "model": model,
            "system_prompt_length": len(system_prompt),
            "input_keys": list(input_json.keys()) if isinstance(input_json, dict) else [],
        })

        if self._call_count <= len(self._responses):
            resp = self._responses[self._call_count - 1]
            return ModelResponse(
                content=resp.get("content", "{}"),
                parsed_json=resp.get("parsed_json"),
                model=model,
                input_tokens=resp.get("input_tokens", 100),
                output_tokens=resp.get("output_tokens", 200),
                total_tokens=resp.get("input_tokens", 100) + resp.get("output_tokens", 200),
                latency_ms=resp.get("latency_ms", 500),
                finish_reason=resp.get("finish_reason", "stop"),
            )

        return ModelResponse(
            content='{"summary": "stub", "observations": [], "hypotheses": [], '
                    '"proposed_experiments": [], "anomalies": [], "confidence": "LOW", '
                    '"limitations": ["stub"], "evidence_refs": []}',
            parsed_json={
                "summary": "stub",
                "observations": [],
                "hypotheses": [],
                "proposed_experiments": [],
                "anomalies": [],
                "confidence": "LOW",
                "limitations": ["stub"],
                "evidence_refs": [],
            },
            model=model,
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            latency_ms=100,
            finish_reason="stop",
        )
