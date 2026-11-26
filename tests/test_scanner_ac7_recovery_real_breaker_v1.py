"""Real shared breaker -> BybitClient -> Scanner report -> Recovery.

Offline only: private tmp_path limiter, fake HTTP session, no production DB.
"""

import pytest

import app.exchange.bybit_client as bybit_module
from app.exchange.bybit_client import BybitRateLimitError
from app.exchange import retry_budget
from app.exchange.global_rate_limiter import GrantResult

from app.scanners.cycle_completion_v1 import (
    CycleCompletionReport,
    classify_symbol_failure,
    REQUEST_STATE_PRE_SEND_SKIP,
)

from test_global_bybit_rate_limiter_integration_v1 import (
    ScriptedSession,
    _isolated_limiter,
    _enabled_client,
)
from test_scanner_ac7_recovery_real_report_v1 import execute


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    retry_budget.reset_process_state()

    def blocked_network(*args, **kwargs):
        pytest.fail("REAL_NETWORK_FORBIDDEN")

    monkeypatch.setattr(bybit_module, "urlopen", blocked_network)

    yield

    retry_budget.reset_process_state()


def build_open_breaker(tmp_path):
    limiter = _isolated_limiter(
        tmp_path,
        breaker_cooldowns_s=(120.0, 120.0, 120.0),
        max_wait_s=5.0,
    )

    first = limiter.acquire("P0")
    assert first.granted

    assert limiter.prepare_send(first.grant_id)

    result = limiter.record_result(
        first.grant_id,
        "P0",
        "RATE_LIMIT",
        epoch=first.epoch,
    )
    assert result == "APPLIED"

    snap = limiter.snapshot()

    assert snap["state"] == "OPEN"
    assert snap["level"] == 1

    return limiter


def test_real_breaker_blocks_50_symbols_and_triggers_recovery(tmp_path):
    limiter = build_open_breaker(tmp_path)

    session = ScriptedSession()
    client, _ = _enabled_client(
        session,
        tmp_path,
        limiter=limiter,
    )

    report = CycleCompletionReport(expected_symbols=50)

    for i in range(50):
        symbol = f"S{i}USDT"

        with pytest.raises(BybitRateLimitError) as caught:
            client.get_klines(symbol)

        exc = caught.value

        assert exc.limiter_result in (
            "BREAKER_OPEN",
            "SKIPPED",
        )
        assert exc.telemetry_result == "BYBIT_API_SKIP"

        outcome = classify_symbol_failure(symbol, exc)

        assert outcome.request_state == REQUEST_STATE_PRE_SEND_SKIP
        assert outcome.category == "SKIPPED"

        report.record_outcome(outcome)

    assert session.calls == []
    assert report.processed_count == 0
    assert len(report.limiter_pre_send_skip_symbols) == 50

    assert report.completion_status == "FAILED"
    assert report.run_status == "PARTIAL"

    before = limiter.snapshot()

    delay, attempted, events = execute(
        report,
        enabled=True,
        attempted=False,
    )

    after = limiter.snapshot()

    assert delay == 130.0
    assert attempted is True
    assert not any(level == "exception" for level, _ in events)

    assert before["state"] == after["state"] == "OPEN"
    assert before["level"] == after["level"] == 1

    assert session.calls == []


def test_restart_guard_blocks_real_breaker_recovery(tmp_path):
    limiter = build_open_breaker(tmp_path)

    session = ScriptedSession()
    client, _ = _enabled_client(
        session,
        tmp_path,
        limiter=limiter,
    )

    report = CycleCompletionReport(expected_symbols=1)

    with pytest.raises(BybitRateLimitError) as caught:
        client.get_klines("BTCUSDT")

    report.record_outcome(
        classify_symbol_failure("BTCUSDT", caught.value)
    )

    assert len(report.limiter_pre_send_skip_symbols) == 1

    delay, attempted, _ = execute(
        report,
        enabled=True,
        attempted=True,
    )

    assert delay == 260.0
    assert attempted is True
    assert session.calls == []
