"""Production OpenAI-compatible LLM client for Stage 3.

Uses httpx for HTTP calls — no heavy SDK dependency.
Falls back gracefully if provider is not configured.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Any, Mapping, Optional

from app.analytics.agents.llm_client import AgentModelClient, ModelResponse

logger = logging.getLogger(__name__)


class OpenAICompatibleClient(AgentModelClient):
    """OpenAI-compatible API client using httpx.

    Works with OpenAI, Azure OpenAI, and compatible providers
    (vLLM, Ollama, etc.) that implement the /v1/chat/completions endpoint.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        default_model: str = "gpt-4o",
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        request_timeout_s: float = 120.0,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._request_timeout_s = request_timeout_s

    async def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        input_json: Mapping[str, Any],
        response_schema: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 120.0,
    ) -> ModelResponse:
        """Generate a response from the model."""
        import httpx

        effective_timeout = timeout_s or self._request_timeout_s

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(input_json, default=str)},
        ]

        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
        }

        # If response_schema provided, use structured output
        if response_schema:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        start_time = time.monotonic()

        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)

            if response.status_code != 200:
                raise ConnectionError(
                    f"LLM provider returned HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )

            data = response.json()

            # Extract content
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            finish_reason = choice.get("finish_reason", "unknown")

            # Parse JSON
            parsed_json = None
            if content:
                try:
                    parsed_json = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Token usage
            usage = data.get("usage", {})

            return ModelResponse(
                content=content,
                parsed_json=parsed_json,
                model=data.get("model", model),
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                latency_ms=latency_ms,
                finish_reason=finish_reason,
                provider_request_id=data.get("id"),
            )
