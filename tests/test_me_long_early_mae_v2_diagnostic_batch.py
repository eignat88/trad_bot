"""Diagnostic batch integrity regression tests for Early MAE V2."""

from unittest.mock import MagicMock

import pytest

from app.research.me_r_long_early_mae_v2.protocol import VARIANTS
from app.research.me_r_long_early_mae_v2.repository import MeEarlyMaeV2Repository


def diagnostic_rows():
    return [
        {
            "variant_id": v.variant_id,
            "variant_status": "INCOMPLETE_COVERAGE",
            "trigger_result": "INCOMPLETE_COVERAGE",
            "mae_r": None,
            "mfe_r": None,
            "window_low": None,
            "window_high": None,
            "variant_exit_at": None,
            "variant_exit_price": None,
            "variant_exit_reason": None,
            "variant_gross_r": None,
            "variant_net_r": None,
            "delta_net_r": None,
            "ambiguous_ordering": False,
            "coverage_reason": "BASELINE_CANDLE_COVERAGE_INCOMPLETE",
        }
        for v in VARIANTS
    ]


def make_repo(statuses=None, source_status="ENTRY_FILLED", fail_at=None):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (
        (source_status,) if source_status is not None else None
    )

    statuses = statuses or {
        v.variant_id: "PENDING" for v in VARIANTS
    }
    cursor.fetchall.return_value = list(statuses.items())

    updates = []

    def execute(sql, params=None):
        if sql.lstrip().upper().startswith("UPDATE") and "variant_outcome" in sql:
            updates.append(params["variant_id"])
            if fail_at is not None and len(updates) == fail_at:
                raise RuntimeError("Injected variant update failure")
        cursor.rowcount = 1

    cursor.execute.side_effect = execute
    return MeEarlyMaeV2Repository(conn), conn, updates


def test_diagnostic_batch_requires_seven_unique_variants():
    repo, conn, _ = make_repo()
    with pytest.raises(ValueError, match="seven unique"):
        repo.save_variant_batch(1, diagnostic_rows()[:6])
    conn.commit.assert_not_called()


def test_diagnostic_batch_rejects_missing_source():
    repo, conn, _ = make_repo(source_status=None)
    with pytest.raises(ValueError, match="ENTRY_FILLED"):
        repo.save_variant_batch(1, diagnostic_rows())
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_diagnostic_batch_rejects_final_result():
    repo, conn, _ = make_repo()
    rows = diagnostic_rows()
    rows[0]["variant_status"] = "EVALUATED_NO_TRIGGER"
    with pytest.raises(ValueError, match="Diagnostic batch"):
        repo.save_variant_batch(1, rows)
    conn.commit.assert_not_called()


def test_diagnostic_batch_rejects_incomplete_stored_tuple():
    repo, conn, _ = make_repo(
        statuses={v.variant_id: "PENDING" for v in VARIANTS[:6]}
    )
    with pytest.raises(ValueError, match="Incomplete frozen"):
        repo.save_variant_batch(1, diagnostic_rows())
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_diagnostic_batch_fourth_update_rolls_back():
    repo, conn, updates = make_repo(fail_at=4)
    with pytest.raises(RuntimeError, match="Injected"):
        repo.save_variant_batch(1, diagnostic_rows())
    assert len(updates) == 4
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_diagnostic_batch_preserves_completed_variants():
    statuses = {v.variant_id: "PENDING" for v in VARIANTS}
    statuses[VARIANTS[0].variant_id] = "AMBIGUOUS"
    statuses[VARIANTS[1].variant_id] = "EVALUATED_NO_TRIGGER"
    statuses[VARIANTS[2].variant_id] = "BASELINE_EXITED_EARLY"

    repo, conn, updates = make_repo(statuses=statuses)
    repo.save_variant_batch(1, diagnostic_rows())

    assert len(updates) == 4
    assert VARIANTS[0].variant_id not in updates
    assert VARIANTS[1].variant_id not in updates
    assert VARIANTS[2].variant_id not in updates
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_diagnostic_batch_rejects_finalized_variant():
    statuses = {v.variant_id: "PENDING" for v in VARIANTS}
    statuses[VARIANTS[0].variant_id] = "FINALIZED"

    repo, conn, _ = make_repo(statuses=statuses)
    with pytest.raises(ValueError, match="Finalized variant"):
        repo.save_variant_batch(1, diagnostic_rows())
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
