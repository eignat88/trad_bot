"""BYBIT_API_TELEMETRY_V1 first-slice tests — Issue #228.

Coverage requested by the first PR slice:

* exactly one structured log line per HTTP attempt;
* logical-request <-> retry binding (``logical_request_id`` + strictly
  ordered ``attempt_index`` for timeout and rate-limit retries);
* ``X-Bapi-Limit*`` header capture in both modes (values when present,
  explicit ``headers_absent`` marker when stripped) and never-guessed
  ``status_code`` plus its ``status_code_source`` provenance marker
  (``measured`` / ``unknown_response_status`` / ``no_response``);
* transport / HTTP / API error classification, including previously
  unhandled transport exceptions (``ConnectionResetError`` & friends) with
  exactly one record per attempt and identical exception identity;
* secret hygiene (no keys, secrets, signatures, auth headers or private
  payload in any record or log line);
* ``create_order`` POST instrumentation;
* service tagging regression (no known consumer maps to ``other``);
* BYBIT_API_CALL legacy prefix compatibility;
* log-injection hardening (control-character and key=value escaping of
  free-form fields, incl. multiline provider messages and env values);
* fail-open telemetry and unchanged HTTP request counts.
"""
from __future__ import annotations

import logging
import re
from urllib.error import URLError

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import Settings
from app.exchange.api_telemetry import (
    SERVICE_ENV,
    ApiCallRecord,
    ApiTelemetry,
    detect_service_tag,
    extract_response_meta,
    resolve_service_tag,
)
from app.exchange.bybit_client import BybitClient, BybitError, _RateLimiter

TELEMETRY_MARKER = "BYBIT_API_CALL"


# ----------------------------------------------------------------------
# Fakes (session/response doubles, no network)
# ----------------------------------------------------------------------


class FakeResponse:
    """Response double with optional status/headers, like a real HTTP reply."""

    def __init__(self, payload=None, status=200, headers=None, http_error=False):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "", "result": {"list": []},
        }
        self.status = status
        self.headers = headers
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error or not 200 <= self.status < 300:
            raise BybitError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class MinimalResponse:
    """Legacy-style double: only raise_for_status()/json(), no status/headers."""

    def raise_for_status(self):
        pass

    def json(self):
        return {"retCode": 0, "retMsg": "", "result": {"list": []}}


class BadJsonResponse:
    status = 200
    headers = None

    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class ScriptedSession:
    """Session double that replays scripted outcomes (responses/exceptions)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, dict(headers or {})))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_client(session, **overrides):
    settings = Settings(
        bybit_max_attempts=3,
        bybit_retry_backoff=0,
        bybit_rate_limit_max_retries=2,
        bybit_rate_limit_retry_backoff=0,
        **overrides,
    )
    return BybitClient(settings, session)


def live_settings(**overrides):
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def telemetry_env(monkeypatch, caplog):
    """Fresh telemetry per test, clean env, fast limiter, INFO capture."""
    monkeypatch.setattr(bybit_client_module, "_telemetry", ApiTelemetry())
    monkeypatch.delenv(SERVICE_ENV, raising=False)
    monkeypatch.setattr(BybitClient, "_global_limiter", _RateLimiter(rps=10_000))
    caplog.set_level(logging.INFO)
    yield


def attempt_lines(caplog):
    """Exactly the BYBIT_API_CALL attempt lines, in emission order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(TELEMETRY_MARKER)
    ]


def field(line, name):
    match = re.search(rf"(?:^| ){re.escape(name)}=(\S+)", line)
    assert match, f"{name!r} not found in line: {line}"
    return match.group(1)


# ----------------------------------------------------------------------
# One record per HTTP attempt + logical request <-> retries binding
# ----------------------------------------------------------------------


def test_one_log_line_per_http_attempt_success(caplog):
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    assert len(session.calls) == 1
    line = lines[0]
    assert field(line, "endpoint") == "/v5/market/kline"
    assert field(line, "result") == "OK"
    assert field(line, "attempt_index") == "1"
    # budget: 3 outer attempts * (1 + 2 rate-limit retries)
    assert field(line, "total_attempts") == "9"
    assert field(line, "retry_reason") == "none"
    assert field(line, "status_code") == "200"
    assert field(line, "method") == "GET"
    assert len(field(line, "logical_request_id")) == 16


