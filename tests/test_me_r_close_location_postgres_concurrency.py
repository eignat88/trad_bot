"""Concurrent writes against isolated local ME PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal

from test_me_r_close_location_postgres_integration import connect_isolated
from test_me_r_close_location_postgres_atomicity import (
    make_signal,
    cleanup,
    horizon,
)
from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
)


def test_two_concurrent_writers_same_horizon():
    setup_conn = connect_isolated()
    signal_id, symbol = make_signal(setup_conn)
    barrier = Barrier(2, timeout=10)

    def writer(value):
        conn = connect_isolated()
        try:
            repo = MERLongCLoOosRepository(conn)
            barrier.wait()
            return repo.save_outcome_partial(
                signal_id=signal_id,
                symbol=symbol,
                **horizon(15, value=value),
            )
        finally:
            conn.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(writer, 3.0)
            b = pool.submit(writer, 7.0)
            assert a.result(timeout=20) is True
            assert b.result(timeout=20) is True

        cursor = setup_conn.cursor()
        cursor.execute(
            """
            SELECT mfe_15m, mae_15m, mfe_15m_r, mae_15m_r,
                   evaluated_15m_at
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        row = cursor.fetchone()

        assert row is not None
        assert row[0] in (Decimal("3.0"), Decimal("7.0"))
        assert row[1] == Decimal("1.0")
        assert row[2] == row[0] / Decimal("2.5")
        assert row[3] == Decimal("0.4")
        assert row[4] is not None

    finally:
        cleanup(setup_conn, signal_id)


def test_concurrent_120m_and_240m_finalization():
    setup_conn = connect_isolated()
    signal_id = None

    try:
        signal_id, symbol = make_signal(setup_conn)
        repo = MERLongCLoOosRepository(setup_conn)

        # The first three horizons are already finalized.
        for h in (15, 30, 60):
            assert repo.save_outcome_partial(
                signal_id=signal_id,
                symbol=symbol,
                **horizon(h),
            ) is True

        barrier = Barrier(2, timeout=10)

        def writer(h):
            conn = connect_isolated()
            try:
                local_repo = MERLongCLoOosRepository(conn)
                barrier.wait()
                return local_repo.save_outcome_partial(
                    signal_id=signal_id,
                    symbol=symbol,
                    is_final=(h == 240),
                    **horizon(h),
                )
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_120 = pool.submit(writer, 120)
            future_240 = pool.submit(writer, 240)

            result_120 = future_120.result(timeout=20)
            result_240 = future_240.result(timeout=20)

        assert result_120 is True

        cursor = setup_conn.cursor()
        cursor.execute(
            """
            SELECT is_final,
                   evaluated_120m_at,
                   evaluated_240m_at,
                   mfe_15m, mfe_30m, mfe_60m,
                   mfe_120m, mfe_240m
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        row = cursor.fetchone()

        assert row is not None
        final, t120, t240, m15, m30, m60, m120, m240 = row

        assert t120 is not None
        assert float(m120) == 3.0
        assert all(float(x) == 3.0 for x in (m15, m30, m60))

        if result_240:
            assert final is True
            assert t240 is not None
            assert float(m240) == 3.0
        else:
            # 240m arrived before 120m: rejection is correct.
            assert final is False
            assert t240 is None
            assert m240 is None

            # Evaluator retry after 120m exists must succeed.
            assert repo.save_outcome_partial(
                signal_id=signal_id,
                symbol=symbol,
                is_final=True,
                **horizon(240),
            ) is True

        cursor.execute(
            """
            SELECT is_final,
                   evaluated_15m_at, evaluated_30m_at,
                   evaluated_60m_at, evaluated_120m_at,
                   evaluated_240m_at,
                   mfe_15m, mfe_30m, mfe_60m,
                   mfe_120m, mfe_240m
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (signal_id,),
        )
        finished = cursor.fetchone()

        assert finished is not None
        assert finished[0] is True
        assert all(value is not None for value in finished[1:6])
        assert all(float(value) == 3.0 for value in finished[6:])

    finally:
        if signal_id is not None:
            cleanup(setup_conn, signal_id)
        else:
            setup_conn.close()
