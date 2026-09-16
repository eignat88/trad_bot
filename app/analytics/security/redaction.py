"""Centralized secret redaction for Stage 3 security.

Sanitizes secrets from error messages, logs, and persisted data before
they enter agent_run.error_message, provider logs, or report content.

Pattern list covers common secret formats without being overly aggressive.
"""
from __future__ import annotations

import re


# Patterns that indicate a secret value
_PATTERNS: list[re.Pattern] = [
    # API keys / tokens
    re.compile(r'sk-[A-Za-z0-9]{20,}'),            # OpenAI-style
    re.compile(r'Bearer\s+[A-Za-z0-9\._\-]{20,}', re.I),  # Bearer token
    re.compile(r'Authorization["\s:=]+[^\s]{10,}', re.I),   # Authorization header
    # Database credentials
    re.compile(r'postgresql://[^:]+:[^@]+@', re.I),          # postgresql://user:pass@host
    re.compile(r'password[=:]\s*["\']?[^\s"\']{6,}', re.I), # password=
    re.compile(r'PGPASSWORD[=:]\s*\S+', re.I),               # PGPASSWORD=
    # Common env-var patterns
    re.compile(r'OPENAI_API_KEY[=:]\s*\S+', re.I),
    re.compile(r'ANTHROPIC_API_KEY[=:]\s*\S+', re.I),
    re.compile(r'LLM_API_KEY[=:]\s*\S+', re.I),
    re.compile(r'SECRET_KEY[=:]\s*\S+', re.I),
    re.compile(r'API_KEY[=:]\s*["\']?[A-Za-z0-9]{20,}', re.I),
    re.compile(r'API_SECRET[=:]\s*["\']?[A-Za-z0-9]{20,}', re.I),
    re.compile(r'TOKEN[=:]\s*["\']?[A-Za-z0-9]{20,}', re.I),
]


def sanitize(text: str) -> str:
    """Replace secret patterns with [REDACTED] in the given text.
    
    Returns the sanitized string. If no secrets found, returns original.
    Designed for error messages and log strings — not for structured data.
    """
    if not text:
        return text
    
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    
    return result


def contains_secret(text: str) -> bool:
    """Check if text contains a detected secret pattern."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PATTERNS)
