"""Structured error taxonomy for Stage 3 agent orchestration.

All error codes used by agent orchestration, executor, and retry logic live here.
The ``classify_error`` function maps raw exceptions and status codes into
typed ``AgentErrorCode`` values so that retry policies and observability
dashboards share a single source of truth.

Error categories:
  MODEL_*    — LLM provider / network errors
  SCHEMA_*   — input/output contract violations
  DATA_*     — dataset quality or availability issues
  AGENT_*    — internal orchestration / logic errors
  UNKNOWN_*  — uncategorised fallback
"""
from __future__ import annotations

import enum
from typing import Optional


class AgentErrorCode(enum.Enum):
    """Canonical error codes for agent execution failures."""

    # ── Data ──────────────────────────────────────────────────────────
    DATASET_NOT_READY = "DATASET_NOT_READY"
    DATASET_COVERAGE_INSUFFICIENT = "DATASET_COVERAGE_INSUFFICIENT"
    DATA_QUALITY_FAIL = "DATA_QUALITY_FAIL"
    QUALITY_GATE_FAILED = "QUALITY_GATE_FAILED"

    # ── Model / provider ──────────────────────────────────────────────
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    MODEL_REFUSAL = "MODEL_REFUSAL"
    MODEL_EMPTY_RESPONSE = "MODEL_EMPTY_RESPONSE"

    # ── Schema / contract ─────────────────────────────────────────────
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    SCHEMA_REPAIR_FAILED = "SCHEMA_REPAIR_FAILED"

    # ── Agent internal ────────────────────────────────────────────────
    REPAIR_FAILED = "REPAIR_FAILED"
    INPUT_ASSEMBLY_ERROR = "INPUT_ASSEMBLY_ERROR"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    EVIDENCE_VALIDATION_ERROR = "EVIDENCE_VALIDATION_ERROR"
    CRASH_RECOVERY = "CRASH_RECOVERY"
    ORCHESTRATION_ERROR = "ORCHESTRATION_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    SECURITY_POLICY_ERROR = "SECURITY_POLICY_ERROR"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    CHIEF_NOT_ELIGIBLE = "CHIEF_NOT_ELIGIBLE"

    # ── Unknown / fallback ────────────────────────────────────────────
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class RetryableError(Exception):
    """Transient error that may succeed on retry (rate-limit, timeout)."""

    def __init__(self, code: AgentErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class RepairableError(Exception):
    """Error that can be fixed with a single repair attempt (bad JSON, schema drift)."""

    def __init__(self, code: AgentErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class TerminalError(Exception):
    """Unrecoverable error — no retry or repair will help."""

    def __init__(self, code: AgentErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


# ---------------------------------------------------------------------------
# HTTP status → error code mapping
# ---------------------------------------------------------------------------

_HTTP_STATUS_MAP: dict[int, AgentErrorCode] = {
    408: AgentErrorCode.MODEL_TIMEOUT,
    429: AgentErrorCode.MODEL_RATE_LIMIT,
    500: AgentErrorCode.MODEL_PROVIDER_ERROR,
    502: AgentErrorCode.MODEL_PROVIDER_ERROR,
    503: AgentErrorCode.MODEL_PROVIDER_ERROR,
    504: AgentErrorCode.MODEL_TIMEOUT,
}


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_error(
    error: Optional[BaseException] = None,
    http_status: Optional[int] = None,
    error_message: Optional[str] = None,
) -> AgentErrorCode:
    """Classify an error into a typed ``AgentErrorCode``.

    Supports multiple call signatures for compatibility:
      - ``classify_error(exc)``          — classify by exception
      - ``classify_error(exc, http_status=503)`` — HTTP status priority
      - ``classify_error(error_message="rate limit exceeded")`` — by text

    Parameters
    ----------
    error:
        The original exception, if any.
    http_status:
        HTTP status code from the provider, if available.
    error_message:
        Free-text error description for pattern matching.

    Returns
    -------
    AgentErrorCode
        The most specific matching code, or ``UNKNOWN_ERROR``.
    """
    msg = (error_message or "").lower()
    exc_type = type(error).__name__.lower() if error else ""

    # HTTP status first — most reliable signal
    if http_status and http_status in _HTTP_STATUS_MAP:
        return _HTTP_STATUS_MAP[http_status]

    # Check for known error codes on the exception object
    if error is not None and hasattr(error, "code") and isinstance(error.code, AgentErrorCode):
        return error.code

    # Timeout patterns
    if "timeout" in msg or "timed out" in msg or "timeout" in exc_type:
        return AgentErrorCode.MODEL_TIMEOUT

    # Rate limit patterns
    if "rate limit" in msg or "429" in msg or "too many requests" in msg:
        return AgentErrorCode.MODEL_RATE_LIMIT

    # Provider errors
    if "provider" in msg or "service unavailable" in msg or "bad gateway" in msg:
        return AgentErrorCode.MODEL_PROVIDER_ERROR

    # Refusal / content filter
    if "refusal" in msg or "content policy" in msg or "safety" in msg:
        return AgentErrorCode.MODEL_REFUSAL

    # Empty response
    if "empty response" in msg or "no content" in msg:
        return AgentErrorCode.MODEL_EMPTY_RESPONSE

    # Schema errors
    if "schema" in msg or "validation" in msg or "json" in exc_type:
        return AgentErrorCode.SCHEMA_VALIDATION_ERROR

    # Data errors
    if "dataset not ready" in msg or "not ready" in msg:
        return AgentErrorCode.DATASET_NOT_READY
    if "coverage" in msg:
        return AgentErrorCode.DATASET_COVERAGE_INSUFFICIENT
    if "quality" in msg and ("fail" in msg or "check" in msg):
        return AgentErrorCode.DATA_QUALITY_FAIL

    # Network errors
    if "connection" in msg or "network" in msg or "dns" in msg:
        return AgentErrorCode.MODEL_PROVIDER_ERROR

    # Legacy exception-type classification
    # PermissionError MUST be checked BEFORE OSError (it's a subclass)
    if error is not None:
        name = type(error).__name__
        if "timeout" in name.lower() or isinstance(error, TimeoutError):
            return AgentErrorCode.MODEL_TIMEOUT
        if "rate" in name.lower() and "limit" in name.lower():
            return AgentErrorCode.MODEL_RATE_LIMIT
        if isinstance(error, PermissionError):
            return AgentErrorCode.SECURITY_POLICY_ERROR
        if isinstance(error, OSError):
            return AgentErrorCode.MODEL_PROVIDER_ERROR
        if isinstance(error, (ConnectionError,)):
            return AgentErrorCode.MODEL_PROVIDER_ERROR
        if isinstance(error, ValueError):
            return AgentErrorCode.INVALID_INPUT

    return AgentErrorCode.UNKNOWN_ERROR
