"""Issue #152 — TEST_ONLY real PostgreSQL lifecycle integration test.

Only the dedicated database ``trad_bot_runner_152_it`` is used through the exact
runner connection path. Temporary instrument/candle fixtures are removed by
exact IDs in ``finally``; no production database or service is touched.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.research.me_r_long_early_mae_v3 import protocol, repository
from app.research.me_r_long_early_mae_v3.evaluator import MeEarlyMaeV3Evaluator
from app.research.me_r_long_early_mae_v3.runner import (
    connect_isolated_database,
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
               (instrument_id, symbol, base_asset, quote_asset, category, status)
               VALUES (%s, %s, 'ISSUE', 'USDT', 'ISOLATED_TEST_ONLY', 'ACTIVE')""",
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
    conn = connect_isolated_database()
    try:
        require_isolated_schema(conn)
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
        # Disposable cleanup is guarded by exact TEST_ONLY identity. No
        # arbitrary schemas or production rows are deleted. Finalized rows
        # are not force-mutated; immutable triggers remain authoritative.
        try:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM {SCHEMA}.variant_outcome "
                f"WHERE source_event_id IN ("
                f"  SELECT source_event_id FROM {SCHEMA}.source_event "
                f"  WHERE protocol_version=%s AND source_setup_id LIKE %s"
                f")",
                (protocol.PROTOCOL_VERSION, "ISSUE152-%"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM {SCHEMA}.source_event "
                f"WHERE protocol_version=%s AND source_setup_id LIKE %s",
                (protocol.PROTOCOL_VERSION, "ISSUE152-%"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM market.candle WHERE source = %s",
                ("ISSUE152_TEST_ONLY",),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dds.instrument WHERE symbol = %s",
                ("ISSUE152TEST",),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()
