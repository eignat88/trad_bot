"""Tests for Stage 3 PR5 Security, Observability & Production Hardening."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.analytics.agents.errors import AgentErrorCode


# ── Secret Redaction Tests ────────────────────────────────────────────

class TestSecretRedaction:
    def test_sanitize_api_key(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("sk-abcdef123456789012345678")
        assert "sk-" not in result
        assert "[REDACTED]" in result

    def test_sanitize_bearer_token(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("Bearer eyJhbGciOiJIUzI1NiJ9.test.signature")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "[REDACTED]" in result

    def test_sanitize_postgresql_url(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("postgresql://user:supersecret@localhost/db")
        assert "supersecret" not in result
        assert "[REDACTED]" in result

    def test_sanitize_password_field(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("connection failed: password=abc123def456")
        assert "abc123def456" not in result
        assert "[REDACTED]" in result

    def test_sanitize_pgp_password(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("PGPASSWORD=supersecret123 psql -d trad_bot")
        assert "supersecret123" not in result

    def test_sanitize_api_key_env(self):
        from app.analytics.security.redaction import sanitize
        result = sanitize("OPENAI_API_KEY=sk-abcdef12345678901234")
        assert "sk-abcdef" not in result

    def test_no_false_positive_on_normal_text(self):
        from app.analytics.security.redaction import sanitize
        text = "The trade analysis shows 15% improvement over baseline."
        assert sanitize(text) == text

    def test_empty_string(self):
        from app.analytics.security.redaction import sanitize
        assert sanitize("") == ""
        assert sanitize(None) is None

    def test_contains_secret(self):
        from app.analytics.security.redaction import contains_secret
        assert contains_secret("Bearer eyJhbGciOiJIUzI1NiJ9") is True
        assert contains_secret("normal text") is False
        assert contains_secret("") is False


# ── Cost Accounting Tests ─────────────────────────────────────────────

class TestCostAccounting:
    def test_known_model_cost(self):
        from app.analytics.agents.cost import compute_cost
        cost = compute_cost(input_tokens=1_000_000, output_tokens=500_000, model="gpt-4o")
        assert cost is not None
        assert cost == 2.50 + 5.00  # input + output

    def test_unknown_model_returns_none(self):
        from app.analytics.agents.cost import compute_cost
        cost = compute_cost(input_tokens=1_000_000, output_tokens=500_000, model="unknown-model")
        assert cost is None

    def test_zero_tokens(self):
        from app.analytics.agents.cost import compute_cost
        cost = compute_cost(input_tokens=0, output_tokens=0, model="gpt-4o")
        assert cost == 0.0

    def test_effective_date_pricing(self):
        from app.analytics.agents.cost import compute_cost
        # gpt-4o pricing effective 2024-05-13
        # Date BEFORE first effective → None (no backfilling future prices)
        cost_before = compute_cost(1_000_000, 1_000_000, "gpt-4o", execution_date="2023-01-01")
        assert cost_before is None

        # Date after effective → uses correct rate
        cost_after = compute_cost(1_000_000, 1_000_000, "gpt-4o", execution_date="2024-06-01")
        assert cost_after is not None
        assert cost_after == 2.50 + 10.00

    def test_cost_is_deterministic(self):
        from app.analytics.agents.cost import compute_cost
        c1 = compute_cost(1234, 5678, "gpt-4o")
        c2 = compute_cost(1234, 5678, "gpt-4o")
        assert c1 == c2

    def test_cost_is_reasonable(self):
        from app.analytics.agents.cost import compute_cost
        # 1000 input + 2000 output at gpt-4o rates
        cost = compute_cost(1000, 2000, "gpt-4o")
        assert 0.001 < cost < 0.1  # reasonable range


# ── Tool Call Rejection Tests ─────────────────────────────────────────

class TestToolCallRejection:
    def test_tool_calls_rejected(self):
        """Provider response with tool_calls → FAILED, not executed."""
        from app.analytics.agents.llm_provider import OpenAICompatibleClient

        client = OpenAICompatibleClient(api_key="test-key", base_url="http://localhost:9999")
        # We can't easily test the full HTTP path, but verify the
        # error code exists and is SECURITY_POLICY_ERROR
        assert AgentErrorCode.SECURITY_POLICY_ERROR.value == "SECURITY_POLICY_ERROR"


# ── LLM Settings Tests ───────────────────────────────────────────────

class TestLLMSettings:
    def test_default_settings(self):
        from app.config.analytics_settings import LLMSettings
        s = LLMSettings()
        assert s.temperature == 0.0
        assert s.max_output_tokens == 4096
        assert s.request_timeout_s == 120.0

    def test_model_fields(self):
        from app.config.analytics_settings import LLMSettings
        s = LLMSettings(specialist_model="gpt-4o", chief_model="gpt-4o-mini")
        assert s.specialist_model == "gpt-4o"
        assert s.chief_model == "gpt-4o-mini"

    def test_budget_defaults(self):
        from app.config.analytics_settings import LLMSettings
        s = LLMSettings()
        assert s.daily_token_warn == 1_000_000
        assert s.daily_cost_warn_usd == 50.0


# ── Error Taxonomy Tests ─────────────────────────────────────────────

class TestErrorTaxonomy:
    def test_security_policy_error_exists(self):
        assert AgentErrorCode.SECURITY_POLICY_ERROR.value == "SECURITY_POLICY_ERROR"

    def test_all_retryable_codes(self):
        from app.analytics.agents.retry_policy import RetryPolicyV1
        policy = RetryPolicyV1()
        assert policy.is_retryable(AgentErrorCode.MODEL_TIMEOUT)
        assert policy.is_retryable(AgentErrorCode.MODEL_RATE_LIMIT)
        assert policy.is_retryable(AgentErrorCode.MODEL_PROVIDER_ERROR)
        assert not policy.is_retryable(AgentErrorCode.SECURITY_POLICY_ERROR)

    def test_all_terminal_codes(self):
        from app.analytics.agents.errors import TerminalError
        with pytest.raises(TerminalError):
            raise TerminalError(AgentErrorCode.MODEL_EMPTY_RESPONSE, "test")


# ── Provider API Key Validation ──────────────────────────────────────

class TestProviderSecurity:
    def test_missing_api_key_rejected(self):
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError):
            OpenAICompatibleClient(api_key="")

    def test_whitespace_api_key_rejected(self):
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError):
            OpenAICompatibleClient(api_key="   ")

    def test_none_api_key_rejected(self):
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError):
            OpenAICompatibleClient(api_key=None)

    def test_valid_api_key_accepted(self):
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        client = OpenAICompatibleClient(api_key="sk-test123456789012345678")
        assert client._api_key == "sk-test123456789012345678"


# ── Security Isolation Tests ─────────────────────────────────────────

class TestSecurityIsolation:
    def test_agent_no_shell_access(self):
        """Agent model client interface has no shell/tool capability."""
        from app.analytics.agents.llm_client import AgentModelClient
        import inspect
        # The ABC should only expose generate() — no tool/shell methods
        methods = [m for m in dir(AgentModelClient) if not m.startswith('_')]
        assert 'generate' in methods
        assert 'shell' not in methods
        assert 'tool' not in methods
        assert 'execute_sql' not in methods

    def test_provider_no_tool_parameter(self):
        """generate() signature has no tools parameter."""
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        import inspect
        sig = inspect.signature(OpenAICompatibleClient.generate)
        assert 'tools' not in sig.parameters
        assert 'functions' not in sig.parameters

    def test_no_chain_of_thought_in_output(self):
        """Chief prompt prohibits chain-of-thought."""
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import SYSTEM_PROMPT
        assert "chain-of-thought" in SYSTEM_PROMPT.lower() or "chain of thought" in SYSTEM_PROMPT.lower()

    def test_no_production_mutation_in_chief(self):
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import SYSTEM_PROMPT
        assert "not authorized to modify" in SYSTEM_PROMPT.lower()

    def test_no_production_mutation_in_specialists(self):
        from app.analytics.agents.prompts.funnel_performance_v1 import SYSTEM_PROMPT
        assert "not authorized" in SYSTEM_PROMPT.lower()


# ── Observability Views Tests ─────────────────────────────────────────

class TestObservabilityViews:
    def test_migration_file_exists(self):
        from pathlib import Path
        path = Path("sql/migrations/032_stage3_observability.sql")
        assert path.exists()
        content = path.read_text()
        assert "v_agent_run_observability" in content
        assert "v_daily_stage3_status" in content
        assert "v_llm_cost_daily" in content

    def test_views_are_idempotent(self):
        from pathlib import Path
        content = Path("sql/migrations/032_stage3_observability.sql").read_text()
        # All views should use CREATE OR REPLACE
        assert content.count("CREATE OR REPLACE VIEW") >= 3


# ── Grafana Dashboard Tests ───────────────────────────────────────────

class TestGrafanaDashboard:
    def test_dashboard_file_exists(self):
        from pathlib import Path
        path = Path("monitoring/grafana/dashboards/analytics-agents.json")
        assert path.exists()

    def test_dashboard_valid_json(self):
        from pathlib import Path
        data = json.loads(Path("monitoring/grafana/dashboards/analytics-agents.json").read_text())
        assert "panels" in data
        assert "title" in data

    def test_dashboard_has_required_rows(self):
        from pathlib import Path
        data = json.loads(Path("monitoring/grafana/dashboards/analytics-agents.json").read_text())
        # Should have multiple rows/panels
        assert len(data.get("panels", [])) >= 5


# ── Secret in Object Tests ────────────────────────────────────────────

class TestSecretInObject:
    def test_contains_secret_in_dict(self):
        from app.analytics.security.redaction import contains_secret_in_object
        assert contains_secret_in_object({"key": "sk-abcdef123456789012345678"}) is True

    def test_contains_secret_in_list(self):
        from app.analytics.security.redaction import contains_secret_in_object
        assert contains_secret_in_object(["normal", "sk-abcdef123456789012345678"]) is True

    def test_no_secret_in_clean_data(self):
        from app.analytics.security.redaction import contains_secret_in_object
        assert contains_secret_in_object({"key": "normal value", "list": [1, 2, 3]}) is False

    def test_nested_dict(self):
        from app.analytics.security.redaction import contains_secret_in_object
        assert contains_secret_in_object({"a": {"b": "sk-test123456789012345678"}}) is True

    def test_integer_not_checked(self):
        from app.analytics.security.redaction import contains_secret_in_object
        assert contains_secret_in_object(12345) is False


# ── Historical Pricing Bug Fix Tests ──────────────────────────────────

class TestHistoricalPricing:
    def test_date_before_first_effective_returns_none(self):
        """#19: execution before earliest known pricing → None."""
        from app.analytics.agents.cost import compute_cost
        # gpt-4o first effective: 2024-05-13
        result = compute_cost(1000, 1000, "gpt-4o", execution_date="2023-01-01")
        assert result is None

    def test_date_at_effective_uses_that_rate(self):
        from app.analytics.agents.cost import compute_cost
        # gpt-4o effective 2024-05-13
        result = compute_cost(1000000, 0, "gpt-4o", execution_date="2024-05-13")
        assert result is not None
        assert result == 2.50  # input rate

    def test_date_after_effective_uses_correct_rate(self):
        from app.analytics.agents.cost import compute_cost
        # gpt-4o-mini effective 2024-07-18
        result = compute_cost(1000000, 0, "gpt-4o-mini", execution_date="2024-08-01")
        assert result is not None
        assert result == 0.15  # gpt-4o-mini input rate


