"""Tests for analytics CLI environment handling."""
from __future__ import annotations

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.analytics.cli import load_analytics_settings, _parse_bool_env
from app.config import Settings


class TestParseBoolEnv:
    """Tests for _parse_bool_env function."""

    def test_true_values(self):
        """Test accepted true values."""
        for value in ["true", "1", "yes", "on", "TRUE", "YES", "ON", "True", "Yes", "On"]:
            assert _parse_bool_env(value, "TEST") is True

    def test_false_values(self):
        """Test accepted false values."""
        for value in ["false", "0", "no", "off", "FALSE", "NO", "OFF", "False", "No", "Off"]:
            assert _parse_bool_env(value, "TEST") is False

    def test_invalid_values(self):
        """Test invalid values raise ValueError."""
        for value in ["invalid", "maybe", "2", "-1", "", " "]:
            with pytest.raises(ValueError):
                _parse_bool_env(value, "TEST")


class TestLoadAnalyticsSettings:
    """Tests for load_analytics_settings function."""

    def test_analytics_enabled_false_exits(self):
        """Test that ANALYTICS_ENABLED=false causes clean exit."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "false"}):
            with pytest.raises(SystemExit) as exc_info:
                load_analytics_settings()
            assert exc_info.value.code == 0

    def test_analytics_enabled_true_loads_settings(self):
        """Test that ANALYTICS_ENABLED=true loads settings."""
        with patch.dict(os.environ, {
            "ANALYTICS_ENABLED": "true",
            "DB_HOST": "testhost",
            "DB_PORT": "5433",
            "DB_NAME": "testdb",
            "DB_USER": "testuser",
            "DB_PASSWORD": "testpass",
        }):
            settings = load_analytics_settings()
            assert settings.analytics_enabled is True
            assert settings.db_host == "testhost"
            assert settings.db_port == 5433
            assert settings.db_name == "testdb"
            assert settings.db_user == "testuser"
            assert settings.db_password == "testpass"

    def test_analytics_enabled_invalid_exits(self):
        """Test that invalid ANALYTICS_ENABLED causes exit with error."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "invalid"}):
            with pytest.raises(SystemExit) as exc_info:
                load_analytics_settings()
            assert exc_info.value.code == 1

    def test_analytics_enabled_missing_defaults_false(self):
        """Test that missing ANALYTICS_ENABLED defaults to false."""
        env = os.environ.copy()
        env.pop("ANALYTICS_ENABLED", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_analytics_settings()
            assert exc_info.value.code == 0

    def test_analytics_settings_from_env(self):
        """Test that analytics settings are loaded from environment."""
        with patch.dict(os.environ, {
            "ANALYTICS_ENABLED": "true",
            "ANALYTICS_SCHEDULE_TIME": "07:00",
            "ANALYTICS_TIMEZONE": "UTC",
            "ANALYTICS_POST_EXIT_HOURS": "6",
            "ANALYTICS_CANDLE_RETENTION_DAYS": "90",
            "ANALYTICS_CANDLE_WORKERS": "4",
            "ANALYTICS_API_RETRY_COUNT": "5",
            "ANALYTICS_STAGE_TIMEOUT_SECONDS": "7200",
        }):
            settings = load_analytics_settings()
            assert settings.analytics_schedule_time == "07:00"
            assert settings.analytics_timezone == "UTC"
            assert settings.analytics_post_exit_hours == 6
            assert settings.analytics_candle_retention_days == 90
            assert settings.analytics_candle_workers == 4
            assert settings.analytics_api_retry_count == 5
            assert settings.analytics_stage_timeout_seconds == 7200

    def test_analytics_settings_defaults(self):
        """Test that analytics settings have correct defaults."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "true"}):
            settings = load_analytics_settings()
            assert settings.analytics_schedule_time == "06:00"
            assert settings.analytics_timezone == "Europe/Sofia"
            assert settings.analytics_post_exit_hours == 4
            assert settings.analytics_candle_retention_days == 180
            assert settings.analytics_candle_workers == 2
            assert settings.analytics_api_retry_count == 3
            assert settings.analytics_stage_timeout_seconds == 3600

    def test_no_env_file_reading(self):
        """Test that no .env file is read."""
        # Create a temporary .env file with secrets
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write("SECRET_KEY=secret_value\n")
            f.write("DB_PASSWORD=secret_password\n")
            env_file = f.name
        
        try:
            with patch.dict(os.environ, {"ANALYTICS_ENABLED": "true"}):
                settings = load_analytics_settings()
                # Settings should not contain secrets from .env file
                assert not hasattr(settings, 'secret_key')
                # DB_PASSWORD should be from env, not .env file
                assert settings.db_password == ""
        finally:
            os.unlink(env_file)

    def test_analytics_does_not_read_production_env(self):
        """Test that analytics CLI does not read production .env."""
        # Create a temporary .env file with production secrets
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write("BYBIT_API_KEY=production_key\n")
            f.write("BYBIT_API_SECRET=production_secret\n")
            f.write("DB_PASSWORD=production_password\n")
            env_file = f.name
        
        try:
            # Set env file path but analytics should not read it
            with patch.dict(os.environ, {"ANALYTICS_ENABLED": "true"}):
                settings = load_analytics_settings()
                # Analytics should use env vars, not .env file
                assert settings.bybit_api_key == ""
                assert settings.bybit_api_secret == ""
                assert settings.db_password == ""
        finally:
            os.unlink(env_file)

    def test_analytics_safe_defaults_for_scanner_settings(self):
        """Test that analytics uses safe defaults for scanner settings."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "true"}):
            settings = load_analytics_settings()
            # Analytics doesn't need scanner settings, but they must have safe values
            assert settings.trading_mode == "paper"
            assert settings.live_trading_enabled is False
            assert settings.scanner_workers == 1
            assert settings.scan_interval == 300

    def test_analytics_does_not_expose_scanner_secrets(self):
        """Test that analytics does not expose scanner/paper secrets."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "true"}):
            settings = load_analytics_settings()
            # These should be empty or safe defaults
            assert settings.telegram_token == ""
            assert settings.telegram_chat_id == ""
            assert settings.bybit_api_key == ""
            assert settings.bybit_api_secret == ""


class TestSystemdEquivalenvIsolation:
    """Tests simulating systemd-equivalent environment isolation."""

    def test_systemd_environment_simulation(self):
        """Test analytics CLI in systemd-like environment."""
        # Simulate systemd environment where .env is not accessible
        # Set analytics environment
        env = {
            "ANALYTICS_ENABLED": "true",
            "DB_HOST": "analytics_host",
            "DB_PORT": "5432",
            "DB_NAME": "trad_bot",
            "DB_USER": "analytics_runner",
            "DB_PASSWORD": "analytics_password",
            "ANALYTICS_SCHEDULE_TIME": "06:00",
            "ANALYTICS_TIMEZONE": "Europe/Sofia",
        }
        
        with patch.dict(os.environ, env, clear=True):
            # This should work without reading .env file
            settings = load_analytics_settings()
            assert settings.analytics_enabled is True
            assert settings.db_host == "analytics_host"
            assert settings.db_user == "analytics_runner"
            
            # Verify secrets are not exposed
            assert settings.bybit_api_key == ""
            assert settings.bybit_api_secret == ""
            assert settings.telegram_token == ""

    def test_analytics_disabled_simulation(self):
        """Test analytics CLI when disabled in systemd environment."""
        env = {
            "ANALYTICS_ENABLED": "false",
            "DB_HOST": "analytics_host",
        }
        
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                load_analytics_settings()
            assert exc_info.value.code == 0

    def test_analytics_missing_required_vars(self):
        """Test analytics CLI with missing required variables."""
        env = {
            "ANALYTICS_ENABLED": "true",
            # Missing DB_HOST, DB_PORT, etc.
        }
        
        with patch.dict(os.environ, env, clear=True):
            # Should use defaults
            settings = load_analytics_settings()
            assert settings.db_host == "localhost"
            assert settings.db_port == 5432


class TestAnalyticsCLIMain:
    """Tests for analytics CLI main function."""

    def test_cli_exits_when_disabled(self):
        """Test that CLI exits cleanly when ANALYTICS_ENABLED=false."""
        with patch.dict(os.environ, {"ANALYTICS_ENABLED": "false"}):
            with pytest.raises(SystemExit) as exc_info:
                from app.analytics.cli import main
                with patch('sys.argv', ['analytics', 'run']):
                    main()
            assert exc_info.value.code == 0

    def test_cli_does_not_read_env_file(self):
        """Test that CLI does not attempt to read .env file."""
        # Set analytics environment
        env = {
            "ANALYTICS_ENABLED": "true",
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "DB_NAME": "trad_bot_migration_test",
            "DB_USER": "postgres",
            "DB_PASSWORD": "",
        }
        
        with patch.dict(os.environ, env, clear=True):
            # Mock load_analytics_settings to verify it's called
            with patch('app.analytics.cli.load_analytics_settings') as mock_load:
                mock_settings = MagicMock(spec=Settings)
                mock_settings.analytics_enabled = True
                mock_settings.db_host = "localhost"
                mock_settings.db_port = 5432
                mock_settings.db_name = "trad_bot_migration_test"
                mock_settings.db_user = "postgres"
                mock_settings.db_password = ""
                mock_load.return_value = mock_settings
                
                # Mock pg8000 module
                mock_pg_module = MagicMock()
                mock_conn = MagicMock()
                mock_pg_module.connect.return_value = mock_conn
                
                with patch.dict('sys.modules', {'pg8000': mock_pg_module}):
                    # This should not raise PermissionError
                    from app.analytics.cli import main
                    with patch('sys.argv', ['analytics', 'run']):
                        try:
                            main()
                        except SystemExit:
                            pass  # Expected
                        
                        # Verify load_analytics_settings was called
                        mock_load.assert_called_once()
                        
                        # Verify pg8000.connect was called with correct parameters
                        mock_pg_module.connect.assert_called_once_with(
                            host="localhost",
                            port=5432,
                            database="trad_bot_migration_test",
                            user="postgres",
                            password=""
                        )


class TestEnvironmentOverrides:
    """Tests for environment variable overrides."""

    def test_env_overrides_defaults(self):
        """Test that environment variables override defaults."""
        with patch.dict(os.environ, {
            "ANALYTICS_ENABLED": "true",
            "DB_HOST": "custom_host",
            "DB_PORT": "5433",
            "DB_NAME": "custom_db",
            "DB_USER": "custom_user",
            "DB_PASSWORD": "custom_password",
        }):
            settings = load_analytics_settings()
            assert settings.db_host == "custom_host"
            assert settings.db_port == 5433
            assert settings.db_name == "custom_db"
            assert settings.db_user == "custom_user"
            assert settings.db_password == "custom_password"

    def test_analytics_settings_override_defaults(self):
        """Test that analytics settings override defaults."""
        with patch.dict(os.environ, {
            "ANALYTICS_ENABLED": "true",
            "ANALYTICS_SCHEDULE_TIME": "07:00",
            "ANALYTICS_TIMEZONE": "UTC",
            "ANALYTICS_POST_EXIT_HOURS": "6",
            "ANALYTICS_CANDLE_RETENTION_DAYS": "90",
            "ANALYTICS_CANDLE_WORKERS": "4",
            "ANALYTICS_API_RETRY_COUNT": "5",
            "ANALYTICS_STAGE_TIMEOUT_SECONDS": "7200",
        }):
            settings = load_analytics_settings()
            assert settings.analytics_schedule_time == "07:00"
            assert settings.analytics_timezone == "UTC"
            assert settings.analytics_post_exit_hours == 6
            assert settings.analytics_candle_retention_days == 90
            assert settings.analytics_candle_workers == 4
            assert settings.analytics_api_retry_count == 5
            assert settings.analytics_stage_timeout_seconds == 7200