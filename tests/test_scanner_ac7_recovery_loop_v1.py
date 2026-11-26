"""Execute the actual main-loop AST with fake I/O and a fake clock."""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
import scanner_runner as sr


class Report:
    def __init__(self):
        self.expected_symbols = 0
        self.processed = []
        self.skips = []

    @property
    def processed_count(self):
        return len(self.processed)

    @property
    def is_complete(self):
        return (
            len(self.processed) == self.expected_symbols
            and not self.skips
        )

    @property
    def run_status(self):
        return "COMPLETED" if self.is_complete else "PARTIAL"

    @property
    def completion_status(self):
        return "COMPLETE" if self.is_complete else "FAILED"

    @property
    def failed_symbols(self):
        return []

    @property
    def skipped_symbols(self):
        return self.skips

    @property
    def rate_limit_response_symbols(self):
        return []

    @property
    def limiter_pre_send_skip_symbols(self):
        return self.skips

    def log_summary(self):
        return "synthetic limiter pre-send skips"


def main_loop_code():
    tree = ast.parse(textwrap.dedent(inspect.getsource(sr.main)))
    body = tree.body[0].body
    loops = [
        n for n in body
        if isinstance(n, ast.While)
        and isinstance(n.test, ast.UnaryOp)
        and isinstance(n.test.op, ast.Not)
        and isinstance(n.test.operand, ast.Name)
        and n.test.operand.id == "SHUTDOWN"
    ]
    assert len(loops) == 1
    mod = ast.Module(body=loops, type_ignores=[])
    return compile(ast.fix_missing_locations(mod), "<real_loop>", "exec")


@pytest.mark.parametrize(
    "enabled,expected_delays",
    [(True, [260, 130, 260]), (False, [260, 260, 260])],
)
def test_three_real_loop_iterations(enabled, expected_delays):
    clock = [0.0]
    run_ids = []
    finished = []
    delays = []
    oos_calls = []

    class Repo:
        def start_run(self, **kwargs):
            run_ids.append(len(run_ids) + 1)
            return run_ids[-1]

        def save_run_universe(self, *args):
            pass

        def finish_run(self, run_id, **kwargs):
            finished.append((run_id, kwargs["status"]))

    state = {}

    def scan(*args, report, **kwargs):
        run_id = args[4]
        clock[0] += 40.0
        report.expected_symbols = 50
        if run_id == 2:
            report.skips = [f"S{i}" for i in range(50)]
        else:
            report.processed = [f"S{i}" for i in range(50)]
        return 0, len(report.processed), len(report.skips)

    def log_info(fmt, *args):
        if fmt.startswith("cycle #%d sleeping"):
            delays.append(args[1])

    def fake_sleep(seconds):
        clock[0] += seconds
        if len(run_ids) >= 3:
            state["SHUTDOWN"] = True

    state.update({
        "SHUTDOWN": False,
        "cycle": 0,
        "ac7_recovery_enabled": enabled,
        "ac7_recovery_attempted": True,
        "symbols": [],
        "universe": [],
        "client": object(),
        "orchestrator": object(),
        "repository": Repo(),
        "settings": SimpleNamespace(
            scan_interval=300,
            scanner_universe=SimpleNamespace(mode="test"),
            oos_evaluator_cycle_interval=0,
        ),
        "source_only": True,
        "expectancy_filter": None,
        "prospective_obs": None,
        "me_early_mae_v2_pipeline": None,
        "me_capture_spool": None,
        "get_scanner_universe": lambda *_: [
            {"symbol": f"S{i}"} for i in range(50)
        ],
        "run_scan_cycle": scan,
        "CycleCompletionReport": Report,
        "_postscan_housekeeping": lambda *a, **k: 0,
        "_run_oos_evaluator": lambda *a: oos_calls.append(a),
        "report_aborted_cycle": lambda *a: pytest.fail("unexpected abort"),
        "load_expectancy": lambda *a: None,
        "seconds_until_next_cycle": sr.seconds_until_next_cycle,
        "decide_recovery": sr.decide_recovery,
        "time": SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=fake_sleep,
        ),
        "logger": SimpleNamespace(
            info=log_info,
            warning=lambda *a: None,
            exception=lambda *a: pytest.fail("unexpected exception"),
            error=lambda *a: None,
        ),
    })

    exec(main_loop_code(), state)

    assert run_ids == [1, 2, 3]
    assert finished == [
        (1, "COMPLETED"),
        (2, "PARTIAL"),
        (3, "COMPLETED"),
    ]
    assert delays == expected_delays
    assert oos_calls == []
    assert state["ac7_recovery_attempted"] is (not enabled)
