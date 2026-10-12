"""Versioned ME signal/outcome persistence on isolated PostgreSQL."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pg8000.dbapi

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)

from test_me_r_close_location_postgres_integration import connect_isolated


class NoCommitAdapter:
    def __init__(self, connection):
        self.connection = connection
        self.commit_calls = 0

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        return self.connection.rollback()


def test_versioned_signal_and_outcome_isolation():
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

        adapter = NoCommitAdapter(conn)
        repo = MERLongCLoOosRepository(adapter)

        symbol = "MEV" + uuid4().hex[:10].upper()
        signal_time = datetime(
            2026, 10, 9, 12, 0, tzinfo=timezone.utc
        )

        ids = {}

        for version in ("1.0.0", "1.2.0"):
            kwargs = dict(
                symbol=symbol,
                signal_time=signal_time,
                signal_price=100.0,
                open=100.0,
                high=102.0,
                low=98.0,
                close=101.0,
                volume=1000.0,
                close_location=0.75,
                close_location_threshold=0.70,
                filter_passed=True,
                signal_version=version,
            )

            if version == "1.2.0":
                kwargs["decision_time"] = signal_time
                kwargs["signal_candle_open_time"] = signal_time

            inserted = repo.save_signal(**kwargs)

            assert inserted.status == MERLongCLoOosSaveStatus.INSERTED
            ids[version] = inserted.signal_id

            assert repo.signal_exists(
                symbol, signal_time, signal_version=version
            )

            duplicate = repo.save_signal(**kwargs)

            assert duplicate.status == MERLongCLoOosSaveStatus.DUPLICATE

        assert ids["1.0.0"] != ids["1.2.0"]

        expected = {
            "1.0.0": Decimal("1.5"),
            "1.2.0": Decimal("3.5"),
        }

        for version, mfe in expected.items():
            assert repo.save_outcome_partial(
                signal_id=ids[version],
                symbol=symbol,
                mfe_15m=float(mfe),
                mae_15m=0.5,
                mfe_15m_r=float(mfe / Decimal("2.5")),
                mae_15m_r=0.2,
                evaluated_15m_at=signal_time + timedelta(minutes=15),
            )

        cur.execute("""
            SELECT s.signal_version, s.signal_id,
                   o.signal_id, o.mfe_15m
            FROM dds.me_r_long_close_location_oos_signal s
            JOIN dds.me_r_long_close_location_oos_outcome o
              ON o.signal_id = s.signal_id
            WHERE s.symbol = %s AND s.signal_time = %s
            ORDER BY s.signal_version
        """, (symbol, signal_time))

        rows = cur.fetchall()

        assert len(rows) == 2

        for version, signal_id, outcome_id, mfe in rows:
            assert signal_id == ids[version]
            assert outcome_id == signal_id
            assert mfe == expected[version]

        assert adapter.commit_calls == 6

    finally:
        conn.rollback()
        conn.close()
