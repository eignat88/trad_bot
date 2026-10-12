"""Outcome integrity tests against isolated local PostgreSQL only."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
)
from test_me_r_close_location_postgres_integration import connect_isolated


def make_signal(conn):
    symbol = "MEA" + uuid4().hex[:12].upper()
    signal_time = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO dds.me_r_long_close_location_oos_signal (
            symbol, signal_time, signal_price,
            open, high, low, close, volume,
            filter_passed, signal_version, decision_time,
            signal_candle_open_time
        ) VALUES (
            %s, %s, 100,
            100, 101, 99, 100, 1000,
            TRUE, '1.2.0', %s, %s
        )
        RETURNING signal_id
        """,
        (
            symbol,
            signal_time,
            signal_time + timedelta(minutes=5),
            signal_time,
        ),
    )
    signal_id = cursor.fetchone()[0]
    conn.commit()
    return signal_id, symbol


def cleanup(conn, signal_id):
    try:
        conn.rollback()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dds.me_r_long_close_location_oos_outcome "
            "WHERE signal_id = %s",
            (signal_id,),
        )
        cursor.execute(
            "DELETE FROM dds.me_r_long_close_location_oos_signal "
            "WHERE signal_id = %s",
            (signal_id,),
        )
        conn.commit()
    finally:
        conn.close()


def horizon(h, value=3.0):
    return {
        f"mfe_{h}m": value,
        f"mae_{h}m": 1.0,
        f"mfe_{h}m_r": value / 2.5,
        f"mae_{h}m_r": 0.4,
        f"evaluated_{h}m_at": (
            datetime(2026, 10, 9, 12, 5, tzinfo=timezone.utc)
            + timedelta(minutes=h)
        ),
    }


def test_premature_finalization_leaves_no_outcome():
    conn = connect_isolated()
    signal_id = None
    try:
        signal_id, symbol = make_signal(conn)
        repo = MERLongCLoOosRepository(conn)

        assert repo.save_outcome_partial(
            signal_id=signal_id,
            symbol=symbol,
            is_final=True,
            **horizon(240),
        ) is False

        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) "
            "FROM dds.me_r_long_close_location_oos_outcome "
            "WHERE signal_id = %s",
            (signal_id,),
        )
        assert cursor.fetchone()[0] == 0
    finally:
        if signal_id is not None:
            cleanup(conn, signal_id)
        else:
            conn.close()


def test_damaged_horizon_cannot_be_repaired_implicitly():
    conn = connect_isolated()
    signal_id = None
    try:
        signal_id, symbol = make_signal(conn)

        # Deliberately inject damage into isolated test DB.
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dds.me_r_long_close_location_oos_outcome (
                signal_id, symbol, mfe_15m
            ) VALUES (%s, %s, 3.0)
            """,
            (signal_id, symbol),
        )
        conn.commit()

        repo = MERLongCLoOosRepository(conn)
        assert repo.save_outcome_partial(
            signal_id=signal_id,
            symbol=symbol,
            **horizon(15, value=9.0),
        ) is False

        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mfe_15m, mae_15m, evaluated_15m_at,
                   is_final
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        mfe, mae, evaluated, final = cursor.fetchone()

        assert float(mfe) == 3.0
        assert mae is None
        assert evaluated is None
        assert final is False
    finally:
        if signal_id is not None:
            cleanup(conn, signal_id)
        else:
            conn.close()


def test_staged_finalization_preserves_horizons():
    conn = connect_isolated()
    signal_id = None
    try:
        signal_id, symbol = make_signal(conn)
        repo = MERLongCLoOosRepository(conn)

        for h in (15, 30, 60, 120):
            assert repo.save_outcome_partial(
                signal_id=signal_id,
                symbol=symbol,
                **horizon(h),
            ) is True

        assert repo.save_outcome_partial(
            signal_id=signal_id,
            symbol=symbol,
            is_final=True,
            **horizon(240),
        ) is True

        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT is_final,
                   evaluated_15m_at, evaluated_30m_at,
                   evaluated_60m_at, evaluated_120m_at,
                   evaluated_240m_at, mfe_15m
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        row = cursor.fetchone()

        assert row[0] is True
        assert all(value is not None for value in row[1:6])
        assert float(row[6]) == 3.0

        # Retry a completed horizon with different values.
        assert repo.save_outcome_partial(
            signal_id=signal_id,
            symbol=symbol,
            **horizon(15, value=999.0),
        ) is True

        cursor.execute(
            """
            SELECT mfe_15m, is_final
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        mfe, final = cursor.fetchone()
        assert float(mfe) == 3.0
        assert final is True
    finally:
        if signal_id is not None:
            cleanup(conn, signal_id)
        else:
            conn.close()
