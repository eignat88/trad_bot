from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.research.me_r_long_early_mae_v3.entry_policy import (
    decide_next_open,
)


T = datetime(2026, 10, 14, tzinfo=timezone.utc)


def decide(candles, minutes):
    return decide_next_open(
        candles=candles,
        entry_eligible_at=T,
        detected_at=T,
        activation_at=T,
        asof=T + timedelta(minutes=minutes),
    )


def test_p2_before_entry_open_is_pending():
    result = decide([], 4)
    assert result.state == "ENTRY_PENDING"


def test_p2_entry_candle_open_but_unclosed_is_pending():
    result = decide([], 6)
    assert result.state == "ENTRY_PENDING"
    assert result.reason == "SELECTED_ENTRY_CANDLE_UNCLOSED"


def test_p2_missing_candle_after_close_is_incomplete():
    result = decide([], 10)
    assert result.state == "INCOMPLETE_COVERAGE"


def test_p1_sql_guard_requires_unambiguous_finalized_baseline():
    sql = Path(
        "sql/migrations/"
        "070_me_r_early_mae_v3_finalized_baseline_guard.sql"
    ).read_text()

    assert "chk_me_mae_v3_finalized_baseline_unambiguous" in sql
    assert "source_status <> 'FINALIZED'" in sql
    assert "baseline_exit_reason IS NOT NULL" in sql
    assert "baseline_exit_reason <> 'AMBIGUOUS_STOP_FIRST'" in sql