def test_timeout_retries_share_logical_request_id(caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    session = ScriptedSession([TimeoutError("slow"), TimeoutError("slow"), FakeResponse()])
    assert make_client(session).get_klines("BTCUSDT") == []
    lines = attempt_lines(caplog)
    assert len(lines) == 3
    assert len(session.calls) == 3  # HTTP request count unchanged by telemetry
    assert len({field(line, "logical_request_id") for line in lines}) == 1
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2, 3]
    assert [field(line, "retry_reason") for line in lines] == ["none", "timeout", "timeout"]
    assert [field(line, "result") for line in lines] == ["TIMEOUT", "TIMEOUT", "OK"]


def test_rate_limit_retries_share_logical_request_id(caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    rate_limited = FakeResponse({"retCode": 10006, "retMsg": "Too many visits", "result": {}})
    session = ScriptedSession([rate_limited, FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 2
    assert len(session.calls) == 2
    assert len({field(line, "logical_request_id") for line in lines}) == 1
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2]
    assert [field(line, "retry_reason") for line in lines] == ["none", "rate_limit"]
    assert [field(line, "result") for line in lines] == ["RATE_LIMIT", "OK"]


def test_reconciliation_n_logical_requests_k_attempts(caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    outcomes = []
    for _ in range(3):
        outcomes += [TimeoutError("slow"), FakeResponse()]
    session = ScriptedSession(outcomes)
    client = make_client(session)
    for _ in range(3):
        client.get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 6                      # N logical * k attempts rows
    groups = {}
    for line in lines:
        groups.setdefault(field(line, "logical_request_id"), []).append(line)
    assert len(groups) == 3                     # N logical groups
    for group in groups.values():
        assert [int(field(line, "attempt_index")) for line in group] == [1, 2]


def test_mixed_failures_one_record_per_attempt(caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    session = ScriptedSession([
        TimeoutError("slow"),
        FakeResponse({"retCode": 10001, "retMsg": "params error"}),
    ])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 2
    assert len({field(line, "logical_request_id") for line in lines}) == 1
    assert [field(line, "result") for line in lines] == ["TIMEOUT", "ERROR"]
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2]


# ----------------------------------------------------------------------
# X-Bapi-Limit* header capture + status_code population
# ----------------------------------------------------------------------


def test_x_bapi_headers_captured_when_present(caplog):
    headers = {
        "X-Bapi-Limit": "600",
        "X-Bapi-Limit-Status": "598",
        "X-Bapi-Limit-Reset-Timestamp": "1699999999999",
    }
    session = ScriptedSession([FakeResponse(headers=headers)])
    make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "bapi_limit") == "600"
    assert field(line, "bapi_limit_status") == "598"
    assert field(line, "bapi_limit_reset_ms") == "1699999999999"
    assert field(line, "headers_absent") == "false"


def test_headers_absent_marker_when_stripped(caplog):
    session = ScriptedSession([FakeResponse(headers={})])
    make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "headers_absent") == "true"
    assert field(line, "bapi_limit") == "none"
    assert field(line, "bapi_limit_status") == "none"
    assert field(line, "bapi_limit_reset_ms") == "none"
    assert field(line, "status_code") == "200"


def test_headers_absent_marker_without_headers_attribute(caplog):
    session = ScriptedSession([MinimalResponse()])
    make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "headers_absent") == "true"


def test_header_lookup_is_case_insensitive_and_partial():
    meta = extract_response_meta(
        FakeResponse(headers={"x-bapi-limit": "5", "X-BAPI-LIMIT-STATUS": "4"})
    )
    assert meta.bapi_limit == 5
    assert meta.bapi_limit_status == 4
    assert meta.bapi_limit_reset_ms is None
    assert meta.headers_absent is False


def test_header_lookup_supports_pair_sequences():
    class PairHeaders:
        def __iter__(self):
            return iter([("X-Bapi-Limit", "7")])

    meta = extract_response_meta(FakeResponse(headers=PairHeaders()))
    assert meta.bapi_limit == 7
    assert meta.headers_absent is False


def test_extract_response_meta_is_fail_open():
    class HostileResponse:
        @property
        def status(self):
            raise RuntimeError("boom")

        @property
        def headers(self):
            raise RuntimeError("boom")

    meta = extract_response_meta(HostileResponse())
    assert meta.status_code is None
    assert meta.headers_absent is True


