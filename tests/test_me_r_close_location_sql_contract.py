from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)


def make_repo(fetchone=(123,)):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = fetchone
    return MERLongCLoOosRepository(conn), conn, cursor


def test_clean_signal_sql_contract():
    repo, conn, cursor = make_repo()

    decision = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
    candle_open = datetime(2026, 10, 9, 13, 55, tzinfo=timezone.utc)

    result = repo.save_signal(
        symbol="BTCUSDT",
        signal_time=candle_open,
        signal_price=100.0,
        open=101.0,
        high=103.0,
        low=99.0,
        close=100.0,
        volume=1200.0,
        close_location=0.25,
        close_location_threshold=0.70,
        filter_passed=False,
        rsi=75.0,
        atr=1.5,
        signal_version="1.2.0",
        decision_time=decision,
        signal_candle_open_time=candle_open,
    )

    sql, params = cursor.execute.call_args.args

    assert "decision_time" in sql
    assert "signal_candle_open_time" in sql
    assert "ON CONFLICT" in sql
    assert sql.count("%s") == len(params)

    assert params[-3:] == (
        "1.2.0",
        decision,
        candle_open,
    )

    assert result.status == MERLongCLoOosSaveStatus.INSERTED
    assert result.signal_id == 123
    conn.commit.assert_called_once()


def test_outcome_upsert_preserves_existing_values():
    repo, conn, cursor = make_repo()
    cursor.fetchone.side_effect = [(123,), None]

    saved = repo.save_outcome_partial(
        signal_id=123,
        symbol="BTCUSDT",
        mfe_15m=3.0,
        mae_15m=1.0,
        mfe_15m_r=1.2,
        mae_15m_r=0.4,
        evaluated_15m_at=datetime(
            2026, 10, 9, 14, 20, tzinfo=timezone.utc
        ),
    )

    sql, params = cursor.execute.call_args.args

    assert saved is True
    assert (
        "COALESCE(me_r_long_close_location_oos_outcome.mfe_15m, "
        "EXCLUDED.mfe_15m)"
    ) in sql

    assert (
        "COALESCE(me_r_long_close_location_oos_outcome.mae_15m, "
        "EXCLUDED.mae_15m)"
    ) in sql

    assert sql.count("%s") == len(params)
    conn.commit.assert_called_once()


def test_empty_outcome_does_not_write():
    repo, conn, cursor = make_repo()

    assert repo.save_outcome_partial(
        signal_id=123,
        symbol="BTCUSDT",
    ) is False

    cursor.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_sql_failure_rolls_back():
    repo, conn, cursor = make_repo()
    cursor.execute.side_effect = RuntimeError("database failure")

    result = repo.save_outcome_partial(
        signal_id=123,
        symbol="BTCUSDT",
        mfe_15m=3.0,
        mae_15m=1.0,
        mfe_15m_r=1.2,
        mae_15m_r=0.4,
        evaluated_15m_at=datetime(
            2026, 10, 9, 14, 20, tzinfo=timezone.utc
        ),
    )

    assert result is False
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()



def test_rejects_partial_horizon():
    repo, conn, cursor = make_repo()

    result = repo.save_outcome_partial(
        signal_id=123,
        symbol="BTCUSDT",
        mfe_15m=3.0,
    )

    assert result is False
    cursor.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_rejects_premature_finalization():
    repo, conn, cursor = make_repo()
    cursor.fetchone.side_effect = [(123,), None]

    result = repo.save_outcome_partial(
        signal_id=123,
        symbol="BTCUSDT",
        mfe_240m=3.0,
        mae_240m=1.0,
        mfe_240m_r=1.2,
        mae_240m_r=0.4,
        evaluated_240m_at=datetime(
            2026, 10, 9, 18, 0, tzinfo=timezone.utc
        ),
        is_final=True,
    )

    assert result is False
    assert cursor.execute.call_count == 2
    assert all(
        call.args[0].lstrip().startswith("SELECT")
        for call in cursor.execute.call_args_list
    )
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
