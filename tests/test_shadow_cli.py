"""Tests for Shadow CLI entrypoints."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestShadowBackfillCLI:
    """Test cases for shadow backfill CLI."""

    def test_backfill_main_help(self):
        """Test backfill CLI help message."""
        with patch("sys.argv", ["python", "-m", "app.shadow.backfill", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                from app.shadow.backfill import main
                main()
            assert exc_info.value.code == 0

    def test_backfill_main_missing_start(self):
        """Test backfill CLI without required --start argument."""
        with patch("sys.argv", ["python", "-m", "app.shadow.backfill"]):
            with pytest.raises(SystemExit) as exc_info:
                from app.shadow.backfill import main
                main()
            assert exc_info.value.code != 0


class TestShadowRunnerCLI:
    """Test cases for shadow runner CLI."""

    def test_runner_main_help(self):
        """Test runner CLI help message."""
        with patch("sys.argv", ["python", "-m", "app.shadow.runner", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                from app.shadow.runner import main
                main()
            assert exc_info.value.code == 0

    def test_runner_main_once(self):
        """Test runner CLI with --once flag."""
        with patch("sys.argv", ["runner", "--once"]):
            with patch("app.shadow.runner.load_settings") as mock_settings:
                with patch("app.shadow.runner.ScannerRepository") as mock_repo:
                    with patch("app.shadow.runner.BybitClient") as mock_client:
                        with patch("app.shadow.runner.ShadowScannerRunner") as mock_runner:
                            mock_settings_obj = MagicMock(
                                db_host="localhost",
                                db_port=5432,
                                db_name="trad_bot",
                                db_user="postgres",
                                db_password="",
                                bybit_api_key="",
                                bybit_api_secret="",
                                bybit_timeout=15,
                                symbols=["BTCUSDT"],
                                scanner_universe=MagicMock(top_n=50),
                                scanner_workers=5,
                            )
                            mock_settings.return_value = mock_settings_obj
                            mock_repo_instance = MagicMock()
                            mock_repo_instance._use_pg = True
                            mock_repo.return_value = mock_repo_instance
                            mock_runner_instance = MagicMock()
                            mock_runner_instance.run_cycle.return_value = {
                                "symbols_scanned": 1,
                                "raw_candidates": 1,
                                "strict_pass": 0,
                                "inserted": 1,
                                "duplicates": 0,
                                "scan_errors": 0,
                                "db_errors": 0,
                            }
                            mock_runner.return_value = mock_runner_instance

                            from app.shadow.runner import main
                            main()

                            mock_client.assert_called_once_with(mock_settings_obj)
                            mock_runner_instance.run_cycle.assert_called_once()


class TestShadowEvaluatorCLI:
    """Test cases for shadow evaluator CLI."""

    def test_evaluator_main_help(self):
        """Test evaluator CLI help message."""
        with patch("sys.argv", ["python", "-m", "app.shadow.evaluator", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                from app.shadow.evaluator import main
                main()
            assert exc_info.value.code == 0

    def test_evaluator_main_once(self):
        """Test evaluator CLI with --once flag."""
        with patch("sys.argv", ["evaluator", "--once"]):
            with patch("app.shadow.evaluator.load_settings") as mock_settings:
                with patch("app.shadow.evaluator.ScannerRepository") as mock_repo:
                    with patch("app.shadow.evaluator.BybitClient") as mock_client:
                        with patch("app.shadow.evaluator.ShadowSignalEvaluator") as mock_eval:
                            # Setup mocks
                            mock_settings_obj = MagicMock(
                                db_host="localhost",
                                db_port=5432,
                                db_name="trad_bot",
                                db_user="postgres",
                                db_password="",
                                bybit_api_key="",
                                bybit_api_secret="",
                                bybit_timeout=15,
                            )
                            mock_settings.return_value = mock_settings_obj
                            mock_repo_instance = MagicMock()
                            mock_repo_instance._use_pg = True
                            mock_repo.return_value = mock_repo_instance
                            mock_eval_instance = MagicMock()
                            mock_eval_instance.run_evaluation_cycle.return_value = {
                                "signals_checked": 0,
                                "outcomes_created": 0,
                                "outcomes_updated": 0,
                                "finalized": 0,
                                "errors": 0,
                                "horizons_updated": {"15m": 0, "30m": 0, "60m": 0, "120m": 0, "240m": 0, "eod": 0},
                            }
                            mock_eval.return_value = mock_eval_instance

                            from app.shadow.evaluator import main
                            main()

                            # Verify BybitClient was called with settings
                            mock_client.assert_called_once_with(mock_settings_obj)

                            # Verify evaluator was called
                            mock_eval_instance.run_evaluation_cycle.assert_called_once()


class TestShadowInit:
    """Test cases for shadow module init."""

    def test_lazy_imports(self):
        """Test that shadow module uses lazy imports."""
        import app.shadow

        # Check that the module has __getattr__
        assert hasattr(app.shadow, "__getattr__")

        # Check that importing doesn't eagerly import submodules
        # This is a bit tricky to test directly, but we can check the module structure
        assert "ShadowScannerRunner" in app.shadow.__all__
        assert "ShadowBackfillRunner" in app.shadow.__all__
        assert "ShadowSignalEvaluator" in app.shadow.__all__


class TestShadowBackfillWarmup:
    """Regression tests for backfill warmup and indicator requirements."""

    def test_backfill_stats_dataclass(self):
        """Test BackfillStats dataclass initialization."""
        from app.shadow.backfill import BackfillStats

        stats = BackfillStats()
        assert stats.evaluated == 0
        assert stats.raw_candidates == 0
        assert stats.strict_pass == 0
        assert stats.signals_inserted == 0
        assert stats.duplicates_skipped == 0
        assert stats.all_wick_atr_values == []
        assert stats.wick_atr_bucket_counts == {}
        assert stats.top_candles == []

    def test_indicator_warmup_constant(self):
        """Test that INDICATOR_WARMUP is sufficient for all indicators."""
        from app.shadow.backfill import INDICATOR_WARMUP

        # Need at least:
        # - EMA(200) needs 200 candles
        # - ema_slope(50, lookback=5) needs 55 candles
        # - BB(20) needs 20 candles
        # - ATR(14) needs 15 candles
        # - RSI(14) needs 15 candles
        # - volume_ratio(20) needs 21 candles
        # Total: 200 + 50 = 250 candles minimum
        assert INDICATOR_WARMUP >= 250

    def test_detect_raw_candidate_returns_valid(self):
        """Test detect_raw_candidate returns a valid signal or None."""
        from app.shadow.backfill import ShadowBackfillRunner
        from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner
        from app.models import Candle
        from app.scanners.models import MarketContext, IndicatorSnapshot, MarketLevels

        # Create scanner
        scanner = AtrWickRejectionShortScanner()

        # Create candles
        candles = []
        base_time = int(datetime.now(timezone.utc).timestamp() * 1000)

        for i in range(260):
            base_price = 100.0 - (i * 0.01)
            candles.append(Candle(
                timestamp=base_time + (i * 300000),
                open=base_price,
                high=base_price + 0.5,
                low=base_price - 0.3,
                close=base_price - 0.2,
                volume=1000.0 + (i * 10),
            ))

        # Create context
        indicators = IndicatorSnapshot(
            atr=0.5,
            rsi=65.0,
            ema20=99.0,
            ema50=99.5,
            ema200=100.0,
            bb_upper=100.5,
            bb_lower=98.5,
            bb_width=0.04,
            volume_sma=1000.0,
            adx=25.0,
            ema50_slope=-0.0002,
        )

        ctx = MarketContext(
            symbol="TESTUSDT",
            candles_5m=tuple(candles),
            candles_15m=tuple(candles),
            candles_1h=tuple(candles),
            candles_4h=tuple(candles),
            indicators=indicators,
            market_regime="RANGE",
            levels=MarketLevels(),
            evaluated_at=datetime.now(timezone.utc),
        )

        # Test raw candidate detection
        result = scanner.detect_raw_candidate(ctx)

        # Should return WickRejectionSignal or None
        if result is not None:
            from app.scanners.atr_wick_rejection_short import WickRejectionSignal
            assert isinstance(result, WickRejectionSignal)
            # Raw candidate should have upper_wick > 0 (the only condition)
            assert result.wick_size > 0
            # Raw candidate should have all features computed
            assert result.wick_atr >= 0  # wick_atr is computed but not filtered
            assert result.close_location >= 0  # close_location is computed but not filtered

    def test_no_lookahead_in_context_creation(self):
        """Test that context creation doesn't use future candles."""
        from app.shadow.backfill import ShadowBackfillRunner
        from app.models import Candle

        # Create a mock runner
        mock_settings = MagicMock()
        mock_client = MagicMock()
        mock_repo = MagicMock()
        runner = ShadowBackfillRunner(mock_settings, mock_client, mock_repo)

        # Create candles
        base_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        candles = []

        for i in range(300):
            base_price = 100.0 + (i * 0.01)
            candles.append(Candle(
                timestamp=base_time + (i * 300000),
                open=base_price,
                high=base_price + 0.5,
                low=base_price - 0.3,
                close=base_price + 0.1,
                volume=1000.0,
            ))

        # Create context with only first 260 candles
        ctx = runner._create_context("TESTUSDT", candles[:260], candles[259].timestamp)

        # Verify that the context only has 260 candles
        assert len(ctx.candles_5m) == 260

        # Verify that the last candle is at index 259
        assert ctx.candles_5m[-1].timestamp == candles[259].timestamp


