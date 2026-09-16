"""Production OpenAI-compatible LLM client for Stage 3.

Uses httpx for HTTP calls — no heavy SDK dependency.

Fails closed: missing/invalid api_key raises ValueError at construction.
Provider errors are classified into the Stage 3 error taxonomy.
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

    Fails closed: missing/invalid api_key raises ValueError at construction.
    Provider errors are classified into the Stage 3 error taxonomy.

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
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required and must not be empty/whitespace")
        self._api_key = api_key.strip()
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
        """Generate a response from the model.

        Raises
        ------
        ValueError
            If ``model`` is empty/missing.
        RetryableError
            For transient provider errors (timeout, rate-limit, 5xx).
        TerminalError
            For permanent failures (auth, empty response).
        """
        import httpx
        from app.analytics.agents.errors import AgentErrorCode, RetryableError, TerminalError

        # model is required — no fallback
        if not model:
            raise ValueError("model parameter is required")

        effective_timeout = timeout_s or self._request_timeout_s

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(input_json, default=str)},
        ]

        payload: dict[str, Any] = {
            "model": model,
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

            # ── HTTP error taxonomy ───────────────────────────────────
            if response.status_code == 408 or response.status_code == 504:
                raise RetryableError(
                    AgentErrorCode.MODEL_TIMEOUT,
                    f"HTTP {response.status_code}: request timed out",
                )
            elif response.status_code == 429:
                raise RetryableError(
                    AgentErrorCode.MODEL_RATE_LIMIT,
                    f"HTTP 429: rate limit exceeded",
                )
            elif response.status_code in (500, 502, 503):
                raise RetryableError(
                    AgentErrorCode.MODEL_PROVIDER_ERROR,
                    f"HTTP {response.status_code}: provider error",
                )
            elif response.status_code in (401, 403):
                raise TerminalError(
                    AgentErrorCode.SECURITY_POLICY_ERROR,
                    f"HTTP {response.status_code}: auth failed — "
                    "check api_key and permissions",
                )
            elif response.status_code != 200:
                raise TerminalError(
                    AgentErrorCode.MODEL_PROVIDER_ERROR,
                    f"HTTP {response.status_code}: {response.text[:200]}",
                )

            data = response.json()

            # ── Empty response handling ───────────────────────────────
            if not data.get("choices"):
                raise TerminalError(
                    AgentErrorCode.MODEL_EMPTY_RESPONSE,
                    "No choices in response",
                )

            choice = data["choices"][0]
            content = choice.get("message", {}).get("content", "")

            if not content:
                raise TerminalError(
                    AgentErrorCode.MODEL_EMPTY_RESPONSE,
                    "Empty content in response",
                )

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
