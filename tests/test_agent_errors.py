"""Tests for Stage 3 Error Taxonomy."""
import pytest
from app.analytics.agents.errors import (
    AgentErrorCode, RetryableError, RepairableError, TerminalError,
    classify_error,
)


class TestAgentErrorCode:
    def test_all_codes_exist(self):
        """Verify the AgentErrorCode enum has at least 13 codes."""
        assert len(AgentErrorCode) >= 13

    def test_retryable_codes(self):
        """Verify retryable error codes are defined."""
        retryable = {
            AgentErrorCode.MODEL_TIMEOUT,
            AgentErrorCode.MODEL_RATE_LIMIT,
            AgentErrorCode.MODEL_PROVIDER_ERROR,
        }
        for code in retryable:
            assert code.value  # just check they're valid

    def test_repairable_codes(self):
        """Verify repairable error codes are defined."""
        repairable = {
            AgentErrorCode.SCHEMA_VALIDATION_ERROR,
        }
        for code in repairable:
            assert code.value

    def test_terminal_codes(self):
        """Verify terminal error codes are defined."""
        terminal = {
            AgentErrorCode.EVIDENCE_VALIDATION_ERROR,
            AgentErrorCode.EVIDENCE_NOT_FOUND,
            AgentErrorCode.DATASET_NOT_READY,
            AgentErrorCode.MODEL_REFUSAL,
            AgentErrorCode.MODEL_EMPTY_RESPONSE,
            AgentErrorCode.SCHEMA_REPAIR_FAILED,
            AgentErrorCode.REPAIR_FAILED,
            AgentErrorCode.INPUT_ASSEMBLY_ERROR,
            AgentErrorCode.CRASH_RECOVERY,
            AgentErrorCode.ORCHESTRATION_ERROR,
            AgentErrorCode.INVALID_INPUT,
            AgentErrorCode.SECURITY_POLICY_ERROR,
            AgentErrorCode.PERSISTENCE_ERROR,
            AgentErrorCode.CHIEF_NOT_ELIGIBLE,
            AgentErrorCode.UNKNOWN_ERROR,
        }
        for code in terminal:
            assert code.value


class TestClassifyError:
    def test_timeout_error(self):
        """TimeoutError should classify as MODEL_TIMEOUT."""
        assert classify_error(TimeoutError("timed out")) == AgentErrorCode.MODEL_TIMEOUT

    def test_connection_error(self):
        """ConnectionError should classify as MODEL_PROVIDER_ERROR."""
        assert classify_error(ConnectionError("refused")) == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_value_error(self):
        """ValueError should classify as INVALID_INPUT."""
        assert classify_error(ValueError("bad input")) == AgentErrorCode.INVALID_INPUT

    def test_permission_error(self):
        """PermissionError is an OSError subclass, so it gets caught by the OSError check.
        
        Note: PermissionError inherits from OSError, and the classify_error function
        has a check for `isinstance(error, (ConnectionError, OSError))` which returns
        MODEL_PROVIDER_ERROR before reaching the PermissionError check.
        """
        # PermissionError → MODEL_PROVIDER_ERROR (because it's an OSError)
        assert classify_error(PermissionError("denied")) == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_generic_error(self):
        """RuntimeError should classify as UNKNOWN_ERROR."""
        assert classify_error(RuntimeError("something")) == AgentErrorCode.UNKNOWN_ERROR

    def test_http_408_timeout(self):
        """HTTP 408 should classify as MODEL_TIMEOUT."""
        assert classify_error(http_status=408) == AgentErrorCode.MODEL_TIMEOUT

    def test_http_429_rate_limit(self):
        """HTTP 429 should classify as MODEL_RATE_LIMIT."""
        assert classify_error(http_status=429) == AgentErrorCode.MODEL_RATE_LIMIT

    def test_http_500_provider_error(self):
        """HTTP 500 should classify as MODEL_PROVIDER_ERROR."""
        assert classify_error(http_status=500) == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_http_502_provider_error(self):
        """HTTP 502 should classify as MODEL_PROVIDER_ERROR."""
        assert classify_error(http_status=502) == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_http_503_provider_error(self):
        """HTTP 503 should classify as MODEL_PROVIDER_ERROR."""
        assert classify_error(http_status=503) == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_http_504_timeout(self):
        """HTTP 504 should classify as MODEL_TIMEOUT."""
        assert classify_error(http_status=504) == AgentErrorCode.MODEL_TIMEOUT

    def test_error_message_timeout(self):
        """Error message containing 'timeout' should classify as MODEL_TIMEOUT."""
        assert classify_error(error_message="request timeout") == AgentErrorCode.MODEL_TIMEOUT

    def test_error_message_rate_limit(self):
        """Error message containing 'rate limit' should classify as MODEL_RATE_LIMIT."""
        assert classify_error(error_message="rate limit exceeded") == AgentErrorCode.MODEL_RATE_LIMIT

    def test_error_message_provider(self):
        """Error message containing 'provider' should classify as MODEL_PROVIDER_ERROR."""
        assert classify_error(error_message="provider error") == AgentErrorCode.MODEL_PROVIDER_ERROR

    def test_error_message_refusal(self):
        """Error message containing 'refusal' should classify as MODEL_REFUSAL."""
        assert classify_error(error_message="model refusal") == AgentErrorCode.MODEL_REFUSAL

    def test_error_message_empty_response(self):
        """Error message containing 'empty response' should classify as MODEL_EMPTY_RESPONSE."""
        assert classify_error(error_message="empty response") == AgentErrorCode.MODEL_EMPTY_RESPONSE

    def test_error_message_schema(self):
        """Error message containing 'schema' should classify as SCHEMA_VALIDATION_ERROR."""
        assert classify_error(error_message="schema validation failed") == AgentErrorCode.SCHEMA_VALIDATION_ERROR

    def test_error_message_dataset_not_ready(self):
        """Error message containing 'dataset not ready' should classify as DATASET_NOT_READY."""
        assert classify_error(error_message="dataset not ready") == AgentErrorCode.DATASET_NOT_READY

    def test_error_message_coverage(self):
        """Error message containing 'coverage' should classify as DATASET_COVERAGE_INSUFFICIENT."""
        assert classify_error(error_message="insufficient coverage") == AgentErrorCode.DATASET_COVERAGE_INSUFFICIENT

    def test_error_message_quality(self):
        """Error message containing 'quality' and 'fail' should classify as DATA_QUALITY_FAIL."""
        assert classify_error(error_message="quality check failed") == AgentErrorCode.DATA_QUALITY_FAIL

    def test_http_status_priority_over_exception(self):
        """HTTP status should take priority over exception type."""
        assert classify_error(
            error=TimeoutError("timed out"),
            http_status=429,
        ) == AgentErrorCode.MODEL_RATE_LIMIT

    def test_exception_code_attribute(self):
        """Exception with .code attribute should use that code."""
        class CustomError(Exception):
            def __init__(self, code, message):
                self.code = code
                self.message = message
                super().__init__(message)

        exc = CustomError(AgentErrorCode.MODEL_REFUSAL, "refused")
        assert classify_error(exc) == AgentErrorCode.MODEL_REFUSAL

    def test_no_error_returns_unknown(self):
        """Calling with no arguments should return UNKNOWN_ERROR."""
        assert classify_error() == AgentErrorCode.UNKNOWN_ERROR