class TestShadowEvaluatorMultiHorizon:
    """Multi-horizon evaluator regression tests."""

    def test_is_horizon_mature(self):
        """Test horizon maturity checks for each horizon."""
        from app.shadow.evaluator import _is_horizon_mature

        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)

        # 20 minutes later: 15m mature, 30m not
        now_20m = signal_time + __import__('datetime').timedelta(minutes=20)
        assert _is_horizon_mature(signal_time, 15, now_20m) is True
        assert _is_horizon_mature(signal_time, 30, now_20m) is False

        # 65 minutes later: 60m mature, 120m not
        now_65m = signal_time + __import__('datetime').timedelta(minutes=65)
        assert _is_horizon_mature(signal_time, 60, now_65m) is True
        assert _is_horizon_mature(signal_time, 120, now_65m) is False

        # 130 minutes later: 120m mature, 240m not
        now_130m = signal_time + __import__('datetime').timedelta(minutes=130)
        assert _is_horizon_mature(signal_time, 120, now_130m) is True
        assert _is_horizon_mature(signal_time, 240, now_130m) is False

        # 250 minutes later: 240m mature
        now_250m = signal_time + __import__('datetime').timedelta(minutes=250)
        assert _is_horizon_mature(signal_time, 240, now_250m) is True

    def test_is_eod_mature(self):
        """Test EOD maturity boundary logic."""
        from app.shadow.evaluator import _is_eod_mature

        # Signal at 08:00 UTC, same day 23:59 — not yet
        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
        same_day = datetime(2026, 9, 23, 23, 59, tzinfo=timezone.utc)
        assert _is_eod_mature(signal_time, same_day) is False

        # Next day 00:01 — yes
        next_day = datetime(2026, 9, 24, 0, 1, tzinfo=timezone.utc)
        assert _is_eod_mature(signal_time, next_day) is True

    def test_mfe_mae_no_lookahead(self):
        """Test that 30m horizon has >= MFE than 15m (no lookahead)."""
        from app.shadow.evaluator import _calculate_mfe_mae_for_window
        from app.models import Candle
        from datetime import timedelta

        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
        signal_price = 100.0

        # Create candles: first 3 within 15m, then a big drop at 20m
        candles = []
        for i in range(10):
            ts = signal_time + timedelta(minutes=5 * (i + 1))
            candles.append(Candle(
                timestamp=int(ts.timestamp() * 1000),
                open=100.0 - i * 0.1,
                high=100.0 - i * 0.1 + 0.2,
                low=100.0 - i * 0.1 - 0.3,
                close=100.0 - i * 0.1 - 0.1,
                volume=1000.0,
            ))

        # 15m horizon: only first 3 candles
        mfe_15, mae_15 = _calculate_mfe_mae_for_window(candles, signal_price, 15, signal_time)

        # 30m horizon: first 6 candles (includes the bigger drop)
        mfe_30, mae_30 = _calculate_mfe_mae_for_window(candles, signal_price, 30, signal_time)

        # 30m should have larger MFE than 15m (more candles to observe)
        assert mfe_30 is not None
        assert mfe_15 is not None
        assert mfe_30 >= mfe_15

    def test_calculate_mfe_mae_short_signal(self):
        """Test basic MFE/MAE calculation for SHORT signal."""
        from app.shadow.evaluator import _calculate_mfe_mae_for_window
        from app.models import Candle
        from datetime import timedelta

        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
        signal_price = 100.0

        candles = [
            Candle(
                timestamp=int((signal_time + timedelta(minutes=5 * (i + 1))).timestamp() * 1000),
                open=100.0 - i * 0.1,
                high=100.0 - i * 0.1 + 0.5,
                low=100.0 - i * 0.1 - 0.2,
                close=100.0 - i * 0.1 - 0.1,
                volume=1000.0,
            )
            for i in range(12)
        ]

        mfe, mae = _calculate_mfe_mae_for_window(candles, signal_price, 60, signal_time)
        assert mfe is not None
        assert mfe > 0  # Price dropped, good for SHORT
        assert mae is not None
        assert mae > 0  # Price rose a bit

    def test_horizon_maturity_constants(self):
        """Verify HORIZONS list covers all expected horizons."""
        from app.shadow.evaluator import HORIZONS

        names = [h[0] for h in HORIZONS]
        assert "15m" in names
        assert "30m" in names
        assert "60m" in names
        assert "120m" in names
        assert "240m" in names

    def test_calculate_mfe_mae_empty_candles(self):
        """Test MFE/MAE with empty candle list returns None."""
        from app.shadow.evaluator import _calculate_mfe_mae_for_window
        from datetime import timedelta

        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
        mfe, mae = _calculate_mfe_mae_for_window([], 100.0, 15, signal_time)
        assert mfe is None
        assert mae is None

    def test_calculate_mfe_mae_no_candles_in_window(self):
        """Test MFE/MAE when all candles are before signal_time."""
        from app.shadow.evaluator import _calculate_mfe_mae_for_window
        from app.models import Candle
        from datetime import timedelta

        signal_time = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)

        # Candles before signal_time
        candles = [
            Candle(
                timestamp=int((signal_time - timedelta(minutes=10)).timestamp() * 1000),
                open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0,
            )
        ]

        mfe, mae = _calculate_mfe_mae_for_window(candles, 100.0, 15, signal_time)
        assert mfe is None
        assert mae is None

    def test_evaluator_cli_has_no_min_age(self):
        """Test evaluator CLI no longer has --min-age-minutes."""
        from app.shadow.evaluator import main as eval_main
        import inspect

        # The source should not contain --min-age-minutes
        source = inspect.getsource(eval_main)
        assert "--min-age-minutes" not in source


