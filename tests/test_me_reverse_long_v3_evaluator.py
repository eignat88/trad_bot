from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import Candle

from app.research.me_reverse_long_v3.entry_policy import (
    next_executable_entry_at,
)
from app.research.me_reverse_long_v3.evaluator import (
    MEReverseLongV3ExecutionEvaluator,
)
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SOURCE_VALIDATED,
)

UTC = timezone.utc
START = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
SIGNAL = START + timedelta(seconds=1)


def make_evaluator():
    conn = MagicMock()
    source = MagicMock()

    evaluator = MEReverseLongV3ExecutionEvaluator(conn, source)
    evaluator.repository = MagicMock()
    evaluator.repository.upsert.return_value = "INSERTED_OR_UPDATED"

    row = (
        101, "BTCUSDT", SIGNAL,
        100.0, 97.5, 103.0,
        {
            "_v3_filter_group": "PASS",
            "_v3_execution_entry_at": next_executable_entry_at(SIGNAL).isoformat(),
        },
        START - timedelta(minutes=5),
    )

    evaluator._eligible = MagicMock(return_value=[row])
    return evaluator, source


def candles():
    return [
        Candle(
            timestamp=int(
                (next_executable_entry_at(SIGNAL) + timedelta(minutes=i * 5)).timestamp() * 1000
            ),
            open=100.0, high=101.0, low=99.0,
            close=100.0, volume=1.0,
        )
        for i in range(48)
    ]


def test_mature_complete_window_finalizes():
    evaluator, source = make_evaluator()
    source.fetch_5m_candles.return_value = SimpleNamespace(
        status=SOURCE_VALIDATED,
        candles=candles(),
        reason_code="SOURCE_VALIDATED",
    )

    stats = evaluator.run_evaluation_cycle(
        next_executable_entry_at(SIGNAL) + timedelta(minutes=240)
    )

    assert stats["finalized"] == 1
    record = evaluator.repository.upsert.call_args.args[0]
    assert record.status == "FINALIZED"
    assert record.exit_reason == "TIMEOUT"
    assert record.candle_count == 48
    assert record.execution_entry_at == next_executable_entry_at(SIGNAL)
    assert record.execution_entry_price == 100.0
    assert record.execution_stop_price == 97.5
    assert record.execution_target_price == 103.0


def test_immature_window_stays_pending():
    evaluator, source = make_evaluator()

    stats = evaluator.run_evaluation_cycle(
        START + timedelta(minutes=100)
    )

    assert stats["pending"] == 1
    source.fetch_5m_candles.assert_not_called()
    assert evaluator.repository.upsert.call_args.args[0].status == "PENDING"


def test_missing_candle_does_not_finalize():
    evaluator, source = make_evaluator()
    data = candles()
    data.pop(12)
    source.fetch_5m_candles.return_value = SimpleNamespace(
        status=SOURCE_VALIDATED,
        candles=data,
        reason_code="SOURCE_VALIDATED",
    )

    stats = evaluator.run_evaluation_cycle(
        next_executable_entry_at(SIGNAL) + timedelta(minutes=240)
    )

    assert stats["coverage_invalid"] == 1
    record = evaluator.repository.upsert.call_args.args[0]
    assert record.status == "COVERAGE_INVALID"
    assert record.gross_r is None


def test_incomplete_source_never_finalizes():
    evaluator, source = make_evaluator()
    data = candles()[:-1]
    source.fetch_5m_candles.return_value = SimpleNamespace(
        status=SOURCE_VALIDATED,
        candles=data,
        reason_code="SOURCE_VALIDATED",
    )

    stats = evaluator.run_evaluation_cycle(
        next_executable_entry_at(SIGNAL) + timedelta(minutes=240)
    )

    assert stats["coverage_invalid"] == 1
    record = evaluator.repository.upsert.call_args.args[0]
    assert record.status == "COVERAGE_INVALID"
    assert record.gross_r is None


def test_no_cross_experiment_query():
    evaluator, _ = make_evaluator()
    evaluator._eligible = (
        MEReverseLongV3ExecutionEvaluator
        ._eligible.__get__(evaluator)
    )
    evaluator.conn.cursor.return_value.fetchall.return_value = []

    rows = evaluator._eligible()

    assert rows == []
    query, params = (
        evaluator.conn.cursor.return_value.execute.call_args.args
    )
    assert "o.experiment_id = %s" in query
    assert "r.status <> 'FINALIZED'" in query
    assert params == (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS",
    )
