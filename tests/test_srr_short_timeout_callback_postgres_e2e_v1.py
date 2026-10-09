from types import SimpleNamespace
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
from app.research.policies.srr_short_timeout_candle_policy_v1 import (
    CANDLE_MS,
    FROZEN_FREEZE_TS,
)
from tests.srr_postgres_test_safety import (
    SRR_TEST_ACTIVATION_TS,
    load_srr_postgres_test_config,
    seed_test_activation_record,
    validate_test_activation_seed,
    validate_server_identity,
)
from tests.test_srr_short_prospective_evaluator_policy_routing_v1 import (
    _ArchiveSource, _clean_candles, _obs, SIGNAL_MS,
)
from tests.test_srr_short_execution_outcome_persistence_v1 import make_activation_gate

CUTOFF = SIGNAL_MS + 120 * 60_000


def _post_activation_signal_ms():
    activation_ms = int(
        __import__("datetime").datetime.fromisoformat(
            SRR_TEST_ACTIVATION_TS.replace("Z", "+00:00")
        ).timestamp() * 1000
    )
    freeze_ms = int(
        __import__("datetime").datetime.fromisoformat(
            FROZEN_FREEZE_TS.replace("Z", "+00:00")
        ).timestamp() * 1000
    )
    first_signal_open_ms = (activation_ms + CANDLE_MS) // CANDLE_MS * CANDLE_MS
    signal_ms = first_signal_open_ms + CANDLE_MS
    cutoff_ms = signal_ms + 120 * 60_000
    if not signal_ms > activation_ms or not cutoff_ms > freeze_ms:
        raise AssertionError("post-activation synthetic timeline is inconsistent")
    return signal_ms, cutoff_ms


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


def _seed_timeout_activation_once():
    config = load_srr_postgres_test_config()
    signal_ms, _ = _post_activation_signal_ms()
    signal_time = datetime.fromtimestamp(signal_ms / 1000, timezone.utc)
    conn = pg8000.connect(
        host=config.host,
        port=config.port,
        database=config.database,
        user=config.user,
        timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_database(), current_setting('server_version'),
                       inet_server_addr(), inet_server_port()
            """)
            database, version, address, port = cur.fetchone()
            validate_server_identity(
                config,
                {
                    "current_database": database,
                    "server_version": version,
                    "inet_server_addr": address,
                    "inet_server_port": port,
                },
            )
            cur.execute("""
                SELECT to_regclass('research.srr_short_writer_activation')
            """)
            if cur.fetchone()[0] is None:
                raise RuntimeError("STOP: migration 062 not installed")
        seed_test_activation_record(
            conn,
            signal_times=(signal_time,),
            config=config,
        )
    finally:
        conn.close()


def connect():
    config = load_srr_postgres_test_config()
    conn = pg8000.connect(
        host=config.host,
        port=config.port,
        database=config.database,
        user=config.user,
        timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_database(), current_setting('server_version'),
                       inet_server_addr(), inet_server_port()
            """)
            database, version, address, port = cur.fetchone()
            validate_server_identity(
                config,
                {
                    "current_database": database,
                    "server_version": version,
                    "inet_server_addr": address,
                    "inet_server_port": port,
                },
            )
    except Exception:
        conn.close()
        raise
    return conn


def snapshot(conn):
    config = load_srr_postgres_test_config()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT current_database(), inet_server_port()
        """)
        database, port = cur.fetchone()
        assert database == config.database
        assert port == config.port

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
    _seed_timeout_activation_once()
    post_activation_signal_ms, post_activation_cutoff_ms = _post_activation_signal_ms()
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

        config = load_srr_postgres_test_config()
        with writer_conn.cursor() as cur:
            cur.execute("""
                SELECT current_database(), inet_server_port()
            """)
            database, port = cur.fetchone()
            assert database == config.database
            assert port == config.port
        writer_conn.rollback()

        obs = _obs(
            observation_id=observation_id,
            signal_time=datetime.fromtimestamp(
                post_activation_signal_ms / 1000, timezone.utc
            ),
        )
        validate_test_activation_seed(
            SRR_TEST_ACTIVATION_TS, (obs["signal_time"],)
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

                # Align synthetic candle timestamps with the post-activation signal.
        # Preserve the original OHLC values and frozen execution semantics.
        original_candles = _clean_candles()
        original_first_open_ms = int(original_candles[0].timestamp)
        shift_ms = post_activation_signal_ms - original_first_open_ms

        post_activation_candles = [
            SimpleNamespace(
                **{
                    **candle.__dict__,
                    "timestamp": int(candle.timestamp) + shift_ms,
                }
            )
            for candle in original_candles
        ]

        actual_opens = [int(c.timestamp) for c in post_activation_candles]
        expected_opens = list(
            range(post_activation_signal_ms, post_activation_cutoff_ms, CANDLE_MS)
        )
        assert actual_opens == expected_opens
        assert len(actual_opens) == 24

        candle_source = _ArchiveSource(post_activation_candles)
        evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
            conn=ScopedConnection(reader, observation_id),
            candle_source=candle_source,
            dry_run=False,
            write_outcomes=SrrOutcomeWriter(
                writer_conn, activation_gate=make_activation_gate()
            ).write,
        )

        assert post_activation_signal_ms > int(
            datetime.fromisoformat(
                SRR_TEST_ACTIVATION_TS.replace("Z", "+00:00")
            ).timestamp() * 1000
        )
        for asof, expected_action, expected_final in (
            (post_activation_cutoff_ms - 1000, "INSERTED", False),
            (post_activation_cutoff_ms, "FINALIZED", True),
        ):
            evaluator._current_asof_ms = lambda value=asof: value
            stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

            expected_source_end_ms = (
                post_activation_cutoff_ms - CANDLE_MS
            )
            assert candle_source.requests[-1:] == [
                (
                    obs["symbol"],
                    post_activation_signal_ms // CANDLE_MS * CANDLE_MS,
                    expected_source_end_ms,
                    asof,
                )
            ]
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
                assert asof == post_activation_cutoff_ms
                assert abs(float(row[2]) + 0.21) < 1e-9
            else:
                assert asof < post_activation_cutoff_ms
                assert row[1] is None
                assert row[2] is None

            writer_conn.rollback()

        evaluator._current_asof_ms = lambda: post_activation_cutoff_ms + 1000
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
