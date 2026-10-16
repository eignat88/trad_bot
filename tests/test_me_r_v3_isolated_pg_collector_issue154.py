"""Issue #154: opt-in live PostgreSQL collector integration, disposable DB only.

Run under sudo -u trad_bot_me_r_v3_oos with
ME_R_V3_PG_INTEGRATION=true. No external exchange calls.
All written rows are rolled back before connection closure.
"""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.research.me_r_long_early_mae_v3 import isolated_oos
from app.research.me_r_long_early_mae_v3.pipeline import MeEarlyMaeV3Pipeline

DB = "trad_bot_me_r_v3_oos_pg_test"
ROLE = "trad_bot_me_r_v3_oos"
SCHEMA = "research_me_early_mae_v3"


def _connect():
    import pg8000.dbapi
    conn = pg8000.dbapi.connect(
        user=ROLE, database=DB, unix_sock=isolated_oos.SOCKET
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT current_database(), current_user, session_user, inet_client_addr() IS NULL"
    )
    assert tuple(cur.fetchone()) == (DB, ROLE, ROLE, True)
    conn.rollback()
    return conn


@pytest.mark.skipif(
    os.environ.get("ME_R_V3_PG_INTEGRATION") != "true",
    reason="explicit disposable PostgreSQL opt-in required",
)
def test_market_collector_insert_duplicate_failure_rollback(monkeypatch):
    import pwd
    assert os.geteuid() == pwd.getpwnam(ROLE).pw_uid
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM dds.instrument), "
            "(SELECT COUNT(*) FROM market.candle), "
            f"(SELECT COUNT(*) FROM {SCHEMA}.source_event)"
        )
        assert tuple(cur.fetchone()) == (0, 0, 0), "TEST_DB_NOT_EMPTY"
        conn.rollback()

        activated = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
        opened = activated + timedelta(minutes=5)
        observed = opened + timedelta(minutes=5, seconds=25)
        candle = SimpleNamespace(
            timestamp=int(opened.timestamp() * 1000),
            open=100, high=102, low=99, close=101, volume=5,
        )
        monkeypatch.setattr(isolated_oos, "ACTIVATION_AT", activated)
        inserted, instrument_id = isolated_oos.store_latest_5m(
            conn, "BTCUSDT", candle, observed
        )
        assert inserted and instrument_id > 0
        inserted2, same_id = isolated_oos.store_latest_5m(
            conn, "BTCUSDT", candle, observed
        )
        assert not inserted2 and same_id == instrument_id
        cur.execute("SELECT COUNT(*) FROM market.candle")
        assert cur.fetchone()[0] == 1

        # Inject a failure at the source-capture boundary before commit.
        class InjectedFailurePipeline:
            def capture_source(self, source, *, commit=True):
                assert commit is False
                raise RuntimeError("INJECTED_CAPTURE_FAILURE")
        with pytest.raises(RuntimeError, match="INJECTED_CAPTURE_FAILURE"):
            InjectedFailurePipeline().capture_source(
                {"instrument_id": instrument_id}, commit=False
            )
        conn.rollback()
        cur.execute("SELECT (SELECT COUNT(*) FROM dds.instrument), "
                    "(SELECT COUNT(*) FROM market.candle)")
        assert tuple(cur.fetchone()) == (0, 0)
        conn.rollback()
    finally:
        conn.rollback()
        conn.close()


def test_transaction_contract_source_adapter_and_repository():
    import inspect
    from app.research.me_r_long_early_mae_v3.repository import MeEarlyMaeV3Repository
    from app.research.me_r_long_early_mae_v3.capture import MeEarlyMaeV3SourceAdapter
    assert inspect.signature(MeEarlyMaeV3Repository.insert_source_with_variants).parameters["commit"].default is True
    assert inspect.signature(MeEarlyMaeV3SourceAdapter.capture).parameters["commit"].default is True
    assert inspect.signature(MeEarlyMaeV3Pipeline.capture_source).parameters["commit"].default is True