def test_status_code_never_guessed_and_source_always_present(caplog):
    # explicit HTTP status -> measured
    session = ScriptedSession([FakeResponse(status=200)])
    make_client(session).get_klines("BTCUSDT")
    assert field(attempt_lines(caplog)[0], "status_code") == "200"
    assert field(attempt_lines(caplog)[0], "status_code_source") == "measured"

    # response without status attribute -> 0 sentinel + explicit marker,
    # NEVER a guessed HTTP 200 (review PR #232 / Issue #228)
    caplog.clear()
    session = ScriptedSession([MinimalResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert field(attempt_lines(caplog)[0], "status_code") == "0"
    assert field(attempt_lines(caplog)[0], "status_code_source") == "unknown_response_status"

    # no HTTP response at all -> explicit 0 (transport failure)
    caplog.clear()
    session = ScriptedSession([URLError(ConnectionRefusedError(111, "refused"))])
    with pytest.raises(URLError):
        make_client(session).get_klines("BTCUSDT")
    assert field(attempt_lines(caplog)[0], "status_code") == "0"
    assert field(attempt_lines(caplog)[0], "status_code_source") == "no_response"


def test_api_error_status_is_measured_not_assumed(caplog):
    """retCode != 0 with a real HTTP 200 keeps the measured status."""
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": "params error", "result": {}}),
    ])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "status_code") == "200"
    assert field(line, "status_code_source") == "measured"


def test_api_error_with_unknown_status_is_marked(caplog):
    """retCode != 0 from a status-less transport must not claim HTTP 200."""

    class NoStatusResponse(MinimalResponse):
        def json(self):
            return {"retCode": 10001, "retMsg": "params error", "result": {}}

    session = ScriptedSession([NoStatusResponse()])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "status_code") == "0"
    assert field(line, "status_code_source") == "unknown_response_status"
    assert field(line, "ret_code") == "10001"


def test_create_order_unknown_status_is_marked(caplog):
    class NoStatusOrderResponse(MinimalResponse):
        def json(self):
            return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "OID"}}

    session = ScriptedSession([NoStatusOrderResponse()])
    BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    line = attempt_lines(caplog)[0]
    assert field(line, "status_code") == "0"
    assert field(line, "status_code_source") == "unknown_response_status"


# ----------------------------------------------------------------------
# Transport / HTTP / API error classification
# ----------------------------------------------------------------------


def test_http_error_recorded_with_status_and_fails_fast(caplog):
    session = ScriptedSession([FakeResponse(status=500, http_error=True)])
    with pytest.raises(BybitError, match="HTTP 500"):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1  # no retry, behavior preserved
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "500"
    assert field(line, "ret_code") == "none"
    assert field(line, "retry_reason") == "none"


def test_api_error_recorded_with_ret_code(caplog):
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": "params error", "result": {}}),
    ])
    with pytest.raises(BybitError, match="params error"):
        make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "ret_code") == "10001"
    assert field(line, "status_code") == "200"


def test_transport_error_recorded_and_reraised_unchanged(caplog):
    error = URLError(ConnectionRefusedError(111, "Connection refused"))
    session = ScriptedSession([error])
    with pytest.raises(URLError) as exc_info:
        make_client(session).get_klines("BTCUSDT")
    assert exc_info.value is error  # exception identity preserved
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "0"
    assert field(line, "ret_code") == "none"


def test_malformed_json_recorded_and_reraised(caplog):
    session = ScriptedSession([BadJsonResponse()])
    with pytest.raises(ValueError):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "200"


# ----------------------------------------------------------------------
# create_order POST instrumentation
# ----------------------------------------------------------------------


def test_create_order_emits_one_attempt_record(caplog):
    session = ScriptedSession([
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "OID"}}),
    ])
    payload = BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    assert payload["result"]["orderId"] == "OID"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert field(line, "endpoint") == "/v5/order/create"
    assert field(line, "method") == "POST"
    assert field(line, "result") == "OK"
    assert field(line, "attempt_index") == "1"
    assert field(line, "total_attempts") == "1"
    assert field(line, "ret_code") == "0"
    assert field(line, "status_code") == "200"
    assert len(field(line, "logical_request_id")) == 16
    assert "symbol=BTCUSDT" in line


def test_create_order_api_error_instrumented(caplog):
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": "order rejected", "result": {}}),
    ])
    with pytest.raises(BybitError, match="order rejected"):
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "ret_code") == "10001"
    assert field(line, "status_code") == "200"


