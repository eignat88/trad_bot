"""Offline execution of the actual Scanner recovery scheduling block.

No main() execution, PostgreSQL, Bybit or systemd.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

import scanner_runner
from app.scanners.recovery_policy_v1 import decide_recovery


def _scheduler_code():
    source = inspect.getsource(scanner_runner.main)
    tree = ast.parse(textwrap.dedent(source))

    main = tree.body[0]

    candidates = [
        node for node in ast.walk(main)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and any(
            isinstance(v, ast.Name)
            and v.id == "ac7_recovery_enabled"
            for v in node.test.values
        )
    ]

    assert len(candidates) == 1, (
        "Expected exactly one AC7 scheduling integration block"
    )

    block = candidates[0]

    module = ast.Module(
        body=[block],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    return compile(
        module,
        "<scanner_ac7_actual_scheduler>",
        "exec",
    )


SCHEDULER_CODE = _scheduler_code()


def schedule(
    *,
    enabled,
    finished_ok,
    complete,
    processed,
    skips,
    already_attempted,
    normal_delay=260.0,
):
    logs = []

    report = SimpleNamespace(
        is_complete=complete,
        expected_symbols=50,
        processed_count=processed,
        limiter_pre_send_skip_symbols=[
            f"SYM{i}" for i in range(skips)
        ],
    )

    state = {
        "ac7_recovery_enabled": enabled,
        "ac7_cycle_finished_ok": finished_ok,
        "ac7_recovery_attempted": already_attempted,
        "report": report,
        "delay": normal_delay,
        "decide_recovery": decide_recovery,
        "cycle": 297,
        "logger": SimpleNamespace(
            info=lambda *args: logs.append(("info", args)),
            exception=lambda *args: logs.append(("exception", args)),
        ),
    }

    exec(SCHEDULER_CODE, state)

    return (
        state["delay"],
        state["ac7_recovery_attempted"],
        logs,
    )


def test_feature_flag_off_preserves_schedule():
    delay, attempted, logs = schedule(
        enabled=False,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=False,
    )
    assert delay == 260.0
    assert attempted is False
    assert not logs


def test_incident_297_expedites_once():
    delay, attempted, logs = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=False,
    )
    assert delay == 130.0
    assert attempted is True
    assert logs


def test_second_incomplete_cycle_does_not_storm():
    delay, attempted, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=True,
    )
    assert delay == 260.0
    assert attempted is True


def test_complete_cycle_resets_one_shot_guard():
    delay, attempted, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=True,
        processed=50,
        skips=0,
        already_attempted=True,
    )
    assert delay == 260.0
    assert attempted is False


def test_aborted_cycle_never_expedites():
    delay, attempted, _ = schedule(
        enabled=True,
        finished_ok=False,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=False,
    )
    assert delay == 260.0
    assert attempted is False


def test_non_limiter_failure_not_retried():
    delay, attempted, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=44,
        skips=0,
        already_attempted=False,
    )
    assert delay == 260.0
    assert attempted is False


def test_expediting_never_lengthens_schedule():
    delay, attempted, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=False,
        normal_delay=60.0,
    )
    assert delay == 60.0
    assert attempted is False


def test_unexpected_report_fails_closed():
    delay, attempted, logs = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=50,
        skips=50,
        already_attempted=False,
    )
    assert delay == 260.0
    assert attempted is False
    assert any(level == "exception" for level, _ in logs)
    assert not any(level == "info" for level, _ in logs)
