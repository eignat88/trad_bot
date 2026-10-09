"""Real PostgreSQL integration test. Local ME isolation only."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pg8000.dbapi

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)

HOST = "127.0.0.1"
PORT = 55432
DATABASE = "me_close_location_it"
USER = "me_test"
EXPECTED_DATA = "d:/py_pro/trad_bot/.me_pg_isolation/data"


def connect_isolated():
    conn = pg8000.dbapi.connect(
        host=HOST,
        port=PORT,
        database=DATABASE,
        user=USER,
        timeout=5,
    )

    cursor = conn.cursor()
    cursor.execute(
        "SELECT current_database(), current_user, "
        "current_setting('data_directory'), "
        "inet_server_port(), inet_server_addr()::text"
    )
    db, user, data, port, address = cursor.fetchone()

    assert db == DATABASE
    assert user == USER
    assert data.replace("\\", "/").lower() == EXPECTED_DATA
    assert port == PORT
    assert address.split("/")[0] == HOST

    conn.rollback()
    return conn



class NoCommitAdapter:
    """Keep repository writes inside the test transaction."""

    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        pass

    def rollback(self):
        raise RuntimeError(
            "Unexpected repository rollback in integration test"
        )


def test_me_repository_real_postgres():
    conn = connect_isolated()
    try:
        cursor = conn.cursor()
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '60s'")

        # Transactional application of migration 064.
        # No COMMIT from migration file is executed.
        migration = (
            Path("sql/migrations/064_me_r_close_location_versioned_uniqueness.sql")
            .read_text(encoding="utf-8")
        )
        body = migration.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0]
        cursor.execute(body)
    except Exception:
        conn.rollback()
        conn.close()
        raise

    repo = MERLongCLoOosRepository(NoCommitAdapter(conn))

    # Unique symbols avoid collisions with other test runs.
    suffix = uuid4().hex[:10].upper()
    clean_symbol = "MEC" + suffix
    legacy_symbol = "MEL" + suffix

    created_ids = []

    now = datetime.now(timezone.utc)
    candle_time = (
        now - timedelta(hours=6)
    ).replace(second=0, microsecond=0)

    candle_time -= timedelta(minutes=candle_time.minute % 5)
    decision_time = candle_time + timedelta(minutes=5, seconds=2)

    def save_symbol(symbol, version, decision):
        result = repo.save_signal(
            symbol=symbol,
            signal_time=candle_time,
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
            signal_version=version,
            decision_time=decision,
            signal_candle_open_time=(
                candle_time if decision else None
            ),
        )
        assert result.status == MERLongCLoOosSaveStatus.INSERTED
        created_ids.append(result.signal_id)
        return result.signal_id

    try:
        clean_id = save_symbol(
            clean_symbol, "1.2.0", decision_time
        )
        legacy_id = save_symbol(
            legacy_symbol, "1.0.0", None
        )

        # 1. Persisted signal provenance.
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT signal_version, decision_time,
                   signal_candle_open_time,
                   open, high, low, close, volume
            FROM dds.me_r_long_close_location_oos_signal
            WHERE signal_id = %s
            """,
            (clean_id,),
        )
        row = cursor.fetchone()

        assert row[0] == "1.2.0"
        assert row[1] == decision_time
        assert row[2] == candle_time
        assert [float(x) for x in row[3:]] == [
            101.0, 103.0, 99.0, 100.0, 1200.0
        ]

        # 2. Duplicate must not insert another signal.
        duplicate = repo.save_signal(
            symbol=clean_symbol,
            signal_time=candle_time,
            signal_price=100.0,
            open=101.0,
            high=103.0,
            low=99.0,
            close=100.0,
            volume=1200.0,
            close_location=0.25,
            close_location_threshold=0.70,
            filter_passed=False,
            signal_version="1.2.0",
            decision_time=decision_time,
            signal_candle_open_time=candle_time,
        )
        assert duplicate.status == MERLongCLoOosSaveStatus.DUPLICATE

        # 3. Pending queue: clean only.
        pending = repo.get_eligible_signals(limit=1000)
        pending_ids = {s["signal_id"] for s in pending}

        assert clean_id in pending_ids
        assert legacy_id not in pending_ids

        t15 = decision_time + timedelta(minutes=15)

        # 4. Save first horizon.
        assert repo.save_outcome_partial(
            signal_id=clean_id,
            symbol=clean_symbol,
            mfe_15m=3.0,
            mae_15m=1.0,
            evaluated_15m_at=t15,
            mfe_15m_r=1.2,
            mae_15m_r=0.4,
        ) is True

        # 5. Retry must not overwrite established values.
        assert repo.save_outcome_partial(
            signal_id=clean_id,
            symbol=clean_symbol,
            mfe_15m=999.0,
            mae_15m=999.0,
            mfe_15m_r=999.0,
            mae_15m_r=999.0,
            evaluated_15m_at=t15 + timedelta(minutes=1),
        ) is True

        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT mfe_15m, mae_15m, evaluated_15m_at
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (clean_id,),
        )
        mfe, mae, evaluated = cursor.fetchone()

        assert float(mfe) == 3.0
        assert float(mae) == 1.0
        assert evaluated == t15

        # 6. Finalize with all remaining horizons.
        kwargs = {}
        for horizon in (30, 60, 120, 240):
            kwargs[f"mfe_{horizon}m"] = 4.0
            kwargs[f"mae_{horizon}m"] = 1.0
            kwargs[f"mfe_{horizon}m_r"] = 1.6
            kwargs[f"mae_{horizon}m_r"] = 0.4
            kwargs[f"evaluated_{horizon}m_at"] = (
                decision_time + timedelta(minutes=horizon)
            )

        assert repo.save_outcome_partial(
            signal_id=clean_id,
            symbol=clean_symbol,
            hit_0_5r=True,
            hit_1r=True,
            hit_1_5r=True,
            hit_2r=False,
            is_final=True,
            **kwargs,
        ) is True

        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT is_final, evaluated_240m_at,
                   mfe_15m, mfe_240m
            FROM dds.me_r_long_close_location_oos_outcome
            WHERE signal_id = %s
            """,
            (clean_id,),
        )
        final, t240, mfe15, mfe240 = cursor.fetchone()

        assert final is True
        assert t240 is not None
        assert float(mfe15) == 3.0
        assert float(mfe240) == 4.0

        pending = repo.get_eligible_signals(limit=1000)
        assert clean_id not in {
            s["signal_id"] for s in pending
        }

    finally:
        # Roll back migration 064, signals and outcomes together.
        try:
            conn.rollback()
        finally:
            conn.close()