def test_create_order_http_error_instrumented(caplog):
    session = ScriptedSession([FakeResponse(status=403, http_error=True)])
    with pytest.raises(BybitError, match="HTTP 403"):
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "403"


def test_create_order_transport_error_instrumented(caplog):
    session = ScriptedSession([URLError(ConnectionRefusedError(111, "refused"))])
    with pytest.raises(URLError):
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    line = attempt_lines(caplog)[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "0"
    assert field(line, "method") == "POST"


def test_create_order_guard_emits_no_record(caplog):
    session = ScriptedSession([])
    with pytest.raises(RuntimeError, match="explicit switch"):
        BybitClient(Settings(trading_mode="paper"), session).create_order("BTCUSDT", "Buy", 1)
    assert session.calls == []          # no HTTP request at all (behavior preserved)
    assert attempt_lines(caplog) == []  # and no telemetry record


# ----------------------------------------------------------------------
# Secret hygiene
# ----------------------------------------------------------------------


def test_secret_hygiene_no_credentials_or_payload_in_logs(caplog):
    session = ScriptedSession([
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "OID"}}),
    ])
    client = BybitClient(live_settings(), session)
    client.create_order("BTCUSDT", "Buy", 0.123456789)

    # sanity: the real request DID carry credentials (so the test is meaningful)
    sent_headers = session.calls[0][3]
    assert sent_headers["X-BAPI-API-KEY"] == "APIKEY_SENTINEL_1234567890"
    assert sent_headers["X-BAPI-SIGN"]

    text = caplog.text
    assert "APIKEY_SENTINEL_1234567890" not in text
    assert "APISECRET_SENTINEL_0987654321" not in text
    assert "X-BAPI" not in text            # no auth header names/values
    assert "orderType" not in text         # no signed private payload fields
    assert "0.123456789" not in text
    assert "side" not in text or "side=Buy" not in text


def test_secret_hygiene_response_auth_headers_never_recorded(caplog):
    headers = {
        "X-Bapi-Limit": "600",
        "Authorization": "Bearer SUPERSECRET_TOKEN_XYZ",
        "X-BAPI-SIGN": "DEADBEEF_SIGNATURE",
        "Set-Cookie": "session=SECRET_COOKIE",
    }
    session = ScriptedSession([FakeResponse(headers=headers)])
    make_client(session).get_klines("BTCUSDT")
    text = caplog.text
    assert "SUPERSECRET_TOKEN_XYZ" not in text
    assert "DEADBEEF_SIGNATURE" not in text
    assert "SECRET_COOKIE" not in text
    assert "Authorization" not in text
    line = attempt_lines(caplog)[0]
    assert field(line, "bapi_limit") == "600"  # allowlisted header still captured


def test_record_schema_has_no_secret_capable_fields():
    names = [name.lower() for name in ApiCallRecord.__dataclass_fields__]
    for name in names:
        for forbidden in ("key", "secret", "sign", "token", "body", "payload",
                          "param", "authorization", "cookie"):
            assert forbidden not in name, f"field {name} may leak {forbidden}"
    header_fields = [name for name in names if "header" in name]
    assert header_fields == ["headers_absent"]


# ----------------------------------------------------------------------
# Service tagging
# ----------------------------------------------------------------------


def test_service_tag_env_override_wins(caplog, monkeypatch):
    monkeypatch.setenv(SERVICE_ENV, "me_r_v3_release")
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert field(attempt_lines(caplog)[0], "service") == "me_r_v3_release"


