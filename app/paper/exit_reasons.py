"""Canonical exit-reason contract for PostgreSQL-backed paper trades.

Keep ``PAPER_TRADE_EXIT_REASONS`` synchronized with the CHECK constraint in
``app/db/schema.sql``.  The schema regression test enforces that contract.
"""
from __future__ import annotations

from typing import Final, Literal, TypeAlias


PaperTradeExitReason: TypeAlias = Literal[
    "TAKE_PROFIT_1",
    "TAKE_PROFIT_2",
    "TAKE_PROFIT_SLIPPAGE",
    "STOP_LOSS",
    "STOP_LOSS_GAP",
    "TRAILING_STOP",
    "EXPIRED",
    "EXPIRED_PROFITABLE",
    "TIMEOUT",
    "MANUAL",
    "RISK_LIMIT",
]

PAPER_TRADE_EXIT_REASONS: Final[frozenset[PaperTradeExitReason]] = frozenset({
    "TAKE_PROFIT_1",
    "TAKE_PROFIT_2",
    "TAKE_PROFIT_SLIPPAGE",
    "STOP_LOSS",
    "STOP_LOSS_GAP",
    "TRAILING_STOP",
    "EXPIRED",
    "EXPIRED_PROFITABLE",
    "TIMEOUT",
    "MANUAL",
    "RISK_LIMIT",
})

# Reasons emitted by PaperTradingEngine.  The wider contract also retains
# supported operator/legacy close reasons accepted by the repository.
PAPER_ENGINE_EXIT_REASONS: Final[frozenset[PaperTradeExitReason]] = frozenset({
    "TAKE_PROFIT_1",
    "TAKE_PROFIT_2",
    "STOP_LOSS",
    "STOP_LOSS_GAP",
    "TRAILING_STOP",
    "EXPIRED",
    "EXPIRED_PROFITABLE",
})

EXPIRED_EXIT_REASONS: Final[frozenset[PaperTradeExitReason]] = frozenset({
    "EXPIRED",
    "EXPIRED_PROFITABLE",
})
