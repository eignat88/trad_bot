"""One-shot PostgreSQL atomic E2E for Issue #154.

Opt in with ME_R_V3_ATOMIC_E2E=true. Run only on the disposable
trad_bot_me_r_v3_oos_pg_test. A successful COMMIT intentionally persists
one source and seven variants; never run against the clean OOS database.
No cleanup or immutable-row deletion is attempted.
"""
import os
import pwd
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.research.me_r_long_early_mae_v3 import (
    capture, isolated_oos, protocol, repository,
)
from app.research.me_r_long_early_mae_v3.pipeline import MeEarlyMaeV3Pipeline

DB = "trad_bot_me_r_v3_oos_pg_test"
ROLE = "trad_bot_me_r_v3_oos"
SCHEMA = "research_me_early_mae_v3"
FROZEN = datetime(2026, 10, 15, 15, tzinfo=timezone.utc)
ACTIVATION = datetime(2026, 10, 15, 16, tzinfo=timezone.utc)


def _counts(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT (SELECT count(*) FROM dds.instrument), "
        "(SELECT count(*) FROM market.candle), "
        f"(SELECT count(*) FROM {SCHEMA}.protocol), "
        f"(SELECT count(*) FROM {SCHEMA}.variant), "
        f"(SELECT count(*) FROM {SCHEMA}.source_event), "
        f"(SELECT count(*) FROM {SCHEMA}.variant_outcome), "
        f"(SELECT count(*) FROM {SCHEMA}.pipeline_run)"
    )
    result = tuple(cur.fetchone())
    conn.rollback()
    return result


def _candle(opened):
    return SimpleNamespace(
        timestamp=int(opened.timestamp() * 1000),
        open=100.0, high=102.0, low=99.0, close=100.0, volume=5.0,
    )


def _source(opened, instrument_id, label):
    close = opened + timedelta(minutes=5)
    completed = close + timedelta(seconds=5)
    detected = close + timedelta(seconds=6)
    return {
        "source_setup_id": label,
        "instrument_id": instrument_id,
        "symbol": "BTCUSDT",
        "scanner_name": protocol.SCANNER_NAME,
        "scanner_version": protocol.SCANNER_VERSION,
        "direction": "LONG",
        "source_candle_open_at": opened,
        "source_candle_close_at": close,
        "source_fetch_started_at": close + timedelta(seconds=1),
        "source_available_at": completed,
        "source_fetch_completed_at": completed,
        "detected_at": detected,
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
        "reference_price": 100.0,
        "target_price": 103.0,
    }


@pytest.mark.skipif(
    os.environ.get("ME_R_V3_ATOMIC_E2E") != "true",
    reason="explicit one-shot disposable database opt-in required",
)
def test_real_candle_source_seven_variants_commit_and_rollback(monkeypatch):
    import pg8000.dbapi

    assert os.geteuid() == pwd.getpwnam(ROLE).pw_uid
    conn = pg8000.dbapi.connect(
        user=ROLE,
        database=DB,
        unix_sock="/var/run/postgresql/.s.PGSQL.5432",
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT current_database(), current_user, session_user, "
            "inet_client_addr() IS NULL"
        )
        assert tuple(cur.fetchone()) == (DB, ROLE, ROLE, True)
        conn.rollback()
        assert _counts(conn) == (0, 0, 0, 0, 0, 0, 0), "DISPOSABLE_DB_NOT_EMPTY"

        actual = protocol.calculate_protocol_fingerprint(
            protocol.protocol_payload(FROZEN, ACTIVATION)
        )
        monkeypatch.setattr(isolated_oos, "ACTIVATION_AT", ACTIVATION)
        pipeline = MeEarlyMaeV3Pipeline(conn)

        with patch.object(repository, "FROZEN_AT", FROZEN), \
             patch.object(repository, "ACTIVATION_AT", ACTIVATION), \
             patch.object(repository, "require_protocol_fingerprint", return_value=actual), \
             patch.object(repository, "validate_freeze_activation", return_value=None), \
             patch.object(capture, "ACTIVATION_AT", ACTIVATION), \
             patch.object(capture, "require_runtime_configuration", return_value=None), \
             patch.object(protocol, "APPROVED_FINGERPRINT", actual), \
             patch.object(protocol, "FROZEN_AT", FROZEN), \
             patch.object(protocol, "ACTIVATION_AT", ACTIVATION):
            pipeline.initialize()
            assert _counts(conn) == (0, 0, 1, 7, 0, 0, 0)

            opened = ACTIVATION + timedelta(minutes=5)
            observed = opened + timedelta(minutes=5, seconds=5)
            inserted, instrument_id = isolated_oos.store_latest_5m(
                conn, "BTCUSDT", _candle(opened), observed
            )
            assert inserted and instrument_id > 0
            source_id = pipeline.capture_source(
                _source(opened, instrument_id, "ISSUE154-E2E-SUCCESS"),
                commit=False,
            )
            assert isinstance(source_id, int) and source_id > 0
            cur = conn.cursor()
            cur.execute(
                f"SELECT count(*) FROM {SCHEMA}.variant_outcome "
                "WHERE source_event_id=%s", (source_id,)
            )
            assert cur.fetchone()[0] == 7
            conn.commit()
            assert _counts(conn) == (1, 1, 1, 7, 1, 7, 0)

            # Next signal deliberately fails AFTER real repository insert
            # but BEFORE the caller commit. Rollback must remove both.
            second = opened + timedelta(minutes=5)
            second_observed = second + timedelta(minutes=5, seconds=5)
            inserted2, second_id = isolated_oos.store_latest_5m(
                conn, "BTCUSDT", _candle(second), second_observed
            )
            assert inserted2 and second_id == instrument_id
            pending_id = pipeline.capture_source(
                _source(second, second_id, "ISSUE154-E2E-ROLLBACK"),
                commit=False,
            )
            assert pending_id > 0
            with pytest.raises(RuntimeError, match="INJECTED_PRE_COMMIT_FAILURE"):
                raise RuntimeError("INJECTED_PRE_COMMIT_FAILURE")
            conn.rollback()
            assert _counts(conn) == (1, 1, 1, 7, 1, 7, 0)
            cur = conn.cursor()
            cur.execute(
                f"SELECT count(*) FROM {SCHEMA}.source_event "
                "WHERE source_setup_id=%s",
                ("ISSUE154-E2E-ROLLBACK",),
            )
            assert cur.fetchone()[0] == 0
            conn.rollback()
    finally:
        conn.rollback()
        conn.close()