def test_service_tag_known_consumers_never_map_to_other():
    known_consumers = [
        "app/main.py",
        "app/analytics/candle_sync.py",
        "app/analytics/cli.py",
        "app/research/evaluator.py",
        "app/research/evaluator_runner.py",
        "app/research/prospective_evaluator.py",
        "app/research/me_r_long_early_mae_v3/isolated_oos.py",
        "app/research/me_short_a_forward_outcome_runtime_runner_v1.py",
        "app/research/htf_baseline_short_phase2_observer_runner_v1.py",
        "app/execution/live_exchange.py",
        "app/market_data/market_data_service.py",
        "app/scanners/cli.py",
        "app/scanners/context_builder.py",
        "app/scanners/orchestrator.py",
        "app/scanners/outcome_cli.py",
        "app/shadow/v2d_runner.py",
        "app/shadow/v2d_evaluator.py",
        "app/shadow/srr_research_evaluator.py",
        "app/shadow/me_r_long_close_location_oos_evaluator.py",
        "app/shadow/runner.py",
        "app/shadow/backfill.py",
        "app/shadow/evaluator.py",
        "app/paper/cli.py",
        "paper_runner.py",
        "scanner_runner.py",
        "tools/research/me_r_v3_universe_candidate_issue154.py",
        "tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py",
        "tools/research/htf_keylevel_baseline_short_causal_replay_v1.py",
    ]
    for consumer in known_consumers:
        assert detect_service_tag(consumer) != "other", consumer
        assert resolve_service_tag(consumer) != "other", consumer


def test_service_tag_legacy_labels_are_stable():
    legacy = {
        "app/scanners/context_builder.py": "scanner",
        "app/scanners/orchestrator.py": "scanner",
        "scanner_runner.py": "scanner",
        "paper_runner.py": "paper",
        "app/paper/cli.py": "paper",
        "app/shadow/runner.py": "shadow",
        "app/shadow/backfill.py": "shadow",
        "app/shadow/evaluator.py": "shadow",
        "app/analytics/candle_sync.py": "analytics",
        "app/scanners/outcome_cli.py": "outcome",
        "app/market_data/market_data_service.py": "market_data",
        "app/research/me_short_a_forward_outcome_runtime_runner_v1.py": "outcome",
    }
    for caller, tag in legacy.items():
        assert detect_service_tag(caller) == tag, caller


def test_service_tag_me_r_v3_and_srr_are_named():
    assert detect_service_tag(
        "app/research/me_r_long_early_mae_v3/isolated_oos.py") == "me_r_v3"
    assert detect_service_tag(
        "app/shadow/srr_research_evaluator.py") == "srr"
    assert detect_service_tag(
        "app/shadow/me_r_long_close_location_oos_evaluator.py") == "me_r_close_location"


def test_service_tag_explicit_env_used_in_record(caplog, monkeypatch):
    monkeypatch.setenv(SERVICE_ENV, "srr_evaluator_unit")
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert field(attempt_lines(caplog)[0], "service") == "srr_evaluator_unit"


# ----------------------------------------------------------------------
# BYBIT_API_CALL backward compatibility
# ----------------------------------------------------------------------


def test_bybit_api_call_ok_prefix_is_byte_compatible(caplog):
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT", interval="5", limit=200)
    line = attempt_lines(caplog)[0]
    prefix = line.split(" telemetry_v=1 ")[0]
    assert re.fullmatch(
        r"BYBIT_API_CALL service=\S+ endpoint=/v5/market/kline caller=\S+ "
        r"symbol=BTCUSDT interval=5 limit=200 duration_ms=\d+ result=OK",
        prefix,
    )
    assert " telemetry_v=1 logical_request_id=" in line


def test_bybit_api_call_error_prefix_is_byte_compatible(caplog):
    session = ScriptedSession([FakeResponse(status=500, http_error=True)])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    prefix = line.split(" telemetry_v=1 ")[0]
    assert re.fullmatch(
        r"BYBIT_API_CALL service=\S+ endpoint=\S+ caller=\S+ "
        r"symbol=BTCUSDT interval=\S+ limit=\d+ duration_ms=\d+ "
        r"result=ERROR retMsg=HTTP 500 status=500",
        prefix,
    )


