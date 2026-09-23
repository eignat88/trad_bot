"""Tests for Shadow CLI entrypoints."""
from __future__ import annotations

import sys
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