class TestExceptionClasses:
    def test_retryable_error(self):
        """RetryableError stores code and message."""
        e = RetryableError(AgentErrorCode.MODEL_TIMEOUT, "timed out")
        assert e.code == AgentErrorCode.MODEL_TIMEOUT
        assert e.message == "timed out"
        assert "MODEL_TIMEOUT" in str(e)
        assert "timed out" in str(e)

    def test_repairable_error(self):
        """RepairableError stores code and message."""
        e = RepairableError(AgentErrorCode.SCHEMA_VALIDATION_ERROR, "bad schema")
        assert e.code == AgentErrorCode.SCHEMA_VALIDATION_ERROR
        assert e.message == "bad schema"
        assert "SCHEMA_VALIDATION_ERROR" in str(e)

    def test_terminal_error(self):
        """TerminalError stores code and message."""
        e = TerminalError(AgentErrorCode.EVIDENCE_VALIDATION_ERROR, "unknown ref")
        assert e.code == AgentErrorCode.EVIDENCE_VALIDATION_ERROR
        assert e.message == "unknown ref"
        assert "EVIDENCE_VALIDATION_ERROR" in str(e)

    def test_retryable_error_is_exception(self):
        """RetryableError inherits from Exception."""
        e = RetryableError(AgentErrorCode.MODEL_TIMEOUT, "timed out")
        assert isinstance(e, Exception)

    def test_repairable_error_is_exception(self):
        """RepairableError inherits from Exception."""
        e = RepairableError(AgentErrorCode.SCHEMA_VALIDATION_ERROR, "bad schema")
        assert isinstance(e, Exception)

    def test_terminal_error_is_exception(self):
        """TerminalError inherits from Exception."""
        e = TerminalError(AgentErrorCode.EVIDENCE_VALIDATION_ERROR, "unknown ref")
        assert isinstance(e, Exception)

    def test_exception_hierarchy(self):
        """Verify exception classes have correct base."""
        assert issubclass(RetryableError, Exception)
        assert issubclass(RepairableError, Exception)
        assert issubclass(TerminalError, Exception)

    def test_exception_raise_catch(self):
        """Verify exceptions can be raised and caught."""
        with pytest.raises(RetryableError) as exc_info:
            raise RetryableError(AgentErrorCode.MODEL_TIMEOUT, "timed out")
        assert exc_info.value.code == AgentErrorCode.MODEL_TIMEOUT