"""Integration tests for migration 012 and timeframe-grid alignment."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pg8000
import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_012 = ROOT / "sql" / "migrations" / "012_align_candle_coverage_grid.sql"


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


class TestMigration012:
    """Tests for migration 012: align candle coverage grid."""

    def test_first_run_passes(self):
        """Test that migration 012 applies successfully."""
        result = run_psql_file(MIGRATION_012)
        assert result.returncode == 0, f"Migration 012 first run failed: {result.stderr}"

    def test_second_run_idempotent(self):
        """Test that migration 012 can be applied twice safely."""
        first = run_psql_file(MIGRATION_012)
        assert first.returncode == 0, f"First run failed: {first.stderr}"

        second = run_psql_file(MIGRATION_012)
        assert second.returncode == 0, f"Second run failed: {second.stderr}"

    def test_function_exists(self, db_conn):
        """Test that the function exists after migration."""
        cursor = db_conn.cursor()
        cursor.execute(
            """
            SELECT proname
            FROM pg_proc
            WHERE proname = 'check_candle_coverage'
              AND pronamespace = 'market'::regnamespace
            """
        )
        row = cursor.fetchone()
        assert row is not None, "Function market.check_candle_coverage not found"


class TestGridAlignment:
    """Tests for Python-side timeframe-grid alignment."""

    def test_align_5m_down(self):
        """Test 5m alignment down to :00/:05/:10 etc."""
        from app.analytics.candle_sync import align_to_grid

        # 11:12:15 -> 11:10:00
        dt = datetime(2026, 9, 12, 11, 12, 15, tzinfo=timezone.utc)
        result = align_to_grid(dt, "5")
        assert result == datetime(2026, 9, 12, 11, 10, 0, tzinfo=timezone.utc)

    def test_align_5m_already_aligned(self):
        """Test 5m alignment when already on boundary."""
        from app.analytics.candle_sync import align_to_grid

        dt = datetime(2026, 9, 12, 11, 10, 0, tzinfo=timezone.utc)
        result = align_to_grid(dt, "5")
        assert result == dt

    def test_align_15m_down(self):
        """Test 15m alignment down to :00/:15/:30/:45."""
        from app.analytics.candle_sync import align_to_grid

        # 11:22:00 -> 11:15:00
        dt = datetime(2026, 9, 12, 11, 22, 0, tzinfo=timezone.utc)
        result = align_to_grid(dt, "15")
        assert result == datetime(2026, 9, 12, 11, 15, 0, tzinfo=timezone.utc)

    def test_align_1h_down(self):
        """Test 1h alignment down to :00."""
        from app.analytics.candle_sync import align_to_grid

        # 11:22:00 -> 11:00:00
        dt = datetime(2026, 9, 12, 11, 22, 0, tzinfo=timezone.utc)
        result = align_to_grid(dt, "60")
        assert result == datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc)

    def test_align_daily_to_midnight(self):
        """Test daily alignment to midnight UTC."""
        from app.analytics.candle_sync import align_to_grid

        dt = datetime(2026, 9, 12, 15, 30, 0, tzinfo=timezone.utc)
        result = align_to_grid(dt, "D")
        assert result == datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)

    def test_boundary_exact_unchanged(self):
        """Test that boundary-exact timestamps are unchanged."""
        from app.analytics.candle_sync import align_to_grid

        dt = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc)
        assert align_to_grid(dt, "1") == dt
        assert align_to_grid(dt, "5") == dt
        assert align_to_grid(dt, "15") == dt
        assert align_to_grid(dt, "60") == dt

    def test_alignment_with_microseconds(self):
        """Test alignment strips microseconds."""
        from app.analytics.candle_sync import align_to_grid

        dt = datetime(2026, 9, 12, 11, 12, 15, 976987, tzinfo=timezone.utc)
        result = align_to_grid(dt, "5")
        assert result.microsecond == 0
        assert result == datetime(2026, 9, 12, 11, 10, 0, tzinfo=timezone.utc)


class TestGridAlignedCoverage:
    """Tests that existing aligned candles are recognized as coverage."""

    def test_aligned_candles_no_gaps(self, db_conn):
        """Test that existing 5m candles on :00/:05 boundaries with
        p_from containing seconds/microseconds produce NO gaps."""
        cursor = db_conn.cursor()
        now = datetime.now(timezone.utc)

        # Create instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('GRIDTEST1', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            # Insert 12 candles on 5m boundaries
            base = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            for i in range(12):
                candle_from = base + timedelta(minutes=i * 5)
                candle_to = candle_from + timedelta(minutes=5)
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
                    (instrument_id, candle_from, candle_to),
                )
            db_conn.commit()

            # Query with p_from containing arbitrary seconds/microseconds
            # The grid alignment should match the existing candles
            p_from = datetime(2026, 9, 12, 6, 12, 15, 976987, tzinfo=timezone.utc)
            p_to = datetime(2026, 9, 12, 6, 55, 30, 123456, tzinfo=timezone.utc)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, p_from, p_to),
            )
            gaps = cursor.fetchall()

            # Should be NO gaps because aligned candles cover the range
            assert len(gaps) == 0

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_partial_aligned_coverage(self, db_conn):
        """Test partial coverage with aligned candles produces only real gaps."""
        cursor = db_conn.cursor()

        # Create instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('GRIDTEST2', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            base = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            # Insert candles for first 30 min (6 slots)
            for i in range(6):
                candle_from = base + timedelta(minutes=i * 5)
                candle_to = candle_from + timedelta(minutes=5)
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
                    (instrument_id, candle_from, candle_to),
                )
            db_conn.commit()

            # Query for 1 hour range with offset
            p_from = datetime(2026, 9, 12, 6, 10, 0, tzinfo=timezone.utc)
            p_to = datetime(2026, 9, 12, 7, 10, 0, tzinfo=timezone.utc)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, p_from, p_to),
            )
            gaps = cursor.fetchall()

            # Should be 1 gap from 6:30 to 7:10 (6 missing slots)
            assert len(gaps) == 1
            assert gaps[0][0] == datetime(2026, 9, 12, 6, 30, tzinfo=timezone.utc)
            assert gaps[0][1] == datetime(2026, 9, 12, 7, 10, tzinfo=timezone.utc)

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_empty_range_with_offset(self, db_conn):
        """Test empty range with offset timestamps produces one coalesced gap."""
        cursor = db_conn.cursor()

        # Create instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('GRIDTEST3', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            # Query with offset timestamps — no candles exist
            p_from = datetime(2026, 9, 12, 6, 12, 15, tzinfo=timezone.utc)
            p_to = datetime(2026, 9, 12, 8, 12, 30, tzinfo=timezone.utc)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '5', %s, %s)",
                (instrument_id, p_from, p_to),
            )
            gaps = cursor.fetchall()

            # Should be 1 coalesced gap
            assert len(gaps) == 1
            # Gap starts aligned down from p_from
            assert gaps[0][0] == datetime(2026, 9, 12, 6, 10, tzinfo=timezone.utc)

        finally:
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_15m_alignment(self, db_conn):
        """Test 15m alignment with offset timestamps."""
        cursor = db_conn.cursor()

        # Create instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('GRIDTEST4', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            base = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            # Insert 15m candles for 1 hour (4 candles)
            for i in range(4):
                candle_from = base + timedelta(minutes=i * 15)
                candle_to = candle_from + timedelta(minutes=15)
                cursor.execute(
                    """
                    INSERT INTO market.candle (
                        exchange, market_type, instrument_id, timeframe,
                        open_time, close_time, open, high, low, close, volume,
                        is_closed, source, ingested_at, quality_status
                    ) VALUES (
                        'bybit', 'linear', %s, '15',
                        %s, %s, 100.0, 105.0, 99.0, 103.0, 1000.0,
                        TRUE, 'test', NOW(), 'validated'
                    )
                    """,
                    (instrument_id, candle_from, candle_to),
                )
            db_conn.commit()

            # Query with offset
            p_from = datetime(2026, 9, 12, 6, 22, 0, tzinfo=timezone.utc)
            p_to = datetime(2026, 9, 12, 7, 0, 0, tzinfo=timezone.utc)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '15', %s, %s)",
                (instrument_id, p_from, p_to),
            )
            gaps = cursor.fetchall()

            # Alignment: 6:22 -> 6:15, 7:00 -> 7:00
            # Candles at 6:00, 6:15, 6:30, 6:45 cover 6:15-7:00
            # So NO gaps — all aligned slots are covered
            assert len(gaps) == 0

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_60m_alignment(self, db_conn):
        """Test 1h alignment with offset timestamps."""
        cursor = db_conn.cursor()

        # Create instrument
        cursor.execute(
            "INSERT INTO dds.instrument (symbol, base_asset, quote_asset, category, status) "
            "VALUES ('GRIDTEST5', 'TEST', 'USDT', 'linear', 'Trading') RETURNING instrument_id"
        )
        instrument_id = cursor.fetchone()[0]
        db_conn.commit()

        try:
            base = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
            # Insert hourly candles for 3 hours
            for i in range(3):
                candle_from = base + timedelta(hours=i)
                candle_to = candle_from + timedelta(hours=1)
                cursor.execute(
                    """
                    INSERT INTO market.candle (
                        exchange, market_type, instrument_id, timeframe,
                        open_time, close_time, open, high, low, close, volume,
                        is_closed, source, ingested_at, quality_status
                    ) VALUES (
                        'bybit', 'linear', %s, '60',
                        %s, %s, 100.0, 105.0, 99.0, 103.0, 1000.0,
                        TRUE, 'test', NOW(), 'validated'
                    )
                    """,
                    (instrument_id, candle_from, candle_to),
                )
            db_conn.commit()

            # Query with offset
            p_from = datetime(2026, 9, 12, 6, 22, 0, tzinfo=timezone.utc)
            p_to = datetime(2026, 9, 12, 9, 30, 0, tzinfo=timezone.utc)

            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, '60', %s, %s)",
                (instrument_id, p_from, p_to),
            )
            gaps = cursor.fetchall()

            # 6:00 covers 6:22, 7:00 covers 7:xx, 8:00 covers 8:xx
            # Gap: 9:00 to 10:00 (1 missing slot)
            assert len(gaps) == 1
            assert gaps[0][0] == datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
            assert gaps[0][1] == datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

        finally:
            cursor.execute("DELETE FROM market.candle WHERE instrument_id = %s", (instrument_id,))
            cursor.execute("DELETE FROM dds.instrument WHERE instrument_id = %s", (instrument_id,))
            db_conn.commit()

    def test_second_reconciliation_zero_updates(self):
        """Test that reconciling an already-complete range produces zero updates."""
        from unittest.mock import MagicMock
        from app.analytics.candle_sync import CandleSync
        from app.analytics.models import CandleRange
        from datetime import timedelta

        mock_bybit = MagicMock()
        mock_repo = MagicMock()

        # Simulate: all gaps already covered
        mock_repo.get_candle_coverage.return_value = []

        sync = CandleSync(mock_bybit, mock_repo)

        ranges = [
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            )
        ]

        gaps, inserted, updated, rejected, failed = sync.reconcile_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            required_ranges=ranges,
        )

        assert len(gaps) == 0
        assert inserted == 0
        assert updated == 0
        assert rejected == 0
        assert failed == []
        # Bybit should never be called
        mock_bybit._public_get.assert_not_called()