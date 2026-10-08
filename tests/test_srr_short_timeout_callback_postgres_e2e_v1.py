import json
import uuid
from datetime import datetime, timezone

import pg8000
import pytest

from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
    SRR_EXPERIMENT_ID,
)
from tests.test_srr_short_prospective_evaluator_policy_routing_v1 import (
    _ArchiveSource, _clean_candles, _obs, SIGNAL_MS,
)

DB = "trad_bot_srr_persistence_it_20261008"
CUTOFF = SIGNAL_MS + 120 * 60_000


class ScopedCursor:
    def __init__(self, cursor, observation_id):
        self._cursor = cursor
        self._observation_id = observation_id

    def execute(self, sql, params=None):
        if "FROM research.prospective_observation o" in sql:
            assert "ORDER BY o.observation_id" in sql
            assert "research.srr_short_execution_prospective_outcome" in sql

            sql = sql.replace(
                "ORDER BY o.observation_id",
                "AND o.observation_id = %s ORDER BY o.observation_id",
                1,
            )
            params = tuple(params) + (self._observation_id,)

        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def close(self):
        self._cursor.close()


class ScopedConnection:
    def __init__(self, conn, observation_id):
        self._conn = conn
        self._observation_id = observation_id

    def cursor(self):
        return ScopedCursor(
            self._conn.cursor(), self._observation_id
        )


def connect():
    return pg8000.connect(
        host="127.0.0.1",
        port=5432,
        database=DB,
        user="postgres",
        timeout=5,
    )


def snapshot(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT current_database(), inet_server_port()
        """)
        assert tuple(cur.fetchone()) == (DB, 5432)

        cur.execute("""
            SELECT started_at
            FROM research.prospective_experiment
            WHERE experiment_id = %s
        """, (SRR_EXPERIMENT_ID,))
        started_at = cur.fetchone()[0]

        cur.execute("""
            SELECT observation_id
            FROM research.prospective_observation
            WHERE experiment_id = %s
            ORDER BY observation_id
        """, (SRR_EXPERIMENT_ID,))
        observations = tuple(row[0] for row in cur.fetchall())

        cur.execute("""
            SELECT observation_id, is_final, status,
                   reason_code, updated_at
            FROM research.srr_short_execution_prospective_outcome
            WHERE experiment_id = %s
            ORDER BY observation_id
        """, (SRR_EXPERIMENT_ID,))
        outcomes = tuple(tuple(row) for row in cur.fetchall())

    return started_at, observations, outcomes


@pytest.mark.skipif(
    __import__("os").environ.get("SRR_PERSISTENCE_POSTGRES_IT") != "1",
    reason="Explicit local PostgreSQL opt-in required",
)
def test_timeout_callback_nonfinal_final_excluded():
    observation_id = -(
        8_000_000_000_000 + uuid.uuid4().int % 1_000_000_000
    )

    reader = connect()
    writer_conn = connect()
    inserted = False
    baseline = None

    try:
        baseline = snapshot(reader)
        reader.rollback()
        assert baseline[0] is None

        with writer_conn.cursor() as cur:
            cur.execute("""
                SELECT current_database(), inet_server_port()
            """)
            assert tuple(cur.fetchone()) == (DB, 5432)
        writer_conn.rollback()

        obs = _obs(observation_id=observation_id)
        obs["signal_time"] = datetime.fromtimestamp(
            SIGNAL_MS / 1000, timezone.utc
        )

        with writer_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO research.prospective_observation (
                    observation_id, experiment_id, source_signal_id,
                    symbol, direction, signal_time,
                    reference_price, invalidation_price,
                    variant_entry, variant_stop, variant_target,
                    features
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, CAST(%s AS jsonb)
                )
            """, (
                observation_id, SRR_EXPERIMENT_ID, observation_id,
                obs["symbol"], obs["direction"], obs["signal_time"],
                obs["reference_price"], obs["invalidation_price"],
                obs["variant_entry"], obs["variant_stop"],
                obs["variant_target"],
                json.dumps(json.loads(obs["features"])),
            ))
        writer_conn.commit()
        inserted = True

        # The experiment timestamp is visible only to reader.
        # Its transaction will be rolled back after this test.
        with reader.cursor() as cur:
            cur.execute("""
                UPDATE research.prospective_experiment
                SET started_at = %s
                WHERE experiment_id = %s
                  AND started_at IS NULL
            """, (obs["signal_time"], SRR_EXPERIMENT_ID))
            assert cur.rowcount == 1

        evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
            conn=ScopedConnection(reader, observation_id),
            candle_source=_ArchiveSource(_clean_candles()),
            dry_run=False,
            write_outcomes=SrrOutcomeWriter(writer_conn).write,
        )

        for asof, expected_action, expected_final in (
            (CUTOFF - 1000, "INSERTED", False),
            (CUTOFF, "FINALIZED", True),
        ):
            evaluator._current_asof_ms = lambda value=asof: value
            stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

            assert stats["errors"] == 0, stats
            assert stats["signals_checked"] == 1, stats

            record = stats["observations"][0]
            assert record["observation_id"] == observation_id
            assert record["write_action"] == expected_action
            assert (
                record["persistence_finalization_eligible"]
                is expected_final
            )

            with writer_conn.cursor() as cur:
                cur.execute("""
                    SELECT is_final, gross_r, net_r_normal
                    FROM research.srr_short_execution_prospective_outcome
                    WHERE observation_id = %s
                """, (observation_id,))
                row = cur.fetchone()

            assert row[0] is expected_final
            if expected_final:
                assert abs(float(row[2]) + 0.21) < 1e-9
            else:
                assert row[1] is None
                assert row[2] is None

            writer_conn.rollback()

        evaluator._current_asof_ms = lambda: CUTOFF + 1000
        final_cycle = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)
        assert final_cycle["signals_checked"] == 0, final_cycle
        assert final_cycle["errors"] == 0, final_cycle

        print("SRR_CALLBACK_NONFINAL_FINAL_EXCLUDED_PASS")

    finally:
        reader.rollback()
        writer_conn.rollback()

        try:
            if inserted:
                with writer_conn.cursor() as cur:
                    cur.execute("""
                        DELETE FROM research.prospective_observation
                        WHERE observation_id = %s
                          AND experiment_id = %s
                    """, (observation_id, SRR_EXPERIMENT_ID))
                    assert cur.rowcount == 1
                writer_conn.commit()

            if baseline is not None:
                after = snapshot(reader)
                reader.rollback()
                assert after == baseline
                print("SRR_CALLBACK_BASELINE_UNCHANGED_PASS")
        finally:
            reader.rollback()
            writer_conn.rollback()
            reader.close()
            writer_conn.close()
