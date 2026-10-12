"""Canonical ME clean cohort accounting contract."""

from unittest.mock import MagicMock

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
)


def test_clean_cohort_separates_invalid():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (
        10, 4, 5, 1, None, None, True,
    )

    stats = MERLongCLoOosRepository(conn).get_cohort_stats()

    assert len(stats) == 1
    row = stats[0]
    assert row["signal_version"] == "1.2.0"
    assert row["signals"] == 10
    assert row["pass_count"] == 4
    assert row["reject_count"] == 5
    assert row["invalid_count"] == 1
    assert row["cohort_integrity"] is True

    sql = cursor.execute.call_args.args[0]
    assert "dds.v_me_r_cl_clean_cohort_v120" in sql


def test_cohort_integrity_detects_mismatch():
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = (
        10, 4, 4, 1, None, None, False,
    )

    row = MERLongCLoOosRepository(conn).get_cohort_stats()[0]
    assert row["cohort_integrity"] is False