class TestShadowRunnerRawCapture:
    """Regression tests for raw capture mode in shadow runner."""

    def test_passes_strict_filters_exists(self):
        """passes_strict_filters is defined on the scanner."""
        from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner
        scanner = AtrWickRejectionShortScanner()
        assert hasattr(scanner, "passes_strict_filters")

    def test_detect_signal_uses_passes_strict_filters(self):
        """detect_signal delegates to passes_strict_filters."""
        from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner
        import inspect

        scanner = AtrWickRejectionShortScanner()
        source = inspect.getsource(scanner.detect_signal)
        assert "passes_strict_filters" in source

    def test_runner_uses_detect_raw_candidate(self):
        """Runner._scan_symbol calls detect_raw_candidate, not scan()."""
        import inspect
        from app.shadow.runner import ShadowScannerRunner

        source = inspect.getsource(ShadowScannerRunner._scan_symbol)
        assert "detect_raw_candidate" in source
        assert "scanner.scan(" not in source

    def test_runner_scanner_scan_not_called(self):
        """Runner does NOT call scanner.scan() for raw collection."""
        import inspect
        from app.shadow.runner import ShadowScannerRunner

        source = inspect.getsource(ShadowScannerRunner._scan_symbol)
        assert ".scan(ctx)" not in source

    def test_runner_no_repo_in_scan_phase(self):
        """Worker _scan_symbol has NO repo calls — DB-free."""
        import inspect
        from app.shadow.runner import ShadowScannerRunner

        source = inspect.getsource(ShadowScannerRunner._scan_symbol)
        assert "repo" not in source
        assert "save_signal" not in source
        assert "signal_exists" not in source

    def test_runner_persist_in_main_thread(self):
        """Persistence happens in _persist_candidate, not in workers."""
        import inspect
        from app.shadow.runner import ShadowScannerRunner

        source = inspect.getsource(ShadowScannerRunner.run_cycle)
        assert "_persist_candidate" in source

    def test_run_cycle_returns_raw_strict_inserted(self):
        """run_cycle summary has raw/strict/inserted/scan_errors/db_errors."""
        import inspect
        from app.shadow.runner import ShadowScannerRunner

        source = inspect.getsource(ShadowScannerRunner.run_cycle)
        assert "raw_candidates" in source
        assert "strict_pass" in source
        assert "inserted" in source
        assert "duplicates" in source
        assert "scan_errors" in source
        assert "db_errors" in source