import ast
import inspect
import textwrap

import scanner_runner
from test_scanner_ac7_recovery_wiring_v1 import schedule


def test_restart_guard_initially_closed():
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(scanner_runner.main))
    )
    main = tree.body[0]
    assignments = [
        node for node in main.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name)
            and t.id == "ac7_recovery_attempted"
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    assert len(assignments) == 1
    assert assignments[0].value.value is True


def test_first_failed_cycle_after_restart_not_expedited():
    delay, guard, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=True,
    )
    assert delay == 260.0
    assert guard is True


def test_success_rearms_single_recovery():
    _, guard, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=True,
        processed=50,
        skips=0,
        already_attempted=True,
    )
    assert guard is False

    delay, guard, _ = schedule(
        enabled=True,
        finished_ok=True,
        complete=False,
        processed=0,
        skips=50,
        already_attempted=guard,
    )
    assert delay == 130.0
    assert guard is True
