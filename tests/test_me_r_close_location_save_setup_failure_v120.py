"""ME clean capture survives main scanner setup persistence failure."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import scanner_runner

from app.shadow.me_r_close_location_capture_spool import (
    MECleanCaptureSpool,
    ME_NAME,
)
from app.scanners.models import SetupCandidate, SetupState


def test_me_spooled_before_save_setup_failure():
    candle = datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc)
    decision = datetime(2026, 10, 9, 13, 35, 10, tzinfo=timezone.utc)

    candidate = SetupCandidate(
        scanner_name=ME_NAME,
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=decision,
        state=SetupState.EXPIRED,
        features={
            "oos_clean_observer_version": "1.2.0",
            "oos_signal_open": 101.0,
            "oos_signal_high": 103.0,
            "oos_signal_low": 99.0,
            "oos_signal_close": 100.0,
            "oos_signal_volume": 1200.0,
            "oos_signal_entry_price": 100.0,
            "oos_signal_candle_open_ms": int(candle.timestamp() * 1000),
            "close_location_source_timestamp": candle.isoformat(),
            "close_location": 0.25,
            "close_location_threshold": 0.70,
            "close_location_passed": False,
        },
    )

    ctx = SimpleNamespace(symbol="BTCUSDT")

    orchestrator = MagicMock()
    orchestrator.scanners = {ME_NAME: object()}
    orchestrator.scan_all_with_stats.return_value = (
        [],
        {
            ME_NAME: {
                "symbols_scanned": 1,
                "candidates_found": 1,
                "setups_saved": 0,
                "errors_count": 0,
                "duration_ms": 1.0,
            },
            "_observe_candidates": [candidate],
        },
    )

    repo = MagicMock()
    repo._conn = None
    repo.save_setup.side_effect = RuntimeError("simulated setup DB failure")

    settings = MagicMock()
    settings.scanner_workers = 1
    settings.expectancy_filter_enabled = False
    settings.blocked_scanner_directions = frozenset()
    settings.scanner_regime_whitelist = {}
    settings.regime_filter_enabled = False

    with TemporaryDirectory() as directory:
        path = Path(directory) / "capture.sqlite3"
        spool = MECleanCaptureSpool(path)

        future = MagicMock()
        future.result.return_value = ctx

        executor = MagicMock()
        executor.__enter__.return_value.submit.return_value = future

        with patch.object(
            scanner_runner, "ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            scanner_runner, "as_completed",
            return_value=[future],
        ), patch.object(
            scanner_runner.ScannerDirectionGatePolicy,
            "load_for_cycle",
        ), patch(
            "pg8000.connect",
            side_effect=ConnectionError("simulated PostgreSQL outage"),
        ):
            result = scanner_runner.run_scan_cycle(
                client=MagicMock(),
                orchestrator=orchestrator,
                repository=repo,
                symbols=["BTCUSDT"],
                run_id=None,
                settings=settings,
                me_capture_spool=spool,
            )

        assert len(spool.pending()) == 1
        assert spool.pending()[0][1]["symbol"] == "BTCUSDT"
        assert repo.save_setup.call_count == 1
        assert result[1] == 1

        spool.close()

        # The event must still exist after the process closes its DB handle.
        spool = MECleanCaptureSpool(path)
        assert len(spool.pending()) == 1
        spool.close()
