"""Execute the actual main-loop AST with fake I/O and a fake clock.

The harness compiles the REAL ``while not SHUTDOWN`` loop out of
``scanner_runner.main`` and runs it against fake repositories, a fake clock
and synthetic cycle reports — no PostgreSQL, no Bybit, no services.

Every scheduled cycle is an ordinary loop iteration: recovery only shortens
the sleep before the next cycle, so each rescan gets its own
``repository.start_run()`` call and its own ``run_id`` by construction.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
import scanner_runner as sr

#: Skip stories for synthetic cycles.
SKIP_PRE_SEND = "LIMITER_PRE_SEND_SKIP"      # limiter refused before send
SKIP_RESPONSE = "RATE_LIMIT_RESPONSE"        # Bybit answered 10006 / HTTP 429


class Report:
    def __init__(self):
        self.expected_symbols = 0
        self.processed = []
        self.skips = []          # every not-processed symbol (any story)
        self.pre_send = []       # subset refused by the limiter pre-send

    @property
    def processed_count(self):
        return len(self.processed)

    @property
    def unprocessed_symbols(self):
        return list(self.skips)

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
        return list(self.skips)

    @property
    def rate_limit_response_symbols(self):
        return [s for s in self.skips if s not in self.pre_send]

    @property
    def limiter_pre_send_skip_symbols(self):
        return list(self.pre_send)

    def log_summary(self):
        return "synthetic incomplete cycle"


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


MAIN_LOOP_CODE = main_loop_code()


def run_main_loop(
    cycle_specs,
    *,
    enabled=True,
    hint_fn=None,
    oos_interval=0,
    source_only=True,
    universe_size=50,
    scan_seconds=40.0,
):
    """Run the real main loop for ``len(cycle_specs)`` cycles.

    ``cycle_specs`` items are ``(processed, skipped, skip_story)`` triples
    where ``skip_story`` is :data:`SKIP_PRE_SEND` or :data:`SKIP_RESPONSE`.

    Returns ``(run_ids, finished, delays, oos_calls, state)``.
    """
    clock = [0.0]
    run_ids = []
    finished = []
    delays = []
    oos_calls = []
    start_run_calls = []
    state = {}
    spec_index = [0]

    class Repo:
        def start_run(self, **kwargs):
            start_run_calls.append(dict(kwargs))
            run_ids.append(len(run_ids) + 1)
            return run_ids[-1]

        def save_run_universe(self, *args):
            pass

        def finish_run(self, run_id, **kwargs):
            finished.append((run_id, kwargs["status"]))

    def scan(*args, report, **kwargs):
        clock[0] += scan_seconds
        processed, skipped, story = cycle_specs[spec_index[0]]
        spec_index[0] += 1
        report.expected_symbols = universe_size
        report.processed = [f"S{i}" for i in range(processed)]
        report.skips = [
            f"S{processed + i}" for i in range(skipped)
        ]
        if story == SKIP_PRE_SEND:
            report.pre_send = list(report.skips)
        else:
            report.pre_send = []
        return 0, len(report.processed), len(report.skips)

    def log_info(fmt, *args):
        if fmt.startswith("cycle #%d sleeping"):
            delays.append(args[1])

    def fake_sleep(seconds):
        clock[0] += seconds
        if len(run_ids) >= len(cycle_specs):
            state["SHUTDOWN"] = True

    if hint_fn is None:
        hint_fn = lambda _client: 0.0

    state.update({
        "SHUTDOWN": False,
        "cycle": 0,
        "ac7_recovery_enabled": enabled,
        # Fail-closed restart guard: closed for recovery until a complete
        # cycle, exactly as main() initializes it.
        "ac7_recovery_attempted": True,
        "symbols": [],
        "universe": [],
        "client": object(),
        "orchestrator": object(),
        "repository": Repo(),
        "settings": SimpleNamespace(
            scan_interval=300,
            scanner_universe=SimpleNamespace(mode="test"),
            oos_evaluator_cycle_interval=oos_interval,
        ),
        "source_only": source_only,
        "expectancy_filter": None,
        "prospective_obs": None,
        "me_early_mae_v2_pipeline": None,
        "me_capture_spool": None,
        "get_scanner_universe": lambda *_: [
            {"symbol": f"S{i}"} for i in range(universe_size)
        ],
        "run_scan_cycle": scan,
        "CycleCompletionReport": Report,
        "_postscan_housekeeping": lambda *a, **k: 0,
        "_run_oos_evaluator": lambda *a: oos_calls.append(a),
        "report_aborted_cycle": lambda *a: pytest.fail("unexpected abort"),
        "load_expectancy": lambda *a: None,
        "seconds_until_next_cycle": sr.seconds_until_next_cycle,
        "decide_recovery": sr.decide_recovery,
        "breaker_cooldown_hint_seconds": hint_fn,
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

    exec(MAIN_LOOP_CODE, state)

    return run_ids, finished, delays, oos_calls, state, start_run_calls


COMPLETE = (50, 0, SKIP_PRE_SEND)


@pytest.mark.parametrize(
    "enabled,expected_delays",
    [(True, [260, 130, 260]), (False, [260, 260, 260])],
)
def test_three_real_loop_iterations(enabled, expected_delays):
    cycle_specs = [
        COMPLETE,
        (0, 50, SKIP_PRE_SEND),
        COMPLETE,
    ]
    run_ids, finished, delays, oos_calls, state, _ = run_main_loop(
        cycle_specs, enabled=enabled,
    )

    assert run_ids == [1, 2, 3]
    assert finished == [
        (1, "COMPLETED"),
        (2, "PARTIAL"),
        (3, "COMPLETED"),
    ]
    assert delays == expected_delays
    assert oos_calls == []
    assert state["ac7_recovery_attempted"] is (not enabled)