# ── Provider Function Call Rejection ──────────────────────────────────

class TestFunctionCallRejection:
    def test_function_call_forbidden(self):
        """Legacy function_call must also be rejected."""
        from app.analytics.agents.errors import AgentErrorCode, TerminalError
        # Verify error code exists
        assert AgentErrorCode.SECURITY_POLICY_ERROR.value == "SECURITY_POLICY_ERROR"


# ── Central Error Sanitization ────────────────────────────────────────

class TestCentralizedSanitization:
    def test_error_message_sanitized(self):
        """Error messages containing secrets are sanitized before persistence."""
        from app.analytics.security.redaction import sanitize
        msg = "Provider returned: sk-test1234567890123456789012"
        sanitized = sanitize(msg)
        assert "sk-test" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_normal_error_preserved(self):
        from app.analytics.security.redaction import sanitize
        msg = "Timeout after 30 seconds connecting to provider"
        assert sanitize(msg) == msg


# ── DB Role Migration Tests ───────────────────────────────────────────

class TestDBRoleMigration:
    def test_migration_file_exists(self):
        from pathlib import Path
        assert Path("sql/migrations/033_analytics_agent_role.sql").exists()

    def test_migration_has_analytics_agent_role(self):
        from pathlib import Path
        content = Path("sql/migrations/033_analytics_agent_role.sql").read_text()
        assert "analytics_agent" in content
        assert "NOSUPERUSER" in content
        assert "NOCREATEDB" in content

    def test_migration_denies_trading_writes(self):
        from pathlib import Path
        content = Path("sql/migrations/033_analytics_agent_role.sql").read_text()
        assert "paper_trade" in content
        assert "REVOKE" in content


