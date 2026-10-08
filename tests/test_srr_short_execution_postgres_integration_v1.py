from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter
from tests.test_srr_short_execution_outcome_persistence_v1 import (
    EXP_ID,
    observation,
    final_result,
    nonfinal_result,
    make_activation_gate,
)

DB = "trad_bot_srr_persistence_it_20261008"
ENABLE = "SRR_PERSISTENCE_POSTGRES_IT"


@pytest.fixture
def connect():
    if os.getenv(ENABLE) != "1":
        pytest.skip(f"Set {ENABLE}=1 to run isolated PostgreSQL integration tests")

    psycopg = pytest.importorskip("psycopg")

    if os.getenv("TEST_DB_HOST", "127.0.0.1") != "127.0.0.1":
        pytest.fail("STOP: TEST_DB_HOST must be 127.0.0.1")
    if os.getenv("TEST_DB_NAME", DB) != DB:
        pytest.fail("STOP: unexpected TEST_DB_NAME")

    def make_connection():
        conn = psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname=DB,
            user="postgres",
            connect_timeout=5,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), inet_server_addr(), inet_server_port()"
            )
            name, address, port = cur.fetchone()

            if name != DB or str(address) != "127.0.0.1" or port != 5432:
                conn.close()
                raise RuntimeError("STOP: PostgreSQL safety boundary failed")

            cur.execute(
                "SELECT to_regclass("
                "'research.srr_short_execution_prospective_outcome')"
            )
            if cur.fetchone()[0] is None:
                conn.close()
                raise RuntimeError("STOP: migration 061 not installed")

        conn.commit()
        return conn

    return make_connection


def create_observation(connect):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research.prospective_experiment (experiment_id)
                VALUES (%s) ON CONFLICT DO NOTHING
                """,
                (EXP_ID,),
            )
            cur.execute(
                """
                INSERT INTO research.prospective_observation
                    (experiment_id, source_signal_id, symbol, direction,
                     signal_time, reference_price, features)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING observation_id
                """,
                (
                    EXP_ID,
                    uuid4().int % (2**62),
                    "AAAUSDT",
                    "SHORT",
                    observation()["signal_time"],
                    100,
                    observation()["features"],
                ),
            )
            observation_id = cur.fetchone()[0]
        conn.commit()
        return observation_id
    finally:
        conn.close()


def run_two_writers(connect, observation_id, result):
    barrier = threading.Barrier(2)

    def worker(_):
        conn = connect()
        try:
            barrier.wait(timeout=10)
            return SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
                observation_id=observation_id,
                experiment_id=EXP_ID,
                observation=observation(),
                result=result(),
            ).action
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(worker, (1, 2)))


def test_concurrent_insert_is_idempotent(connect):
    observation_id = create_observation(connect)

    actions = run_two_writers(connect, observation_id, nonfinal_result)
    assert actions == ["INSERTED", "REFRESHED"]

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), BOOL_AND(NOT is_final)
                FROM research.srr_short_execution_prospective_outcome
                WHERE observation_id = %s
                """,
                (observation_id,),
            )
            count, all_nonfinal = cur.fetchone()
            assert count == 1
            assert all_nonfinal is True
    finally:
        conn.close()


def test_concurrent_finalization_and_immutability(connect):
    psycopg = pytest.importorskip("psycopg")
    observation_id = create_observation(connect)

    conn = connect()
    try:
        initial = SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
            observation_id=observation_id,
            experiment_id=EXP_ID,
            observation=observation(),
            result=nonfinal_result(),
        )
        assert initial.action == "INSERTED"
    finally:
        conn.close()

    actions = run_two_writers(connect, observation_id, final_result)
    assert actions == ["ALREADY_FINALIZED", "FINALIZED"]

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), BOOL_AND(is_final), MIN(gross_r), MAX(gross_r)
                FROM research.srr_short_execution_prospective_outcome
                WHERE observation_id = %s
                """,
                (observation_id,),
            )
            count, is_final, min_gross, max_gross = cur.fetchone()

        assert count == 1
        assert is_final is True
        assert min_gross == max_gross

        conn.commit()

        with pytest.raises(psycopg.errors.RestrictViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research.srr_short_execution_prospective_outcome
                    SET status = 'MUTATED'
                    WHERE observation_id = %s
                    """,
                    (observation_id,),
                )
        conn.rollback()
    finally:
        conn.close()
