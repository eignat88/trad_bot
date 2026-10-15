"""Issue #152 — TEST_ONLY real PostgreSQL lifecycle integration test.

Uses only trad_bot_runner_152_it_pg_test via explicit local peer connection.
Requires an empty disposable V3 database. Committed/finalized rows are
intentionally not deleted: database teardown is an external TEST_ONLY
administrative operation.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.research.me_r_long_early_mae_v3 import protocol, repository
from app.research.me_r_long_early_mae_v3.evaluator import MeEarlyMaeV3Evaluator
from app.research.me_r_long_early_mae_v3.runner import (
    require_isolated_os_user,
    require_isolated_schema,
)


SCHEMA = protocol.SOURCE_SCHEMA
VARIANT_COUNT = 7
FROZEN = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
ACTIVATION = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
INSTRUMENT_ID = 9152001


def _opted_in() -> bool:
    return os.environ.get("ME_EARLY_MAE_V3_PG_INTEGRATION") == "true"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _source(setup_id: str) -> dict:
    close = datetime(2026, 10, 19, 12, 5, tzinfo=timezone.utc)
    available = close + timedelta(seconds=1)
    detected = close + timedelta(seconds=2)
    return {
        "source_setup_id": setup_id,
        "instrument_id": INSTRUMENT_ID,
        "symbol": "ISSUE152TEST",
        "source_candle_open_at": close - timedelta(minutes=5),
        "source_candle_close_at": close,
        "source_fetch_started_at": close,
        "source_available_at": available,
        "source_fetch_completed_at": available,
        "detected_at": detected,
        "entry_eligible_at": max(available, detected, ACTIVATION),
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
        "reference_price": 100.0,
        "stop_price": 97.5,
        "target_price": 103.0,
        "risk_distance": 2.5,
    }


def _insert_test_fixtures(conn) -> None:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM dds.instrument WHERE instrument_id = %s", (INSTRUMENT_ID,))
    if cur.fetchone() is None:
        cur.execute(
            """INSERT INTO dds.instrument
               (instrument_id, symbol)
               VALUES (%s, %s)""",
            (INSTRUMENT_ID, "ISSUE152TEST"),
        )
    entry = datetime(2026, 10, 19, 12, 10, tzinfo=timezone.utc)
    # Exactly 48 contiguous closed 5m candles for a 240-minute baseline.
    for offset in range(48):
        opened = entry + timedelta(minutes=5 * offset)
        closed = opened + timedelta(minutes=5)
        cur.execute(
            """INSERT INTO market.candle
               (exchange, market_type, instrument_id, timeframe, open_time,
                close_time, open, high, low, close, volume, turnover,
                is_closed, source, source_received_at, ingested_at, quality_status)
               VALUES ('TEST','PERPETUAL',%s,'5',%s,%s,100,100,100,100,1,1,
                       TRUE,'ISSUE152_TEST_ONLY',%s,%s,'validated')""",
            (INSTRUMENT_ID, opened, closed, closed, closed),
        )
    conn.commit()


@pytest.mark.skipif(not _opted_in(), reason="ME_EARLY_MAE_V3_PG_INTEGRATION=true required")
def test_isolated_postgres_real_lifecycle():
    require_isolated_os_user()
    import pg8000.dbapi
    conn = pg8000.dbapi.connect(
        user="trad_bot_runner_152_it",
        database="trad_bot_runner_152_it_pg_test",
        unix_sock="/var/run/postgresql/.s.PGSQL.5432",
    )
    cur = conn.cursor()
    cur.execute("SELECT current_database(), current_user, inet_client_addr() IS NULL")
    assert tuple(cur.fetchone()) == (
        "trad_bot_runner_152_it_pg_test",
        "trad_bot_runner_152_it",
        True,
    )
    conn.rollback()
    try:
        require_isolated_schema(conn)

        # ISSUE152: never reuse an existing finalized test source.
        cur = conn.cursor()
        cur.execute(
            "SELECT current_database(), current_user"
        )
        db_name, db_user = cur.fetchone()

        if (
            db_name != "trad_bot_runner_152_it_pg_test"
            or db_user != "trad_bot_runner_152_it"
        ):
            raise RuntimeError(
                "ISSUE152_E2E_DATABASE_IDENTITY_MISMATCH"
            )

        cur.execute(
            f"SELECT source_event_id, source_status "
            f"FROM {SCHEMA}.source_event "
            f"WHERE source_setup_id = %s",
            ("ISSUE152-FINAL",),
        )
        existing = cur.fetchone()
        conn.rollback()

        if existing is not None:
            raise RuntimeError(
                f"ISSUE152_E2E_DIRTY_DATABASE:"
                f"source_id={existing[0]},status={existing[1]}"
            )

        # Fail closed: never run against previously used test datasets.
        cur = conn.cursor()
        cur.execute(
            f"""SELECT
                (SELECT count(*) FROM {SCHEMA}.protocol),
                (SELECT count(*) FROM {SCHEMA}.variant),
                (SELECT count(*) FROM {SCHEMA}.source_event),
                (SELECT count(*) FROM {SCHEMA}.variant_outcome),
                (SELECT count(*) FROM {SCHEMA}.pipeline_run),
                (SELECT count(*) FROM dds.instrument WHERE instrument_id = %s),
                (SELECT count(*) FROM market.candle WHERE source = %s)
            """,
            (INSTRUMENT_ID, "ISSUE152_TEST_ONLY"),
        )
        counts = tuple(cur.fetchone())
        conn.rollback()
        if counts != (0, 0, 0, 0, 0, 0, 0):
            raise RuntimeError(
                f"ISSUE152_E2E_DISPOSABLE_DATABASE_NOT_EMPTY:{counts}"
            )

        repo = repository.MeEarlyMaeV3Repository(conn)
        actual = protocol.calculate_protocol_fingerprint(protocol.protocol_payload(FROZEN, ACTIVATION))
        with patch.object(repository, "FROZEN_AT", FROZEN), patch.object(
            repository, "ACTIVATION_AT", ACTIVATION), patch.object(
            protocol, "APPROVED_FINGERPRINT", actual), patch.object(
            repository, "validate_freeze_activation", return_value=None
        ), patch.object(repository, "require_protocol_fingerprint", return_value=actual), patch.object(
            protocol, "validate_freeze_activation", return_value=None
        ):
            _insert_test_fixtures(conn)
            repo.register_protocol()
            repo.register_protocol()
            pre = _source("ISSUE152-PRE")
            pre.update({
                "source_candle_close_at": ACTIVATION,
                "source_fetch_started_at": ACTIVATION,
                "source_available_at": ACTIVATION,
                "source_fetch_completed_at": ACTIVATION,
                "detected_at": ACTIVATION,
            })
            with pytest.raises(ValueError, match="PRE_ACTIVATION"):
                repo.insert_source_with_variants(pre)

            missing = _source("ISSUE152-MISSING")
            del missing["source_available_at"]
            with pytest.raises(KeyError):
                repo.insert_source_with_variants(missing)

            source_id = repo.insert_source_with_variants(_source("ISSUE152-FINAL"))
            assert source_id > 0

            repo.update_source_entry(source_id, {
                "entry_filled_at": datetime(2026, 10, 19, 12, 10, tzinfo=timezone.utc),
                "entry_price": 100.0,
                "stop_price": 97.5,
                "target_price": 103.0,
                "risk_distance": 2.5,
                "baseline_deadline_at": datetime(2026, 10, 19, 16, 10, tzinfo=timezone.utc),
            })

            evaluator = MeEarlyMaeV3Evaluator(repo)
            stats = evaluator.run_cycle(datetime(2026, 10, 19, 16, 10, 1, tzinfo=timezone.utc))
            assert stats["errors"] == 0
            assert stats["fills"] == 0
            assert stats["variants"] == VARIANT_COUNT

            cur = conn.cursor()
            cur.execute(
                f"SELECT source_status, coverage_status FROM {SCHEMA}.source_event "
                f"WHERE source_event_id=%s", (source_id,)
            )
            assert tuple(cur.fetchone()) == ("FINALIZED", "COMPLETE")
            cur.execute(
                f"SELECT count(*), count(*) FILTER (WHERE variant_status='FINALIZED') "
                f"FROM {SCHEMA}.variant_outcome WHERE source_event_id=%s", (source_id,)
            )
            assert tuple(cur.fetchone()) == (VARIANT_COUNT, VARIANT_COUNT)

            cur.execute(
                f"SELECT source_status, coverage_status FROM {SCHEMA}.source_event "
                f"WHERE source_event_id=%s", (source_id,)
            )
            assert tuple(cur.fetchone()) == ("FINALIZED", "COMPLETE")

            stats2 = evaluator.run_cycle(datetime(2026, 10, 19, 16, 10, 1, tzinfo=timezone.utc))
            assert stats2["errors"] == 0
            cur.execute(
                f"SELECT source_status, coverage_status FROM {SCHEMA}.source_event "
                f"WHERE source_event_id=%s", (source_id,)
            )
            assert tuple(cur.fetchone()) == ("FINALIZED", "COMPLETE")
    finally:
        # Never delete immutable or shared rows by wildcard.
        # TEST_ONLY disposable database teardown occurs outside this test.
        conn.close()
