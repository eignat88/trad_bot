"""ME clean durable runtime integration, no live services."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.shadow.me_r_close_location_capture_spool import (
    MECleanCaptureSpool,
    ME_NAME,
)
from app.shadow.me_r_close_location_capture_replay import (
    replay_me_clean_capture,
)
from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosSaveResult,
    MERLongCLoOosSaveStatus,
)


def make_candidate():
    candle = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)

    return SimpleNamespace(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(
            2026, 10, 9, 13, 35, 10, tzinfo=timezone.utc
        ),
        features={
            "oos_clean_observer_version": "1.2.0",
            "oos_signal_open": 101.0,
            "oos_signal_high": 103.0,
            "oos_signal_low": 99.0,
            "oos_signal_close": 100.0,
            "oos_signal_volume": 1200.0,
            "oos_signal_entry_price": 100.0,
            "oos_signal_candle_open_ms": int(
                candle.timestamp() * 1000
            ),
            "close_location_source_timestamp": candle.isoformat(),
            "close_location": 0.25,
            "close_location_threshold": 0.70,
            "close_location_passed": False,
        },
    )


def test_durable_replay_after_database_recovery():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "capture.sqlite3"

        spool = MECleanCaptureSpool(path)
        spool.enqueue(make_candidate())
        spool.close()

        spool = MECleanCaptureSpool(path)
        repo = MagicMock()

        repo.save_signal.return_value = MERLongCLoOosSaveResult(
            MERLongCLoOosSaveStatus.ERROR
        )

        failed = replay_me_clean_capture(spool, repo)
        assert failed["pending"] == 1
        assert spool.stats()["PENDING"] == 1

        spool.close()
        spool = MECleanCaptureSpool(path)

        repo.save_signal.return_value = MERLongCLoOosSaveResult(
            MERLongCLoOosSaveStatus.INSERTED,
            signal_id=123,
        )

        recovered = replay_me_clean_capture(spool, repo)

        assert recovered["delivered"] == 1
        assert spool.stats()["PENDING"] == 0
        assert spool.stats()["DELIVERED"] == 1
        assert repo.save_signal.call_count == 2

        spool.close()


def test_spool_failure_does_not_claim_success():
    with TemporaryDirectory() as directory:
        spool = MECleanCaptureSpool(
            Path(directory) / "capture.sqlite3"
        )
        spool.close()

        import pytest

        with pytest.raises(sqlite3.ProgrammingError):
            spool.enqueue(make_candidate())

        # No acknowledgement can be claimed after failed enqueue.


def test_scan_cycle_replays_without_symbols():
    import scanner_runner

    spool = MagicMock()
    repo = SimpleNamespace(
        _conn=object(),
        _database="me_close_location_it",
        _user="me_test",
        _password=None,
        _unix_sock=None,
        _host="127.0.0.1",
        _port=55432,
    )

    replay_connection = MagicMock()

    with patch.object(
        scanner_runner.ScannerDirectionGatePolicy,
        "load_for_cycle",
    ) as gate, patch(
        "app.shadow.me_r_close_location_capture_replay."
        "replay_me_clean_capture"
    ) as replay, patch(
        "pg8000.connect",
        return_value=replay_connection,
    ) as connect:

        replay.return_value = {
            "processed": 1,
            "delivered": 1,
            "pending": 0,
            "quarantined": 0,
        }

        result = scanner_runner.run_scan_cycle(
            client=MagicMock(),
            orchestrator=SimpleNamespace(scanners={}),
            repository=repo,
            symbols=[],
            run_id=None,
            settings=MagicMock(),
            me_capture_spool=spool,
        )

        assert result == (0, 0, 0)
        replay.assert_called_once()
        gate.assert_not_called()

        connect.assert_called_once_with(
            database="me_close_location_it",
            user="me_test",
            password=None,
            host="127.0.0.1",
            port=55432,
        )

        replay_repo = replay.call_args.args[1]
        assert replay_repo._conn is replay_connection
        assert replay_repo._conn is not repo._conn
        replay_connection.close.assert_called_once()
