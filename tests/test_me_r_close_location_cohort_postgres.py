"""Clean cohort accounting against isolated PostgreSQL."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)
from test_me_r_close_location_postgres_integration import connect_isolated


class NoCommitAdapter:
    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        pass

    def rollback(self):
        return self.connection.rollback()


def test_clean_cohort_pass_reject_invalid_postgres():
    conn = connect_isolated()

    try:
        cur = conn.cursor()

        # Apply test-only uniqueness transition inside transaction.
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

        migration_065 = Path(
            "sql/migrations/065_me_r_close_location_clean_cohort_views.sql"
        ).read_text(encoding="utf-8")

        body_065 = migration_065.split(
            "BEGIN;", 1
        )[1].rsplit("COMMIT;", 1)[0]

        cur.execute(body_065)

        repo = MERLongCLoOosRepository(NoCommitAdapter(conn))
        moment = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        prefix = "MEC" + uuid4().hex[:8].upper()

        cases = (
            ("PASS", 0.85, True, 101.0, 99.0, 100.70),
            ("REJECT", 0.30, False, 101.0, 99.0, 99.60),
            ("INVALID", None, False, 100.0, 100.0, 100.0),
        )

        created_ids = []

        for label, close_location, passed, high, low, close in cases:
            result = repo.save_signal(
                symbol=prefix + label,
                signal_time=moment,
                signal_price=100.0,
                open=100.0,
                high=high,
                low=low,
                close=close,
                volume=1000.0,
                close_location=close_location,
                close_location_threshold=0.70,
                filter_passed=passed,
                signal_version="1.2.0",
                decision_time=moment.replace(minute=5),
                signal_candle_open_time=moment,
            )

            assert result.status == MERLongCLoOosSaveStatus.INSERTED
            created_ids.append(result.signal_id)

        # Inspect classifications for only the three test records.
        cur.execute("""
            SELECT
                CASE
                    WHEN close_location IS NULL
                      OR close_location NOT BETWEEN 0 AND 1
                      OR high <= low
                      OR open NOT BETWEEN low AND high
                      OR close NOT BETWEEN low AND high
                      OR filter_passed <>
                         (close_location >= close_location_threshold)
                    THEN 'INVALID'
                    WHEN filter_passed THEN 'PASS'
                    ELSE 'REJECT'
                END AS classification
            FROM dds.me_r_long_close_location_oos_signal
            WHERE signal_id IN (%s, %s, %s)
        """, tuple(created_ids))

        classes = sorted(row[0] for row in cur.fetchall())

        assert classes == ["INVALID", "PASS", "REJECT"], classes

        stats = repo.get_cohort_stats()[0]

        assert stats["cohort_integrity"] is True
        assert stats["pass_count"] >= 1
        assert stats["reject_count"] >= 1
        assert stats["invalid_count"] >= 1

    finally:
        conn.rollback()
        conn.close()
