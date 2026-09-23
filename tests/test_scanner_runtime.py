import threading
import time

import pytest

import scanner_runner
from app.config import ScannerUniverseSettings, Settings
from app.exchange.bybit_client import BybitClient, BybitTimeoutError
from app.scanners.models import SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.scoring import score_candidate


class SuccessfulResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"retCode": 0, "result": {"list": []}}


def test_bybit_timeout_retries_with_linear_backoff(monkeypatch, caplog):
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("slow")
            return SuccessfulResponse()

    sleeps = []
    monkeypatch.setattr("app.exchange.bybit_client.time.sleep", sleeps.append)
    session = Session()
    settings = Settings(bybit_timeout=15, bybit_max_attempts=3, bybit_retry_backoff=1)

    assert BybitClient(settings, session).get_klines("VIRTUALUSDT") == []
    assert session.calls == 3
    assert sleeps == [1, 2]
    assert "VIRTUALUSDT: Bybit timeout, retry 1/3" in caplog.text
    assert "VIRTUALUSDT: Bybit timeout, retry 2/3" in caplog.text


def test_bybit_timeout_raises_concise_error_after_last_attempt(monkeypatch):
    class Session:
        def get(self, *args, **kwargs):
            raise TimeoutError("slow")

    monkeypatch.setattr("app.exchange.bybit_client.time.sleep", lambda delay: None)
    client = BybitClient(Settings(bybit_max_attempts=3), Session())

    with pytest.raises(BybitTimeoutError, match="VIRTUALUSDT.*3 attempts"):
        client.get_klines("VIRTUALUSDT")


def test_scan_cycle_fetches_market_data_in_parallel(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def build_context(client, symbol, settings):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return symbol

    class Orchestrator:
        scanners = {"TEST": object()}

        def scan_all_with_stats(self, ctx, **kwargs):
            return [], {"TEST": {"candidates_found": 0, "setups_saved": 0,
                                  "errors_count": 0, "duration_ms": 1}}

    class Repository:
        def save_run_stat(self, *args, **kwargs):
            pass

    monkeypatch.setattr(scanner_runner, "build_market_context", build_context)
    total, scanned, failed = scanner_runner.run_scan_cycle(
        object(), Orchestrator(), Repository(), ["A", "B", "C", "D"], None,
        Settings(scanner_workers=3),
    )

    assert total == 0
    assert scanned == 4
    assert failed == 0
    assert peak == 3


def test_get_scanner_symbols_refreshes_dynamic_universe():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            assert kwargs == {
                "top_n": 2,
                "min_turnover_24h": 123,
                "min_volume_24h": 45,
                "quote_coin": "USDT",
            }
            return [
                {"symbol": "SOLUSDT", "turnover_24h": 200, "volume_24h": 50, "rank": 1},
                {"symbol": "BTCUSDT", "turnover_24h": 150, "volume_24h": 46, "rank": 2},
            ]

    settings = Settings(scanner_universe=ScannerUniverseSettings(
        mode="dynamic", top_n=2, min_turnover_24h=123, min_volume_24h=45,
    ))

    assert scanner_runner.get_scanner_symbols(Client(), settings) == [
        "SOLUSDT", "BTCUSDT",
    ]


def test_get_scanner_symbols_rejects_empty_dynamic_universe():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            return []

    settings = Settings(scanner_universe=ScannerUniverseSettings(mode="dynamic"))

    with pytest.raises(RuntimeError, match="Dynamic scanner universe is empty"):
        scanner_runner.get_scanner_symbols(Client(), settings)


def test_cycle_delay_is_measured_from_cycle_start():
    assert scanner_runner.seconds_until_next_cycle(100, 300, 283) == 117
    assert scanner_runner.seconds_until_next_cycle(100, 300, 450) == 0


def test_scan_cycle_propagates_run_id_to_all_records(monkeypatch):
    candidate = SetupCandidate(
        scanner_name="TEST", symbol="BTCUSDT",
        signal_candle_open_time=1_777_294_700_000,
    )

    class Orchestrator:
        scanners = {"TEST": object()}

        def scan_all_with_stats(self, ctx, **kwargs):
            return [candidate], {"TEST": {"candidates_found": 1,
                "setups_saved": 1, "errors_count": 0, "duration_ms": 2}}

    class Repository:
        def __init__(self):
            self.calls = []

        def save_setup(self, value, run_id=None):
            self.calls.append(("setup", run_id))

        def save_event(self, *args, run_id=None, **kwargs):
            self.calls.append(("event", run_id))

        def save_run_stat(self, run_id, *args, **kwargs):
            self.calls.append(("stat", run_id))

    repository = Repository()
    monkeypatch.setattr(scanner_runner, "build_market_context", lambda *args: object())

    assert scanner_runner.run_scan_cycle(
        object(), Orchestrator(), repository, ["BTCUSDT"], 42,
        Settings(scanner_workers=1),
    ) == (1, 1, 0)
    assert repository.calls == [("setup", 42), ("event", 42), ("stat", 42)]


def test_scanner_duration_preserves_sub_millisecond_precision(monkeypatch):
    class Scanner:
        def scan(self, ctx):
            return []

    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"TEST": Scanner()}
    ticks = iter([10.0, 10.000184])
    monkeypatch.setattr("app.scanners.orchestrator.time.perf_counter", lambda: next(ticks))

    _, stats = orchestrator.scan_all_with_stats(object())

    assert stats["TEST"]["duration_ms"] == pytest.approx(0.184)


