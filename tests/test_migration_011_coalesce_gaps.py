"""Integration tests for migration 011: coalesce candle coverage gaps."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pg8000
import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_011 = ROOT / "sql" / "migrations" / "011_coalesce_candle_coverage_gaps.sql"


def run_psql(sql_text: str) -> subprocess.CompletedProcess[str]:
    """Execute raw SQL against the test database."""
    psql = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")
    return subprocess.run(
        [
            str(psql),
            "-U", "postgres",
            "-h", "localhost",
            "-p", "5432",
            "-d", "trad_bot_migration_test",
            "-v", "ON_ERROR_STOP=1",
            "-c", sql_text,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def run_psql_file(path: Path) -> subprocess.CompletedProcess[str]:
    """Execute a SQL file against the test database."""
    psql = Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe")
    return subprocess.run(
        [
            str(psql),
            "-U", "postgres",
            "-h", "localhost",
            "-p", "5432",
            "-d", "trad_bot_migration_test",
            "-v", "ON_ERROR_STOP=1",
            "-f", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture(scope="module")
def db_conn():
    """Create a real pg8000 connection."""
    conn = pg8000.connect(
        host="localhost",
        port=5432,
        database="trad_bot_migration_test",
        user="postgres",
        password="",
    )
    yield conn
    conn.close()


class TestMigration011:
    """Tests for migration 011: coalesce candle coverage gaps."""

    def test_first_run_passes(self):
        """Test that migration 011 applies successfully."""
        result = run_psql_file(MIGRATION_011)
        assert result.returncode == 0, f"Migration 011 first run failed: {result.stderr}"

    def test_second_run_idempotent(self):
        """Test that migration 011 can be applied twice safely."""
        first = run_psql_file(MIGRATION_011)
        assert first.returncode == 0, f"First run failed: {first.stderr}"

        second = run_psql_file(MIGRATION_011)
        assert second.returncode == 0, f"Second run failed: {second.stderr}"

    def test_function_exists(self, db_conn):
        """Test that the function exists after migration."""
        cursor = db_conn.cursor()
        cursor.execute(
            """
            SELECT proname, proargtypes::regtype[]
            FROM pg_proc
            WHERE proname = 'check_candle_coverage'
              AND pronamespace = 'market'::regnamespace
            """
        )
        row = cursor.fetchone()
        assert row is not None, "Function market.check_candle_coverage not found"

    def test_contiguous_5m_missing_slots_return_one_gap(self, db_conn):
        """Test that contiguous 5m missing slots are returned as one gap."""
        cursor = db_conn.cursor()

        # Create a test instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('TESTGAP1', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            from datetime import datetime, timezone, timedelta

            # Insert a single candle at the start of the range
            candle_time = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            cursor.execute(
                """
                INSERT INTO market.candle (
                    exchange, market_type, instrument_id, timeframe,
                    open_time, close_time, open, high, low, close, volume,
                    is_closed, source, ingested_at, quality_status
                ) VALUES (
                    'bybit', 'linear', %s, '5',
                    %s, %s, 100.0, 105.0, 99.0, 103.0, 1000.0,
                    TRUE, 'test', NOW(), 'validated'
                )
                """,
                (instrument_id, candle_time, candle_time + timedelta(minutes=5)),
            )
            db_conn.commit()

            # Query coverage for a range with 1 candle present and 11 missing
            range_from = candle_time
            range_to = candle_time + timedelta(minutes=60)  # 12 slots, 1 present, 11 missing

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, range_from, range_to),
            )
            gaps = cursor.fetchall()

            # Should be 1 contiguous gap, not 11 individual gaps
            assert len(gaps) == 1
            # Gap should start after the first candle
            assert gaps[0][0] == candle_time + timedelta(minutes=5)
            # Gap should end at range_to
            assert gaps[0][1] == range_to

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_separated_gaps_stay_separate(self, db_conn):
        """Test that non-adjacent missing slots produce separate gaps."""
        cursor = db_conn.cursor()

        # Create a test instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('TESTGAP2', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            from datetime import datetime, timezone, timedelta

            # Insert candles at start and middle (creating two gaps)
            base_time = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            for i in [0, 30]:  # Two candles at 6:00 and 6:30
                candle_time = base_time + timedelta(minutes=i)
                cursor.execute(
                    """
                    INSERT INTO market.candle (
                        exchange, market_type, instrument_id, timeframe,
                        open_time, close_time, open, high, low, close, volume,
                        is_closed, source, ingested_at, quality_status
                    ) VALUES (
                        'bybit', 'linear', %s, '5',
                        %s, %s, 100.0, 105.0, 99.0, 103.0, 1000.0,
                        TRUE, 'test', NOW(), 'validated'
                    )
                    """,
                    (instrument_id, candle_time, candle_time + timedelta(minutes=5)),
                )
            db_conn.commit()

            # Query coverage for 1 hour range
            range_from = base_time
            range_to = base_time + timedelta(minutes=60)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, range_from, range_to),
            )
            gaps = cursor.fetchall()

            # Should be 2 separate gaps:
            # 1) 6:05 -> 6:30 (missing between first candle and second)
            # 2) 6:35 -> 6:00 next hour (missing after second candle)
            assert len(gaps) == 2

            # First gap: after first candle until before second candle
            assert gaps[0][0] == base_time + timedelta(minutes=5)
            assert gaps[0][1] == base_time + timedelta(minutes=30)

            # Second gap: after second candle until end of range
            assert gaps[1][0] == base_time + timedelta(minutes=35)
            assert gaps[1][1] == range_to

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_fully_covered_range_returns_no_gaps(self, db_conn):
        """Test that a fully covered range returns no gaps."""
        cursor = db_conn.cursor()

        # Create a test instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('TESTGAP3', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            from datetime import datetime, timezone, timedelta

            # Insert candles to fully cover a 30-minute range
            base_time = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            for i in range(6):  # 6 x 5m = 30 minutes
                candle_time = base_time + timedelta(minutes=i * 5)
                cursor.execute(
                    """
                    INSERT INTO market.candle (
                        exchange, market_type, instrument_id, timeframe,
                        open_time, close_time, open, high, low, close, volume,
                        is_closed, source, ingested_at, quality_status
                    ) VALUES (
                        'bybit', 'linear', %s, '5',
                        %s, %s, 100.0, 105.0, 99.0, 103.0, 1000.0,
                        TRUE, 'test', NOW(), 'validated'
                    )
                    """,
                    (instrument_id, candle_time, candle_time + timedelta(minutes=5)),
                )
            db_conn.commit()

            # Query coverage
            range_from = base_time
            range_to = base_time + timedelta(minutes=30)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, range_from, range_to),
            )
            gaps = cursor.fetchall()

            assert len(gaps) == 0

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_empty_range_returns_one_gap(self, db_conn):
        """Test that a completely empty range returns one coalesced gap."""
        cursor = db_conn.cursor()

        # Create a test instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('TESTGAP4', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            from datetime import datetime, timezone, timedelta

            base_time = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            range_to = base_time + timedelta(hours=2)  # 24 missing 5m slots

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, base_time, range_to),
            )
            gaps = cursor.fetchall()

            # Should be 1 gap covering the entire range
            assert len(gaps) == 1
            assert gaps[0][0] == base_time
            assert gaps[0][1] == range_to

        finally:
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()