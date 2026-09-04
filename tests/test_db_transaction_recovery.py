"""Regression tests for PostgreSQL transaction recovery and startup deadlock fix.

Tests verify:
1. Rollback after DB failure
2. Deadlock retry (40P01)
3. Retry exhaustion
4. 25P02 (in_failed_sql_transaction) recovery
5. Paper cycle continues after DB error (no cascading 25P02)
6. Schema not applied by both runners at startup
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, call, patch

import pytest

from app.config import Settings
from app.db.repository import (
    ScannerRepository,
    _SQLSTATE_DEADLOCK,
    _SQLSTATE_IN_FAILED_TXN,
    _SQLSTATE_SERIALIZATION,
    _get_sqlstate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pg_repo() -> ScannerRepository:
    """Create a ScannerRepository with a mock PG connection."""
    repo = ScannerRepository.__new__(ScannerRepository)
    repo._use_pg = True
    repo._conn = MagicMock()
    return repo


def _pg_exc(sqlstate: str) -> Exception:
    """Create a fake pg8000-style exception with a .sqlstate attribute."""
    exc = Exception(f"PG error {sqlstate}")
    exc.sqlstate = sqlstate  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# Test 1 — rollback after DB failure
# ---------------------------------------------------------------------------

class TestRollbackAfterFailure:
    def test_rollback_called_on_non_retryable_error(self):
        repo = _make_pg_repo()
        repo._conn.cursor().execute.side_effect = _pg_exc("42P01")

        with pytest.raises(Exception, match="42P01"):
            repo._with_retry(lambda: (_ for _ in ()).throw(_pg_exc("42P01")), label="test_op")

        repo._conn.rollback.assert_called()

    def test_successful_operation_no_rollback(self):
        repo = _make_pg_repo()
        result = repo._with_retry(lambda: 42, label="test_op")
        assert result == 42


# ---------------------------------------------------------------------------
# Test 2 — deadlock retry
# ---------------------------------------------------------------------------

class TestDeadlockRetry:
    def test_deadlock_retried_once_then_succeeds(self):
        repo = _make_pg_repo()
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _pg_exc(_SQLSTATE_DEADLOCK)
            return "success"

        result = repo._with_retry(flaky, label="test_op")
        assert result == "success"
        assert call_count == 2
        repo._conn.rollback.assert_called()

    def test_serialization_failure_retried(self):
        repo = _make_pg_repo()
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _pg_exc(_SQLSTATE_SERIALIZATION)
            return "ok"

        result = repo._with_retry(flaky, label="test_op")
        assert result == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# Test 3 — retry exhausted
# ---------------------------------------------------------------------------

class TestRetryExhausted:
    def test_deadlock_twice_raises_after_retries(self):
        repo = _make_pg_repo()
        call_count = 0

        def always_deadlock():
            nonlocal call_count
            call_count += 1
            raise _pg_exc(_SQLSTATE_DEADLOCK)

        with pytest.raises(Exception, match="40P01"):
            repo._with_retry(always_deadlock, label="test_op")

        assert call_count == 2  # 1 original + 1 retry

    def test_error_logged_after_exhaustion(self, caplog):
        repo = _make_pg_repo()

        def always_fail():
            raise _pg_exc(_SQLSTATE_DEADLOCK)

        with caplog.at_level("ERROR"):
            with pytest.raises(Exception):
                repo._with_retry(always_fail, label="test_op")

        assert "failed after 2 attempts" in caplog.text


# ---------------------------------------------------------------------------
# Test 4 — 25P02 recovery
# ---------------------------------------------------------------------------

class TestFailedTransactionRecovery:
    def test_25p02_triggers_rollback_and_retry(self):
        repo = _make_pg_repo()
        call_count = 0

        def first_call_fails():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _pg_exc(_SQLSTATE_IN_FAILED_TXN)
            return "recovered"

        result = repo._with_retry(first_call_fails, label="test_op")
        assert result == "recovered"
        assert call_count == 2
        repo._conn.rollback.assert_called()

    def test_25p02_recover_calls_rollback(self):
        repo = _make_pg_repo()
        recovered = repo._recover_connection()
        assert recovered is True
        repo._conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5 — paper cycle after DB error (no cascading 25P02)
# ---------------------------------------------------------------------------

class TestPaperCycleResilience:
    def test_run_cycle_continues_after_load_ready_fails(self):
        """Simulate _load_ready_setups raising an exception.
        Subsequent DB operations (expire, snapshot) should still execute.
        """
        from paper_runner import run_cycle

        repo = MagicMock()
        repo.load_ready_setups.side_effect = _pg_exc(_SQLSTATE_IN_FAILED_TXN)
        repo.expire_stale_setups.return_value = 0
        repo.get_paper_trade_stats.return_value = []
        repo.save_paper_account_snapshot.return_value = None

        engine = MagicMock()
        engine.open_trades = {}
        engine.balance = 1000.0
        engine._max_drawdown = 0.0
        engine._cooldown_until = None

        client = MagicMock()
        settings = Settings()

        stats = run_cycle(engine, client, repo, None, settings)

        # Despite the DB error on load_ready_setups, expire and snapshot were called
        repo.expire_stale_setups.assert_called_once()
        repo.get_paper_trade_stats.assert_called_once()
        repo.save_paper_account_snapshot.assert_called_once()
        assert stats["skipped_no_setup"] == 1

    def test_run_cycle_continues_after_expire_fails(self):
        """Simulate expire_stale_setups raising an exception.
        The snapshot block should still execute.
        """
        from paper_runner import run_cycle

        repo = MagicMock()
        repo.load_ready_setups.return_value = []
        repo.expire_stale_setups.side_effect = Exception("expire failed")
        repo.get_paper_trade_stats.return_value = []
        repo.save_paper_account_snapshot.return_value = None

        engine = MagicMock()
        engine.open_trades = {}
        engine.balance = 1000.0
        engine._max_drawdown = 0.0
        engine._cooldown_until = None

        client = MagicMock()
        settings = Settings()

        stats = run_cycle(engine, client, repo, None, settings)

        # Snapshot was still called despite expire failure
        repo.get_paper_trade_stats.assert_called_once()
        repo.save_paper_account_snapshot.assert_called_once()


# ---------------------------------------------------------------------------
# Test 6 — durable safety-gate runtime mode
# ---------------------------------------------------------------------------

class TestPaperSafetyGateModePersistence:
    def test_set_paper_safety_gate_mode_commits_after_upsert(self):
        repo = _make_pg_repo()

        repo.set_paper_safety_gate_mode("observe")

        assert repo._conn.mock_calls[-1] == call.commit()
        repo._conn.cursor().execute.assert_called_once()
        repo._conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7 — schema not applied by both runners at startup
# ---------------------------------------------------------------------------

class TestNoSchemaInStartupFlow:
    def test_scanner_runner_main_does_not_call_ensure_schema(self):
        """scanner_runner.main() should NOT call repository.ensure_schema()."""
        import inspect
        source = inspect.getsource(__import__("scanner_runner").main)
        assert "ensure_schema" not in source, (
            "scanner_runner.main() should not call ensure_schema() — "
            "schema is a deployment step, not a runtime operation"
        )

    def test_paper_runner_main_does_not_call_ensure_schema(self):
        """paper_runner.main() should NOT call repository.ensure_schema()."""
        import inspect
        source = inspect.getsource(__import__("paper_runner").main)
        assert "ensure_schema" not in source, (
            "paper_runner.main() should not call ensure_schema() — "
            "schema is a deployment step, not a runtime operation"
        )

    def test_reconnect_does_not_call_ensure_schema(self):
        """ScannerRepository.reconnect() should NOT call ensure_schema()."""
        import inspect
        source = inspect.getsource(ScannerRepository.reconnect)
        assert "ensure_schema" not in source, (
            "reconnect() should not call ensure_schema() — "
            "runtime reconnect must not run DDL"
        )

    def test_ensure_schema_still_exists_for_manual_use(self):
        """ensure_schema() method must still exist for manual/CLI usage."""
        assert hasattr(ScannerRepository, "ensure_schema")
        assert callable(getattr(ScannerRepository, "ensure_schema"))


# ---------------------------------------------------------------------------
# Test — _get_sqlstate helper
# ---------------------------------------------------------------------------

class TestGetSqlstate:
    def test_pg8000_style(self):
        exc = Exception()
        exc.sqlstate = "40P01"  # type: ignore[attr-defined]
        assert _get_sqlstate(exc) == "40P01"

    def test_psycopg2_style(self):
        exc = Exception()
        exc.pgcode = "40001"  # type: ignore[attr-defined]
        assert _get_sqlstate(exc) == "40001"

    def test_no_sqlstate(self):
        exc = Exception("plain error")
        assert _get_sqlstate(exc) is None