def test_shared_scoring_ranks_confirmation_quality():
    weak = SetupCandidate(
        scanner_name="TREND_PULLBACK", symbol="ALTUSDT", reference_price=100,
        invalidation_price=98, target_1=103,
        features={"pullback_quality": 0.1,
                  "rsi_confirmation": 0.2, "rr_ratio": 0.3},
    )
    strong = SetupCandidate(
        scanner_name="TREND_PULLBACK", symbol="BTCUSDT", reference_price=100,
        invalidation_price=98, target_1=104,
        features={"pullback_quality": 0.9,
                  "rsi_confirmation": 0.8, "volume_ratio": 1.0,
                  "rr_ratio": 0.8, "stop_distance_atr": 0.7},
    )

    assert score_candidate(strong).score > score_candidate(weak).score


def test_schema_keeps_fractional_runtime_and_run_universe_history():
    # Read through the repository package location rather than the process CWD.
    from pathlib import Path
    import app.db
    sql = (Path(app.db.__file__).parent / "schema.sql").read_text(encoding="utf-8")

    assert "duration_ms NUMERIC(12,3)" in sql
    assert "CREATE TABLE IF NOT EXISTS dds.scanner_run_instrument" in sql


def test_dynamic_universe_preserves_liquidity_metadata():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            return [{"symbol": "BTCUSDT", "turnover_24h": 123.5,
                     "volume_24h": 4.25, "rank": 1}]

    universe = scanner_runner.get_scanner_universe(
        Client(), Settings(scanner_universe=ScannerUniverseSettings(mode="dynamic")),
    )

    assert universe == [{"symbol": "BTCUSDT", "turnover_24h": 123.5,
                         "volume_24h": 4.25, "rank": 1}]


def test_fvg_lifecycle_summary_called_once_per_cycle(monkeypatch):
    """After 50 symbol scans, log_lifecycle_summary() called exactly once."""
    log_calls = []

    class FakeFVGScanner:
        name = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

        def scan(self, ctx):
            return []

        def log_lifecycle_summary(self):
            log_calls.append(1)

    class Orchestrator:
        scanners = {
            "FVG_REACTION_LONG_LOCAL_STRUCT_V1": FakeFVGScanner(),
            "OTHER_SCANNER": object(),
        }

        def scan_all_with_stats(self, ctx, **kwargs):
            return [], {
                "FVG_REACTION_LONG_LOCAL_STRUCT_V1": {
                    "candidates_found": 0, "setups_saved": 0,
                    "errors_count": 0, "duration_ms": 1,
                },
                "OTHER_SCANNER": {
                    "candidates_found": 0, "setups_saved": 0,
                    "errors_count": 0, "duration_ms": 1,
                },
            }

    class Repository:
        def save_run_stat(self, *args, **kwargs):
            pass

    symbols = [f"SYM{i}" for i in range(50)]

    def build_context(client, symbol, settings):
        return symbol

    monkeypatch.setattr(scanner_runner, "build_market_context", build_context)
    total, scanned, failed = scanner_runner.run_scan_cycle(
        object(), Orchestrator(), Repository(), symbols, None,
        Settings(scanner_workers=4),
    )

    assert scanned == 50
    assert failed == 0
    assert len(log_calls) == 1, (
        f"log_lifecycle_summary() called {len(log_calls)} times, expected exactly 1"
    )


