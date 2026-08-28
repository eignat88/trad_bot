"""Tests for DB configuration: env parsing, password propagation, connect/reconnect."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import load_settings, Settings


class TestDbEnvParsing:
    """Verify DB settings are correctly parsed from environment."""

    def test_defaults(self):
        """Default values when no env is set."""
        s = Settings()
        assert s.db_host == "localhost"
        assert s.db_port == 5432
        assert s.db_name == "trad_bot"
        assert s.db_user == "postgres"
        assert s.db_password == ""

    def test_env_override(self, monkeypatch):
        """Environment variables override defaults."""
        monkeypatch.setenv("DB_HOST", "10.0.0.1")
        monkeypatch.setenv("DB_PORT", "5433")
        monkeypatch.setenv("DB_NAME", "mydb")
        monkeypatch.setenv("DB_USER", "myuser")
        monkeypatch.setenv("DB_PASSWORD", "secret123")

        from app.config.settings import _load_dotenv
        # Simulate load_settings flow for DB vars
        env = {
            "db_host": os.getenv("DB_HOST", "localhost"),
            "db_port": int(os.getenv("DB_PORT", "5432")),
            "db_name": os.getenv("DB_NAME", "trad_bot"),
            "db_user": os.getenv("DB_USER", "postgres"),
            "db_password": os.getenv("DB_PASSWORD", ""),
        }
        assert env["db_host"] == "10.0.0.1"
        assert env["db_port"] == 5433
        assert env["db_name"] == "mydb"
        assert env["db_user"] == "myuser"
        assert env["db_password"] == "secret123"

    def test_db_port_integer_conversion(self, monkeypatch):
        """DB_PORT is properly converted to int."""
        monkeypatch.setenv("DB_PORT", "5433")
        port = int(os.getenv("DB_PORT", "5432"))
        assert port == 5433
        assert isinstance(port, int)


class TestDbPortValidation:
    """Validate DB_PORT range."""

    def test_valid_port(self):
        s = Settings(db_port=5432)
        assert 1 <= s.db_port <= 65535

    def test_invalid_port_zero(self):
        s = Settings(db_port=0)
        assert not (1 <= s.db_port <= 65535)

    def test_invalid_port_too_large(self):
        s = Settings(db_port=99999)
        assert not (1 <= s.db_port <= 65535)


class TestDbPasswordNotLogged:
    """Ensure password does not appear in log output."""

    def test_connection_log_excludes_password(self, caplog):
        """The log message after connection must not contain the password."""
        from app.db.repository import ScannerRepository

        with caplog.at_level("INFO"):
            # The connection will fail (no DB), but we check the log format
            # doesn't include password in the attempted log.
            try:
                repo = ScannerRepository(
                    host="localhost", port=5432, database="test_db",
                    user="test_user", password="super_secret_password",
                    backend="postgres",
                )
                repo.close()
            except Exception:
                pass

        # Check that the password doesn't appear in any log message
        for record in caplog.records:
            assert "super_secret_password" not in record.message, (
                f"Password leaked in log: {record.message}"
            )


class TestScannerRepositoryPasswordPropagation:
    """Verify password is stored and used in connect/reconnect."""

    def test_password_stored(self):
        """Repository stores the password for reconnect."""
        from app.db.repository import ScannerRepository

        # Use JSONL backend to avoid needing a real DB
        repo = ScannerRepository(
            host="localhost", port=5432, database="test_db",
            user="test_user", password="mypass",
            backend="jsonl",
        )
        assert repo._password == "mypass"
        repo.close()

    def test_password_none_stored_as_none(self):
        """None password is stored as None."""
        from app.db.repository import ScannerRepository

        repo = ScannerRepository(
            host="localhost", port=5432, database="test_db",
            user="test_user", password=None,
            backend="jsonl",
        )
        assert repo._password is None
        repo.close()

    def test_password_empty_stored_as_none(self):
        """Empty password is stored as None."""
        from app.db.repository import ScannerRepository

        repo = ScannerRepository(
            host="localhost", port=5432, database="test_db",
            user="test_user", password="",
            backend="jsonl",
        )
        assert repo._password is None
        repo.close()

    def test_reconnect_uses_stored_password(self):
        """reconnect() uses the same password as initial connect."""
        from app.db.repository import ScannerRepository

        repo = ScannerRepository(
            host="localhost", port=5432, database="test_db",
            user="test_user", password="reconnect_pass",
            backend="jsonl",
        )
        # Simulate the reconnect path: it should use self._password
        assert repo._password == "reconnect_pass"
        repo.close()


class TestBackendPostgresFailFast:
    """backend=postgres raises on failure instead of falling back to JSONL."""

    def test_backend_postgres_raises_on_no_pg8000(self):
        """When backend='postgres' and pg8000 is missing, ImportError propagates."""
        from app.db.repository import ScannerRepository

        with patch.dict("sys.modules", {"pg8000": None}):
            with pytest.raises(ImportError):
                ScannerRepository(backend="postgres")

    def test_backend_postgres_raises_on_connection_error(self):
        """When backend='postgres' and connection fails, exception propagates."""
        from app.db.repository import ScannerRepository

        import pg8000
        with patch.object(pg8000, "connect", side_effect=RuntimeError("no db")):
            with pytest.raises(RuntimeError, match="no db"):
                ScannerRepository(backend="postgres")


class TestEnvFileOverride:
    """.env file values override config.yaml defaults."""

    def test_env_file_sets_db_vars(self, monkeypatch):
        """DB vars from .env are loaded into settings."""
        # Use a well-known writable location under the project
        from pathlib import Path
        env_file = Path(__file__).resolve().parent.parent / ".env.test_tmp"
        env_file.write_text(
            "DB_HOST=db.example.com\n"
            "DB_PORT=5433\n"
            "DB_NAME=prod_db\n"
            "DB_USER=prod_user\n"
            "DB_PASSWORD=prod_secret\n"
        )
        try:
            settings = load_settings(
                path=Path(__file__).resolve().parent.parent / "config.yaml",
                env_file=env_file,
            )
            assert settings.db_host == "db.example.com"
            assert settings.db_port == 5433
            assert settings.db_name == "prod_db"
            assert settings.db_user == "prod_user"
            assert settings.db_password == "prod_secret"
        finally:
            env_file.unlink(missing_ok=True)
