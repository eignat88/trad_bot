"""Offline integration: real CycleCompletionReport + actual scheduler block."""

from types import SimpleNamespace

import pytest

from app.scanners.cycle_completion_v1 import (
    CycleCompletionReport,
    SymbolOutcome,
    CATEGORY_SKIPPED,
    REQUEST_STATE_PRE_SEND_SKIP,
    REQUEST_STATE_RATE_LIMIT_RESPONSE,
    REQUEST_STATE_UNKNOWN,
    STAGE_MARKET_DATA,
)

from app.scanners.recovery_policy_v1 import decide_recovery
from test_scanner_ac7_recovery_wiring_v1 import SCHEDULER_CODE


def make_report(processed, skips, request_state):
    report = CycleCompletionReport(expected_symbols=50)

    for i in range(processed):
        report.record_processed(f"S{i}")

    for i in range(skips):
        report.record_outcome(
            SymbolOutcome(
                symbol=f"S{processed+i}",
                stage=STAGE_MARKET_DATA,
                category=CATEGORY_SKIPPED,
                error_type="BybitRateLimitError",
                rate_limit=True,
                request_state=request_state,
                message="synthetic offline transport outcome",
            )
        )

    return report


def execute(report, *, enabled=True, attempted=False):
    events = []

    state = {
        "ac7_recovery_enabled": enabled,
        "ac7_cycle_finished_ok": True,
        "ac7_recovery_attempted": attempted,
        "report": report,
        "delay": 260.0,
        "decide_recovery": decide_recovery,
        "cycle": 297,
        "logger": SimpleNamespace(
            info=lambda *a: events.append(("info", a)),
            exception=lambda *a: events.append(("exception", a)),
        ),
    }

    exec(SCHEDULER_CODE, state)

    return (
        state["delay"],
        state["ac7_recovery_attempted"],
        events,
    )


def test_complete_50_of_50():
    report = make_report(50, 0, REQUEST_STATE_PRE_SEND_SKIP)

    assert report.is_complete
    assert report.completion_status == "COMPLETE"
    assert report.run_status == "COMPLETED"

    delay, guard, _ = execute(report, attempted=True)

    assert delay == 260.0
    assert guard is False


def test_incident_297_0_of_50():
    report = make_report(0, 50, REQUEST_STATE_PRE_SEND_SKIP)

    assert not report.is_complete
    assert report.completion_status == "FAILED"
    assert report.run_status == "PARTIAL"
    assert len(report.limiter_pre_send_skip_symbols) == 50

    delay, guard, events = execute(report)

    assert delay == 130.0
    assert guard is True
    assert not any(level == "exception" for level, _ in events)


def test_partial_44_of_50():
    report = make_report(44, 6, REQUEST_STATE_PRE_SEND_SKIP)

    assert report.completion_status == "PARTIAL"
    assert report.run_status == "PARTIAL"
    assert len(report.limiter_pre_send_skip_symbols) == 6

    delay, guard, _ = execute(report)

    assert delay == 130.0
    assert guard is True


@pytest.mark.parametrize("request_state", [
    REQUEST_STATE_RATE_LIMIT_RESPONSE,
    REQUEST_STATE_UNKNOWN,
])
def test_without_confirmed_pre_send_skip_no_recovery(request_state):
    report = make_report(0, 50, request_state)

    assert not report.is_complete
    assert report.limiter_pre_send_skip_symbols == []

    delay, guard, _ = execute(report)

    assert delay == 260.0
    assert guard is False


def test_restart_guard_prevents_acceleration():
    report = make_report(0, 50, REQUEST_STATE_PRE_SEND_SKIP)

    delay, guard, _ = execute(report, attempted=True)

    assert delay == 260.0
    assert guard is True


def test_disabled_feature_preserves_schedule():
    report = make_report(0, 50, REQUEST_STATE_PRE_SEND_SKIP)

    delay, guard, events = execute(report, enabled=False)

    assert delay == 260.0
    assert guard is False
    assert events == []


def test_inconsistent_report_fails_closed():
    report = make_report(50, 1, REQUEST_STATE_PRE_SEND_SKIP)

    assert not report.is_complete

    delay, guard, events = execute(report)

    assert delay == 260.0
    assert guard is False
    assert any(level == "exception" for level, _ in events)


def test_empty_universe_does_not_trigger_recovery():
    report = CycleCompletionReport(expected_symbols=0)

    assert report.is_complete

    delay, guard, _ = execute(report, attempted=True)

    assert delay == 260.0
    assert guard is False