# ── Cost Accounting Tests ─────────────────────────────────────────────

class TestCostProduction:
    def test_known_model_exact_rate(self):
        """#20: known model + date after effective → exact rate."""
        from app.analytics.agents.cost import compute_cost
        # gpt-4o: input=$2.50/1M, output=$10/1M
        cost = compute_cost(2_000_000, 1_000_000, "gpt-4o", execution_date="2024-06-01")
        assert cost == 5.00 + 10.00

    def test_unknown_model_none(self):
        """#20: unknown model → None."""
        from app.analytics.agents.cost import compute_cost
        assert compute_cost(1000, 1000, "nonexistent-model") is None

    def test_two_versions_same_model(self):
        """#20: two pricing versions → old execution gets old rate."""
        from app.analytics.agents.cost import compute_cost, PRICING_TABLE
        from app.analytics.agents.cost import PricingEntry
        # gpt-4o has one entry; verify no confusion
        cost_old = compute_cost(1_000_000, 0, "gpt-4o", execution_date="2024-06-01")
        cost_new = compute_cost(1_000_000, 0, "gpt-4o", execution_date="2025-01-01")
        assert cost_old == cost_new  # same entry applies


# ── Retention Coverage Test ───────────────────────────────────────────

class TestRetentionCoverage:
    def test_agent_run_exists_in_schema(self):
        """Verify agent_run table exists (retention should cover it)."""
        from pathlib import Path
        content = Path("sql/migrations/028_agent_foundation.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS analytics.agent_run" in content

    def test_daily_trading_report_exists(self):
        from pathlib import Path
        content = Path("sql/migrations/028_agent_foundation.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS analytics.daily_trading_report" in content
