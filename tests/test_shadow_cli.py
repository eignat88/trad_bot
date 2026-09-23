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
                                "symbols_with_signals": 0,
                                "total_signals": 0,
                            }
                            mock_runner.return_value = mock_runner_instance

                            from app.shadow.runner import main
                            main()

                            # Verify BybitClient was called with settings
                            mock_client.assert_called_once_with(mock_settings_obj)

                            # Verify runner was called
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
                                "signals_evaluated": 0,
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
        assert stats.wick_atr_fail == 0
        assert stats.close_location_fail == 0
        assert stats.rsi_fail == 0
        assert stats.bb_fail == 0
        assert stats.ema_slope_fail == 0
        assert stats.volume_fail == 0
        assert stats.signal_pass == 0
        assert stats.signals_inserted == 0
        assert stats.duplicates_skipped == 0

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

    def test_detect_with_diagnostics_returns_valid(self):
        """Test _detect_with_diagnostics returns a valid rejection reason."""
        from app.shadow.backfill import ShadowBackfillRunner
        from app.models import Candle
        from app.scanners.models import MarketContext, IndicatorSnapshot, MarketLevels

        # Create a mock runner
        mock_settings = MagicMock()
        mock_client = MagicMock()
        mock_repo = MagicMock()
        runner = ShadowBackfillRunner(mock_settings, mock_client, mock_repo)

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

        # Test diagnostic detection
        result = runner._detect_with_diagnostics(ctx)

        # Should return a valid rejection reason or PASS
        valid_results = [
            "PASS", "WICK_ATR", "CLOSE_LOCATION", "RSI",
            "BB", "EMA_SLOPE", "VOLUME", "INSUFFICIENT_DATA"
        ]
        assert result in valid_results

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