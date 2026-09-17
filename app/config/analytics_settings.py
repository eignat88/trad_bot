"""LLM configuration for Stage 3 analytics agents.

Loaded from environment variables via the existing Settings pattern.
These fields are NEW additions to the analytics configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMSettings:
    """LLM provider configuration for analytics agents."""
    
    # Provider connection
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    
    # Model configuration
    specialist_model: str = "gpt-4o"
    chief_model: str = "gpt-4o"
    
    # Request parameters
    temperature: float = 0.0
    max_output_tokens: int = 4096
    request_timeout_s: float = 120.0
    
    # Budget limits (observability, not enforcement)
    daily_token_warn: int = 1_000_000
    daily_cost_warn_usd: float = 50.0


def load_llm_settings() -> LLMSettings:
    """Load LLM settings from environment variables."""
    return LLMSettings(
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        specialist_model=os.getenv("LLM_SPECIALIST_MODEL", "gpt-4o"),
        chief_model=os.getenv("LLM_CHIEF_MODEL", "gpt-4o"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.0")),
        max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096")),
        request_timeout_s=float(os.getenv("LLM_REQUEST_TIMEOUT_S", "120.0")),
        daily_token_warn=int(os.getenv("LLM_DAILY_TOKEN_WARN", "1000000")),
        daily_cost_warn_usd=float(os.getenv("LLM_DAILY_COST_WARN_USD", "50.0")),
    )
