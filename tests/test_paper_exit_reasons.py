"""Regression coverage for the paper-trade exit-reason database contract."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.db.repository import ScannerRepository
from app.paper.exit_reasons import PAPER_ENGINE_EXIT_REASONS, PAPER_TRADE_EXIT_REASONS


_SCHEMA_PATH = Path(__file__).parents[1] / "app" / "db" / "schema.sql"


def _schema_exit_reason_sets() -> list[set[str]]:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    constraints = re.findall(
        r"paper_trade_exit_reason_chk CHECK \(\s*"
        r"exit_reason IS NULL OR exit_reason IN \((.*?)\)\s*\)",
        schema,
        flags=re.DOTALL,
    )
    return [set(re.findall(r"'([^']+)'", constraint)) for constraint in constraints]


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executions.append((sql, params))


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _postgres_repository() -> tuple[ScannerRepository, _Connection]:
    repository = ScannerRepository.__new__(ScannerRepository)
    connection = _Connection()
    repository._use_pg = True
    repository._conn = connection
    return repository, connection


def _close(repository: ScannerRepository, reason: str) -> None:
    repository.close_paper_trade(
        trade_id=42,
        exit_price=105.0,
        exit_reason=reason,  # type: ignore[arg-type] -- exercise runtime boundary.
        exit_fee=0.1,
        pnl_usdt=5.0,
        pnl_r=0.5,
        pnl_percent=5.0,
        slippage=0.0,
        funding_paid=0.0,
        balance_after=1005.0,
        duration_sec=7200.0,
        gross_pnl=5.0,
        mfe=5.0,
        mae=0.0,
        mfe_r=0.5,
        mae_r=0.0,
        price_at_expiry=105.0,
        distance_to_tp=15.0,
        distance_to_sl=15.0,
    )


def test_schema_contract_matches_all_supported_paper_exit_reasons():
    """Both new-install and upgrade CHECK constraints match the Python contract."""
    schema_reason_sets = _schema_exit_reason_sets()
    assert schema_reason_sets == [set(PAPER_TRADE_EXIT_REASONS)] * 2
    assert PAPER_ENGINE_EXIT_REASONS <= PAPER_TRADE_EXIT_REASONS

    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS paper_trade_exit_reason_chk" in schema


@pytest.mark.parametrize("reason", sorted(PAPER_TRADE_EXIT_REASONS))
def test_repository_close_persists_every_contract_exit_reason(reason: str):
    """Repository accepts every DB-constrained value, including profitable expiry."""
    repository, connection = _postgres_repository()

    _close(repository, reason)

    assert connection.commits == 1
    assert connection.cursor_instance.executions[0][1][1] == reason


def test_repository_close_rejects_exit_reason_outside_schema_contract():
    repository, connection = _postgres_repository()

    with pytest.raises(ValueError, match="Unsupported paper trade exit reason"):
        _close(repository, "UNSUPPORTED")

    assert connection.cursor_instance.executions == []
    assert connection.commits == 0
