from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
    SRR_EXPERIMENT_ID,
    SrrRouteResult,
)


class FakeCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return [
            (
                123, "AAAUSDT",
                datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
                SRR_EXPERIMENT_ID, "SHORT",
                100, 100.5, 100, 101, 99.25,
                '{"_frozen_freeze_ts":"2026-10-07T08:17:50Z"}',
                None, None,
            )
        ]

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj


def make_result(final):
    return SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=123,
        status="FINALIZED" if final else "INCOMPLETE_COVERAGE",
        reason_code="TEST",
        path_class="TIMEOUT" if final else None,
        finalization_eligible=final,
        source_status="SOURCE_VALIDATED",
        geometry_valid=True,
        eligible_candle_count=24 if final else 0,
        gross_r=0.75 if final else None,
        cost_r_normal=0.21 if final else None,
        cost_r_elevated=0.31 if final else None,
        net_r_normal=0.54 if final else None,
        net_r_elevated=0.44 if final else None,
        policy_result={
            "cutoff_time_ms": 1791370800000,
        } if final else {},
        source_diagnostics={},
        route_diagnostics={},
    )


@pytest.mark.parametrize(
    "final,action,expected_finalized",
    [
        (False, "INSERTED", 0),
        (False, "REFRESHED", 0),
        (True, "INSERTED", 1),
        (True, "FINALIZED", 1),
        (True, "ALREADY_FINALIZED", 0),
    ],
)
def test_runtime_callback(final, action, expected_finalized):
    conn = FakeConn()
    writer = Mock(
        return_value=SimpleNamespace(action=action, is_final=final)
    )

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=Mock(),
        dry_run=False,
        write_outcomes=writer,
    )
    evaluator.route = Mock(return_value=make_result(final))

    evaluator._current_asof_ms = lambda: 1791370800000
    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    assert stats["errors"] == 0
    assert stats["finalized"] == expected_finalized
    assert stats["observations"][0]["write_action"] == action

    kwargs = writer.call_args.kwargs
    assert kwargs["observation_id"] == 123
    assert kwargs["experiment_id"] == SRR_EXPERIMENT_ID
    assert kwargs["result"]["finalization_eligible"] is final
    assert kwargs["result"]["source_diagnostics"] == {}
    assert kwargs["result"]["route_diagnostics"] == {}

    assert "srr_short_execution_prospective_outcome" in \
        conn.cursor_obj.sql[0][0]
    assert "LEFT JOIN research.prospective_outcome " not in \
        conn.cursor_obj.sql[0][0]


def test_writer_failure_is_reported():
    writer = Mock(side_effect=RuntimeError("TEST_WRITE_FAILURE"))

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=FakeConn(),
        candle_source=Mock(),
        dry_run=False,
        write_outcomes=writer,
    )
    evaluator.route = Mock(return_value=make_result(True))

    evaluator._current_asof_ms = lambda: 1791370800000
    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    assert stats["errors"] == 1
    assert stats["finalized"] == 0
    assert stats["observations"][0]["write_action"] == "WRITE_FAILED"


def test_dry_run_does_not_call_writer():
    writer = Mock()

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=FakeConn(),
        candle_source=Mock(),
        dry_run=True,
        write_outcomes=writer,
    )
    evaluator.route = Mock(return_value=make_result(True))

    evaluator._current_asof_ms = lambda: 1791370800000
    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    assert stats["finalized"] == 1
    writer.assert_not_called()


def test_write_mode_requires_writer():
    with pytest.raises(ValueError, match="write_outcomes"):
        SrrShortExecutionRExpansionProspectiveEvaluator(
            conn=FakeConn(),
            candle_source=Mock(),
            dry_run=False,
        )
