from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.research.me_reverse_long_v3.repository import (
    MEReverseLongV3OutcomeRepository,
    V3OutcomeRecord,
    validate_record,
)

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def record(status="PENDING", **kwargs):
    data = dict(
        observation_id=101,
        status=status,
        filter_group="PASS",
        execution_entry_at=NOW,
        evaluated_at=NOW,
    )
    data.update(kwargs)
    return V3OutcomeRecord(**data)


def test_pending_valid():
    validate_record(record())


def test_nonfinal_economics_rejected():
    with pytest.raises(ValueError, match="NONFINAL_ECONOMICS"):
        validate_record(record(gross_r=1.0))


def test_coverage_reason_required():
    with pytest.raises(ValueError, match="COVERAGE_REASON"):
        validate_record(record("COVERAGE_INVALID"))


def test_valid_finalization():
    validate_record(record(
        "FINALIZED",
        execution_entry_price=100.0,
        execution_stop_price=97.5,
        execution_target_price=103.0,
        exit_at=NOW,
        exit_price=103.0,
        exit_reason="TP_FIRST",
        gross_r=1.2,
        net_r_normal=1.1,
        net_r_elevated=1.05,
        ambiguous=False,
        candle_count=48,
    ))


def test_incomplete_finalization_rejected():
    with pytest.raises(ValueError, match="INVALID_FINALIZED"):
        validate_record(record("FINALIZED"))


def test_successful_atomic_write():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.side_effect = [(101,), (101,)]

    result = MEReverseLongV3OutcomeRepository(conn).upsert(record())

    assert result == "INSERTED_OR_UPDATED"
    assert cursor.execute.call_count == 2
    assert "FOR UPDATE OF o" in cursor.execute.call_args_list[0].args[0]
    assert "status = 'RUNNING'" in cursor.execute.call_args_list[0].args[0]
    assert "DO UPDATE SET" in cursor.execute.call_args_list[1].args[0]
    assert "<> 'FINALIZED'" in cursor.execute.call_args_list[1].args[0]
    conn.commit.assert_called_once()
    cursor.close.assert_called_once()


def test_unowned_observation_is_not_written():
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None

    result = MEReverseLongV3OutcomeRepository(conn).upsert(record())

    assert result == "NOT_ELIGIBLE"
    assert conn.cursor.return_value.execute.call_count == 1
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_finalized_row_is_immutable():
    conn = MagicMock()
    conn.cursor.return_value.fetchone.side_effect = [(101,), None]

    result = MEReverseLongV3OutcomeRepository(conn).upsert(record())

    assert result == "ALREADY_FINALIZED"
    conn.commit.assert_called_once()


def test_sql_failure_rolls_back():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (101,)
    cursor.execute.side_effect = [None, RuntimeError("test failure")]

    with pytest.raises(RuntimeError, match="test failure"):
        MEReverseLongV3OutcomeRepository(conn).upsert(record())

    conn.rollback.assert_called_once()
    cursor.close.assert_called_once()
