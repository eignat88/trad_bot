"""Repository lifecycle transaction regression tests."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.research.me_r_long_early_mae_v2.protocol import VARIANTS
from app.research.me_r_long_early_mae_v2.repository import MeEarlyMaeV2Repository


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def make_evaluations():
    return [
        {
            "variant_id": v.variant_id,
            "variant_status": "EVALUATED_NO_TRIGGER",
            "mae_r": 0.0,
            "mfe_r": 0.0,
            "window_low": 100.0,
            "window_high": 100.0,
            "trigger_result": "NO_TRIGGER",
            "variant_exit_at": NOW,
            "variant_exit_price": 100.0,
            "variant_exit_reason": "FIXED_HORIZON",
            "variant_gross_r": 0.0,
            "variant_net_r": -0.084,
            "delta_net_r": 0.0,
            "ambiguous_ordering": False,
            "coverage_reason": None,
        }
        for v in VARIANTS
    ]


BASELINE = {
    "exit_at": NOW,
    "exit_price": 100.0,
    "exit_reason": "FIXED_HORIZON",
    "gross_r": 0.0,
    "net_r": -0.084,
    "coverage": "COMPLETE",
}


def as_db_row(evaluation):
    fields = (
        "variant_id", "variant_status", "mae_r", "mfe_r",
        "window_low", "window_high", "trigger_result",
        "variant_exit_at", "variant_exit_price",
        "variant_exit_reason", "variant_gross_r",
        "variant_net_r", "delta_net_r",
        "ambiguous_ordering", "coverage_reason",
    )
    return tuple(evaluation[field] for field in fields)


def make_repository(fail_finalization=False):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("ENTRY_FILLED",)
    cursor.fetchall.return_value = [
        as_db_row({**row, "variant_status": "PENDING"})
        for row in make_evaluations()
    ]

    def execute(sql, params=None):
        cursor.rowcount = 1
        if fail_finalization and "SET source_status = 'FINALIZED'" in sql:
            raise RuntimeError("Injected finalization failure")

    cursor.execute.side_effect = execute
    return MeEarlyMaeV2Repository(connection), connection, cursor


def finalized_updates(cursor):
    return [
        call for call in cursor.execute.call_args_list
        if "SET source_status = 'FINALIZED'" in call.args[0]
    ]


def test_complete_tuple_finalizes_in_one_commit():
    repo, conn, cursor = make_repository()
    repo.save_paired_outcome(1, BASELINE, make_evaluations())

    assert len(finalized_updates(cursor)) == 1
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_incomplete_variant_keeps_source_open():
    repo, conn, cursor = make_repository()
    evaluations = make_evaluations()
    evaluations[0].update({
        "variant_status": "INCOMPLETE_COVERAGE",
        "trigger_result": "INCOMPLETE_COVERAGE",
        "variant_net_r": None,
        "delta_net_r": None,
        "variant_exit_at": None,
        "coverage_reason": "MISSING_CANDLE",
    })

    repo.save_paired_outcome(1, BASELINE, evaluations)

    assert finalized_updates(cursor) == []
    conn.commit.assert_called_once()


def test_ambiguous_variant_allows_finalization():
    repo, conn, cursor = make_repository()
    evaluations = make_evaluations()
    evaluations[0]["variant_status"] = "AMBIGUOUS"
    evaluations[0]["ambiguous_ordering"] = True

    repo.save_paired_outcome(1, BASELINE, evaluations)

    assert len(finalized_updates(cursor)) == 1
    conn.commit.assert_called_once()


def test_finalization_failure_rolls_back_everything():
    repo, conn, cursor = make_repository(fail_finalization=True)

    with pytest.raises(RuntimeError, match="Injected finalization failure"):
        repo.save_paired_outcome(1, BASELINE, make_evaluations())

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_ambiguous_plus_incomplete_retry_succeeds():
    repo, conn, cursor = make_repository()
    evaluations = make_evaluations()
    evaluations[0]["variant_status"] = "AMBIGUOUS"
    evaluations[0]["ambiguous_ordering"] = True

    stored = make_evaluations()
    stored[0].update(evaluations[0])
    stored[1].update({
        "variant_status": "INCOMPLETE_COVERAGE",
        "trigger_result": "INCOMPLETE_COVERAGE",
        "variant_exit_at": None,
        "variant_exit_price": None,
        "variant_exit_reason": None,
        "variant_gross_r": None,
        "variant_net_r": None,
        "delta_net_r": None,
        "coverage_reason": "MISSING_CANDLE",
    })
    cursor.fetchall.return_value = [as_db_row(row) for row in stored]

    repo.save_paired_outcome(1, BASELINE, evaluations)

    variant_updates = [
        call for call in cursor.execute.call_args_list
        if call.args[0].lstrip().upper().startswith("UPDATE ")
        and ".variant_outcome" in call.args[0]
    ]

    assert len(variant_updates) == 6
    assert all(
        call.args[1]["variant_id"] != evaluations[0]["variant_id"]
        for call in variant_updates
    )
    assert len(finalized_updates(cursor)) == 1
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_ambiguous_mismatch_rolls_back():
    repo, conn, cursor = make_repository()
    evaluations = make_evaluations()
    evaluations[0]["variant_status"] = "AMBIGUOUS"
    evaluations[0]["ambiguous_ordering"] = True

    stored = make_evaluations()
    stored[0].update(evaluations[0])
    stored[0]["variant_net_r"] = -0.25
    cursor.fetchall.return_value = [as_db_row(row) for row in stored]

    with pytest.raises(ValueError, match="Ambiguous outcome mismatch"):
        repo.save_paired_outcome(1, BASELINE, evaluations)

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_finalized_source_cannot_be_rewritten():
    repo, conn, cursor = make_repository()
    cursor.fetchone.return_value = ("FINALIZED",)

    with pytest.raises(ValueError, match="Source is not in ENTRY_FILLED state"):
        repo.save_paired_outcome(1, BASELINE, make_evaluations())

    updates = [
        call for call in cursor.execute.call_args_list
        if call.args[0].lstrip().upper().startswith("UPDATE ")
    ]

    assert updates == []
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()