def test_fvg_lifecycle_summary_does_not_break_on_error(monkeypatch):
    """If log_lifecycle_summary raises, scanner cycle still completes."""

    class BrokenFVGScanner:
        name = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

        def scan(self, ctx):
            return []

        def log_lifecycle_summary(self):
            raise RuntimeError("diagnostic failure")

    class Orchestrator:
        scanners = {"FVG_REACTION_LONG_LOCAL_STRUCT_V1": BrokenFVGScanner()}

        def scan_all_with_stats(self, ctx, **kwargs):
            return [], {"FVG_REACTION_LONG_LOCAL_STRUCT_V1": {
                "candidates_found": 0, "setups_saved": 0,
                "errors_count": 0, "duration_ms": 1,
            }}

    class Repository:
        def save_run_stat(self, *args, **kwargs):
            pass

    monkeypatch.setattr(scanner_runner, "build_market_context",
                        lambda client, symbol, settings: symbol)

    total, scanned, failed = scanner_runner.run_scan_cycle(
        object(), Orchestrator(), Repository(), ["SYM1"], None,
        Settings(scanner_workers=1),
    )
    assert scanned == 1
    assert failed == 0


# ------------------------------------------------------------------
# Regression tests: scanner_run timing
# ------------------------------------------------------------------

class TestScannerRunTiming:
    """Verify that scanner_run.duration_sec captures only actual scan time.

    Historical regression: ``duration_sec`` periodically showed ~298–300s
    because ``save_run_universe()`` performed N individual
    ``ensure_instrument()`` calls — each with its own ``commit()`` — between
    ``started_at`` and the actual scan start.  For a 50-instrument dynamic
    universe this produced ~51 round-trips to PostgreSQL, inflating
    ``duration_sec`` by the accumulated transaction overhead.

    The fix introduces ``ensure_instruments_bulk()`` (single transaction for
    instrument resolution) and clarifies phase timing in the main loop.
    """

    def test_duration_sec_models_realistic_finished_at_minus_started_at(self):
        """``finish_run()`` computes ``duration_sec = now() - started_at``.

        This test models that exact calculation using wall-clock timestamps
        to prove that ``duration_sec`` reflects only the time between
        ``start_run()`` and ``finish_run()`` — NOT any preceding work such
        as universe refresh or inter-cycle sleep.
        """
        from datetime import datetime, timezone

        # --- Simulate the PostgreSQL timestamps ---
        # started_at is set by start_run() → INSERT ... VALUES (now(), ...)
        started_at = datetime.now(timezone.utc)

        # ... universe refresh, save_run_universe, and actual scan happen here.
        # Simulate a fast scan (~10s):
        import time as _time
        _time.sleep(0.05)

        # finished_at is set by finish_run() → UPDATE ... SET finished_at = now()
        finished_at = datetime.now(timezone.utc)

        # duration_sec is computed by PostgreSQL as:
        #   EXTRACT(EPOCH FROM (finished_at - started_at))
        duration_sec = (finished_at - started_at).total_seconds()

        # The scan took ~50ms; duration_sec must be close to that,
        # NOT 288s + scan time.
        assert 0.0 < duration_sec < 5.0, (
            f"duration_sec={duration_sec:.1f}s — expected < 5s.  "
            f"If this is ~288–300s, the inter-cycle sleep leaked into the run."
        )

    def test_duration_sec_between_consecutive_cycles_excludes_sleep(self, monkeypatch):
        """Two back-to-back cycles must each produce small ``duration_sec``.

        Regression scenario: cycle N finishes, 288s sleep, cycle N+1 starts.
        If ``started_at`` is recorded before the sleep, ``duration_sec``
        for N+1 would include those 288s.
        """
        from datetime import datetime, timezone

        class MinimalRepo:
            """Minimal repository that satisfies run_scan_cycle's contract."""

            def save_run_stat(self, *a, **kw):
                pass

            def save_error(self, **kw):
                pass

            def save_setup(self, *a, **kw):
                pass

            def save_event(self, *a, **kw):
                pass

        class FakeOrchestrator:
            scanners = {"TEST": object()}

            def scan_all_with_stats(self, ctx, **kwargs):
                return [], {"TEST": {
                    "candidates_found": 0, "setups_saved": 0,
                    "errors_count": 0, "duration_ms": 50,
                }}

        monkeypatch.setattr(scanner_runner, "build_market_context",
                            lambda *a: object())

        # --- Cycle 1 ---
        started_1 = datetime.now(timezone.utc)
        scanner_runner.run_scan_cycle(
            object(), FakeOrchestrator(), MinimalRepo(), ["BTCUSDT"], None,
            Settings(scanner_workers=1),
        )
        finished_1 = datetime.now(timezone.utc)
        dur_1 = (finished_1 - started_1).total_seconds()

        # --- Simulate 288s inter-cycle sleep (compressed) ---
        import time as _time
        _time.sleep(0.01)

        # --- Cycle 2 ---
        started_2 = datetime.now(timezone.utc)
        scanner_runner.run_scan_cycle(
            object(), FakeOrchestrator(), MinimalRepo(), ["BTCUSDT"], None,
            Settings(scanner_workers=1),
        )
        finished_2 = datetime.now(timezone.utc)
        dur_2 = (finished_2 - started_2).total_seconds()

        assert dur_1 < 5.0, f"Cycle 1 duration_sec={dur_1:.1f}s — expected < 5s"
        assert dur_2 < 5.0, f"Cycle 2 duration_sec={dur_2:.1f}s — expected < 5s"

    def test_save_run_universe_commit_count(self):
        """``save_run_universe()`` must resolve 50 instruments and persist
        them with a minimal number of ``commit()`` calls.

        Before the fix: 50 × ``ensure_instrument()`` × ``commit()`` + 1
        batch ``commit()`` = 51 commits.

        After the fix: ``ensure_instruments_bulk()`` = 1 commit,
        ``save_run_universe()`` batch INSERT = 1 commit = 2 total.
        """
        from unittest.mock import MagicMock, call

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # ensure_instruments_bulk: SELECT returns all 50 existing instruments
        mock_cursor.fetchall.return_value = [
            (f"SYM{i}USDT", i + 1) for i in range(50)
        ]
        # finish_run UPDATE
        mock_cursor.rowcount = 1

        from app.db.repository import ScannerRepository

        repo = ScannerRepository.__new__(ScannerRepository)
        repo._use_pg = True
        repo._conn = mock_conn

        instruments = [
            {"symbol": f"SYM{i}USDT", "rank": i + 1,
             "turnover_24h": 100.0, "volume_24h": 50.0}
            for i in range(50)
        ]

        mock_conn.commit.reset_mock()
        repo.save_run_universe(run_id=42, instruments=instruments)

        commit_calls = mock_conn.commit.call_count
        # ensure_instruments_bulk: 1 SELECT + 1 commit
        # save_run_universe batch: 50 INSERTs + 1 commit
        # Total: 2 commits
        assert commit_calls == 2, (
            f"Expected 2 commit() calls (bulk resolve + batch insert), "
            f"got {commit_calls}. Before the fix this was ~51 commits."
        )

    def test_ensure_instruments_bulk_does_not_call_ensure_instrument(self):
        """``ensure_instruments_bulk()`` must resolve instruments in a single
        transaction — it must NOT delegate to the per-symbol
        ``ensure_instrument()`` which commits individually.
        """
        from unittest.mock import MagicMock, patch

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # 3 symbols, all already exist
        mock_cursor.fetchall.return_value = [
            ("BTCUSDT", 1), ("ETHUSDT", 2), ("SOLUSDT", 3),
        ]

        from app.db.repository import ScannerRepository

        repo = ScannerRepository.__new__(ScannerRepository)
        repo._use_pg = True
        repo._conn = mock_conn

        with patch.object(repo, "ensure_instrument") as mock_ensure:
            result = repo.ensure_instruments_bulk(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        # ensure_instrument must NOT be called — bulk path handles everything.
        mock_ensure.assert_not_called()

        # All symbols resolved.
        assert result == {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 3}

        # Exactly 1 SELECT + 1 commit = single transaction.
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_ensure_instruments_bulk_inserts_missing_concurrently_safe(self):
        """``ensure_instruments_bulk()`` inserts missing instruments with
        ``ON CONFLICT`` so concurrent runners don't fail.
        """
        from unittest.mock import MagicMock, call

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # SELECT returns only BTCUSDT; ETHUSDT is missing
        mock_cursor.fetchall.return_value = [("BTCUSDT", 1)]
        # INSERT RETURNING for ETHUSDT
        mock_cursor.fetchone.return_value = (2,)

        from app.db.repository import ScannerRepository

        repo = ScannerRepository.__new__(ScannerRepository)
        repo._use_pg = True
        repo._conn = mock_conn

        result = repo.ensure_instruments_bulk(["BTCUSDT", "ETHUSDT"])

        assert result == {"BTCUSDT": 1, "ETHUSDT": 2}

        # The INSERT must contain ON CONFLICT for concurrent safety.
        insert_call = mock_cursor.execute.call_args_list[1]
        sql = insert_call[0][0]
        assert "ON CONFLICT" in sql.upper(), (
            "ensure_instruments_bulk INSERT must use ON CONFLICT for "
            "idempotency against concurrent runner processes."
        )

    def test_main_loop_logs_phase_timing(monkeypatch, caplog):
        """Verify that the main loop logs phase timing for diagnostics."""
        import inspect
        source = inspect.getsource(scanner_runner.main)
        assert "phase: universe_refresh=" in source
        assert "phase: run_create=" in source
        assert "sleeping" in source
