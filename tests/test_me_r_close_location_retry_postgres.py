"""ME clean OOS persistence retry on isolated PostgreSQL."""

from datetime import datetime, timezone
from uuid import uuid4

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)
from test_me_r_close_location_postgres_integration import connect_isolated


class NoCommitAdapter:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        pass

    def rollback(self):
        self.conn.rollback()


class FailOnceAdapter(NoCommitAdapter):
    def __init__(self, conn):
        super().__init__(conn)
        self.fail_once = True

    def cursor(self):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("Simulated transient persistence failure")
        return self.conn.cursor()


def test_failed_save_retry_inserted_then_duplicate():
    conn = connect_isolated()

    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '60s'")

        cur.execute("""
            CREATE UNIQUE INDEX
                uq_me_r_cl_oos_experiment_version_symbol_time
            ON dds.me_r_long_close_location_oos_signal
                (experiment_id, signal_version, symbol, signal_time)
        """)

        cur.execute("""
            ALTER TABLE dds.me_r_long_close_location_oos_signal
            DROP CONSTRAINT
                me_r_long_close_location_oos__experiment_id_symbol_signal_t_key
        """)

        moment = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        symbol = "MR" + uuid4().hex[:8].upper()

        kwargs = dict(
            symbol=symbol,
            signal_time=moment,
            signal_price=100.0,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1000.0,
            close_location=0.50,
            close_location_threshold=0.70,
            filter_passed=False,
            signal_version="1.2.0",
            decision_time=moment,
            signal_candle_open_time=moment,
        )

        adapter = FailOnceAdapter(conn)
        repo = MERLongCLoOosRepository(adapter)

        # Error is raised while obtaining a cursor, before any DB write.
        try:
            first = repo.save_signal(**kwargs)
        except RuntimeError:
            first = None

        assert first is None or first.status == MERLongCLoOosSaveStatus.ERROR

        # The same frozen candidate can be delivered again.
        second = repo.save_signal(**kwargs)
        assert second.status == MERLongCLoOosSaveStatus.INSERTED

        third = repo.save_signal(**kwargs)
        assert third.status == MERLongCLoOosSaveStatus.DUPLICATE

        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM dds.me_r_long_close_location_oos_signal
            WHERE symbol = %s AND signal_version = '1.2.0'
              AND signal_time = %s
        """, (symbol, moment))

        assert cur.fetchone()[0] == 1

    finally:
        conn.rollback()
        conn.close()
