"""ME Close Location versioned persistence contract tests."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)


def test_signal_exists_is_version_scoped():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (1,)

    repo = MERLongCLoOosRepository(conn)
    when = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    assert repo.signal_exists("BTCUSDT", when, "1.2.0")

    sql, params = cursor.execute.call_args.args

    assert "AND signal_version = %s" in sql
    assert params == ("BTCUSDT", when, "1.2.0")


def test_signal_exists_legacy_default():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = None

    repo = MERLongCLoOosRepository(conn)
    when = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    assert not repo.signal_exists("BTCUSDT", when)

    _, params = cursor.execute.call_args.args

    assert params == ("BTCUSDT", when, "1.0.0")


def test_save_signal_uses_versioned_conflict_key():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (101,)

    repo = MERLongCLoOosRepository(conn)
    when = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    result = repo.save_signal(
        symbol="BTCUSDT",
        signal_time=when,
        signal_price=100.0,
        open=100.0,
        high=102.0,
        low=98.0,
        close=101.0,
        volume=1000.0,
        close_location=0.75,
        close_location_threshold=0.70,
        filter_passed=True,
        signal_version="1.2.0",
        decision_time=when,
        signal_candle_open_time=when,
    )

    sql, params = cursor.execute.call_args.args

    assert (
        "ON CONFLICT (experiment_id, signal_version, "
        "symbol, signal_time) DO NOTHING"
    ) in sql

    assert "1.2.0" in params
    assert result.status == MERLongCLoOosSaveStatus.INSERTED