def test_bybit_api_call_rate_limit_prefix_is_byte_compatible(caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    rate_limited = FakeResponse({"retCode": 10006, "retMsg": "Too many visits", "result": {}})
    session = ScriptedSession([rate_limited, FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    line = attempt_lines(caplog)[0]
    prefix = line.split(" telemetry_v=1 ")[0]
    assert re.fullmatch(
        r"BYBIT_API_CALL service=\S+ endpoint=\S+ caller=\S+ "
        r"symbol=BTCUSDT interval=\S+ limit=\d+ duration_ms=\d+ "
        r"result=RATE_LIMIT retMsg=Too many visits",
        prefix,
    )


# ----------------------------------------------------------------------
# Fail-open telemetry + HTTP request count parity
# ----------------------------------------------------------------------


def test_telemetry_failure_never_fails_fetch(monkeypatch):
    class BrokenTelemetry:
        def record_http_attempt(self, **kwargs):
            raise RuntimeError("telemetry bug")

    monkeypatch.setattr(bybit_client_module, "_telemetry", BrokenTelemetry())
    session = ScriptedSession([FakeResponse()])
    assert make_client(session).get_klines("BTCUSDT") == []
    assert len(session.calls) == 1


def test_http_request_count_unchanged_by_telemetry(monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)

    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1

    session = ScriptedSession([TimeoutError(), TimeoutError(), FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 3

    rate_limited = FakeResponse({"retCode": 10006, "retMsg": "Too many visits", "result": {}})
    session = ScriptedSession([rate_limited, FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 2


# ----------------------------------------------------------------------
# Review PR #232 fixes: unhandled transport exceptions (P1), never-guessed
# status (P2 above) and log-injection hardening (P2)
# ----------------------------------------------------------------------


class ExplodingSession:
    """Session double that raises an unhandled transport exception on send."""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        raise self.error

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        raise self.error


UNHANDLED_TRANSPORT_ERRORS = [
    ConnectionResetError(10054, "Connection reset by peer"),
    ConnectionAbortedError(10053, "Connection aborted"),
    OSError("simulated transport failure"),
]


@pytest.mark.parametrize("error", UNHANDLED_TRANSPORT_ERRORS,
                         ids=lambda e: type(e).__name__)
def test_get_unhandled_transport_exception_records_exactly_one_attempt(caplog, error):
    session = ExplodingSession(error)
    with pytest.raises(type(error)) as exc_info:
        make_client(session).get_klines("BTCUSDT")
    assert exc_info.value is error          # identical exception preserved
    assert len(session.calls) == 1          # request count unchanged, no retry
    lines = attempt_lines(caplog)
    assert len(lines) == 1                  # exactly one record per attempt
    line = lines[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "method") == "GET"
    assert field(line, "status_code") == "0"
    assert field(line, "status_code_source") == "no_response"
    assert field(line, "retry_reason") == "none"
    assert field(line, "attempt_index") == "1"
    assert type(error).__name__ in line


@pytest.mark.parametrize("error", UNHANDLED_TRANSPORT_ERRORS,
                         ids=lambda e: type(e).__name__)
def test_post_unhandled_transport_exception_records_exactly_one_attempt(caplog, error):
    session = ExplodingSession(error)
    with pytest.raises(type(error)) as exc_info:
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    assert exc_info.value is error          # identical exception preserved
    assert len(session.calls) == 1          # request count unchanged, no retry
    lines = attempt_lines(caplog)
    assert len(lines) == 1                  # exactly one record per attempt
    line = lines[0]
    assert field(line, "method") == "POST"
    assert field(line, "endpoint") == "/v5/order/create"
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "0"
    assert field(line, "status_code_source") == "no_response"
    assert type(error).__name__ in line


def test_unhandled_transport_exception_in_rate_limit_retry_records_exactly_once(
        caplog, monkeypatch):
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda *_: None)
    rate_limited = FakeResponse({"retCode": 10006, "retMsg": "Too many visits", "result": {}})
    error = ConnectionResetError(10054, "Connection reset by peer")
    session = ScriptedSession([rate_limited, error])
    with pytest.raises(ConnectionResetError) as exc_info:
        make_client(session).get_klines("BTCUSDT")
    assert exc_info.value is error
    assert len(session.calls) == 2          # original send + one rate-limit retry
    lines = attempt_lines(caplog)
    assert len(lines) == 2                  # no double-record on either attempt
    assert [field(line, "result") for line in lines] == ["RATE_LIMIT", "ERROR"]
    assert [field(line, "retry_reason") for line in lines] == ["none", "rate_limit"]
    assert [int(field(line, "attempt_index")) for line in lines] == [1, 2]


def test_unexpected_payload_exception_records_exactly_one_attempt(caplog):
    class ExplodingJsonResponse:
        status = 200
        headers = None

        def raise_for_status(self):
            pass

        def json(self):
            raise RuntimeError("unexpected decoder failure")

    session = ScriptedSession([ExplodingJsonResponse()])
    with pytest.raises(RuntimeError, match="unexpected decoder failure"):
        make_client(session).get_klines("BTCUSDT")
    assert len(session.calls) == 1
    lines = attempt_lines(caplog)
    assert len(lines) == 1                  # exactly one record, no double
    line = lines[0]
    assert field(line, "result") == "ERROR"
    assert field(line, "status_code") == "200"
    assert field(line, "status_code_source") == "measured"


def test_create_order_unexpected_payload_exception_records_exactly_one_attempt(caplog):
    class ExplodingJsonResponse:
        status = 200
        headers = None

        def raise_for_status(self):
            pass

        def json(self):
            raise RuntimeError("unexpected decoder failure")

    session = ScriptedSession([ExplodingJsonResponse()])
    with pytest.raises(RuntimeError, match="unexpected decoder failure"):
        BybitClient(live_settings(), session).create_order("BTCUSDT", "Buy", 1)
    assert len(session.calls) == 1
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    assert field(lines[0], "method") == "POST"


def test_malformed_json_records_exactly_one_attempt(caplog):
    session = ScriptedSession([BadJsonResponse()])
    with pytest.raises(ValueError):
        make_client(session).get_klines("BTCUSDT")
    assert len(attempt_lines(caplog)) == 1  # widened handler must not double-record


# ----------------------------------------------------------------------
# Log-injection hardening (control characters / key=value forgery)
# ----------------------------------------------------------------------


def test_ret_msg_multiline_injection_cannot_forge_records(caplog):
    evil_msg = (
        "bad response\nBYBIT_API_CALL service=evil endpoint=/fake result=FORGED\r\n"
        "X-Injected=1"
    )
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": evil_msg, "result": {}}),
    ])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line and "\r" not in line and "\t" not in line
    assert "\\n" in line and "\\r" in line    # control chars visibly escaped
    assert "result\\x3dFORGED" in line       # key=value forgery neutralized
    assert field(line, "result") == "ERROR"  # real fields still parse


def test_service_env_value_injection_is_escaped(caplog, monkeypatch):
    monkeypatch.setenv(SERVICE_ENV, "evil\nservice=FORGED result=FORGED")
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTCUSDT")
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line
    service = field(line, "service")
    assert service.startswith("evil\\n")
    assert "service\\x3dFORGED" in service
    assert "\\x20" in service                # spaces escaped in keyed fields
    assert field(line, "result") == "OK"     # real fields unaffected


def test_control_chars_in_keyed_fields_are_escaped(caplog):
    session = ScriptedSession([FakeResponse()])
    make_client(session).get_klines("BTC\nUSDT", interval="5\x00")
    line = attempt_lines(caplog)[0]
    assert field(line, "symbol") == "BTC\\nUSDT"
    assert field(line, "interval") == "5\\x00"
    assert "\n" not in line and "\x00" not in line


def test_sanitize_escapes_control_chars_and_key_value_tokens():
    from app.exchange.api_telemetry import _sanitize
    assert _sanitize("plain value") == "plain value"
    assert _sanitize("a\nb\rc\td") == "a\\nb\\rc\\td"
    assert _sanitize("k=v") == "k\\x3dv"
    assert _sanitize("back\\slash") == "back\\\\slash"
    assert _sanitize("\x00\x1b\x7f") == "\\x00\\x1b\\x7f"
    assert _sanitize("a b") == "a b"                        # free text keeps spaces
    assert _sanitize("a b", escape_spaces=True) == "a\\x20b"


def test_secret_hygiene_with_injection_attempts(caplog):
    """Injection hardening must not weaken the secret-hygiene guarantee."""
    headers = {
        "X-Bapi-Limit": "600",
        "Authorization": "Bearer SUPERSECRET_TOKEN_XYZ",
        "X-BAPI-SIGN": "DEADBEEF_SIGNATURE",
    }
    evil_msg = "boom\nX-BAPI-SIGN=FORGED_SIGNATURE"
    session = ScriptedSession([
        FakeResponse({"retCode": 10001, "retMsg": evil_msg, "result": {}},
                     headers=headers),
    ])
    with pytest.raises(BybitError):
        make_client(session).get_klines("BTCUSDT")
    text = caplog.text
    assert "SUPERSECRET_TOKEN_XYZ" not in text
    assert "DEADBEEF_SIGNATURE" not in text
    assert "Authorization" not in text
    lines = attempt_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line
    assert "X-BAPI-SIGN\\x3dFORGED_SIGNATURE" in line  # forged key=value defused
    assert field(line, "bapi_limit") == "600"          # allowlist header still captured
