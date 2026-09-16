"""Unit tests for ScannerRepository transport selection (unix_sock vs TCP).

Verifies that pg8000.connect() is called with the correct arguments
depending on whether unix_sock is provided.  Uses mock to avoid
requiring a real PostgreSQL connection.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch


def _make_fake_pg8000():
    """Create a fake pg8000 module with a mock connect()."""
    fake = ModuleType("pg8000")
    fake.connect = MagicMock(return_value=MagicMock())
    return fake


class TestScannerRepositoryTransport:
    """Verify transport-aware connection in ScannerRepository.__init__."""

    def test_tcp_default_when_no_unix_sock(self):
        """unix_sock=None -> pg8000.connect(host=..., port=...)."""
        fake_pg = _make_fake_pg8000()
        with patch.dict(sys.modules, {"pg8000": fake_pg}):
            from app.db.repository import ScannerRepository
            repo = ScannerRepository(
                host="localhost", port=5432, database="test_db",
                user="postgres", backend="postgres",
            )

        fake_pg.connect.assert_called_once_with(
            host="localhost", port=5432, database="test_db",
            user="postgres", password=None,
        )
        assert repo._use_pg is True
        assert repo._unix_sock is None

    def test_unix_sock_bypasses_host_port(self):
        """unix_sock set -> pg8000.connect(unix_sock=..., no host/port)."""
        fake_pg = _make_fake_pg8000()
        with patch.dict(sys.modules, {"pg8000": fake_pg}):
            from app.db.repository import ScannerRepository
            repo = ScannerRepository(
                host="localhost", port=5432, database="test_db",
                user="postgres", backend="postgres",
                unix_sock="/var/run/postgresql/.s.PGSQL.5432",
            )

        fake_pg.connect.assert_called_once_with(
            unix_sock="/var/run/postgresql/.s.PGSQL.5432",
            database="test_db", user="postgres", password=None,
        )
        assert repo._use_pg is True
        assert repo._unix_sock == "/var/run/postgresql/.s.PGSQL.5432"

    def test_reconnect_uses_unix_sock(self):
        """reconnect() when _unix_sock is set -> pg8000.connect(unix_sock=...)."""
        fake_pg = _make_fake_pg8000()
        with patch.dict(sys.modules, {"pg8000": fake_pg}):
            from app.db.repository import ScannerRepository
            repo = ScannerRepository(
                host="localhost", port=5432, database="test_db",
                user="postgres", backend="postgres",
                unix_sock="/var/run/postgresql/.s.PGSQL.5432",
            )
            fake_pg.connect.reset_mock()

            result = repo.reconnect()

        assert result is True
        fake_pg.connect.assert_called_once_with(
            unix_sock="/var/run/postgresql/.s.PGSQL.5432",
            database="test_db", user="postgres", password=None,
        )

    def test_reconnect_uses_tcp_when_no_unix_sock(self):
        """reconnect() when _unix_sock is None -> pg8000.connect(host=..., port=...)."""
        fake_pg = _make_fake_pg8000()
        with patch.dict(sys.modules, {"pg8000": fake_pg}):
            from app.db.repository import ScannerRepository
            repo = ScannerRepository(
                host="db.example.com", port=5433, database="test_db",
                user="admin", backend="postgres",
            )
            fake_pg.connect.reset_mock()

            result = repo.reconnect()

        assert result is True
        fake_pg.connect.assert_called_once_with(
            host="db.example.com", port=5433, database="test_db",
            user="admin", password=None,
        )

    def test_unix_sock_stored_on_instance(self):
        """unix_sock is stored as _unix_sock for reconnect to use."""
        fake_pg = _make_fake_pg8000()
        with patch.dict(sys.modules, {"pg8000": fake_pg}):
            from app.db.repository import ScannerRepository
            repo = ScannerRepository(
                database="test_db", backend="postgres",
                unix_sock="/tmp/.s.PGSQL.5432",
            )
        assert repo._unix_sock == "/tmp/.s.PGSQL.5432"

    def test_jsonl_backend_ignores_unix_sock(self):
        """JSONL backend does not attempt any pg8000 connection."""
        from app.db.repository import ScannerRepository
        repo = ScannerRepository(
            backend="jsonl", jsonl_path="/tmp/test.jsonl",
            unix_sock="/var/run/postgresql/.s.PGSQL.5432",
        )
        assert repo._use_pg is False
        assert repo._unix_sock == "/var/run/postgresql/.s.PGSQL.5432"
